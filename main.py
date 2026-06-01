import asyncio
import random
from collections import deque
from typing import Optional

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from .blivedm import WebClient
from .blivedm.models import message as bili_msg
from .context_rec import ContextRecord

MAX_DANMAKU_BUFFER = 100


@register("astrbot_plugin_bilibili_live", "Raven95676", "接入Bilibili直播", "0.3.0")
class BilibiliLive(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.web_client: Optional[WebClient] = None
        self.cookie_str: str = config["blivedm_web"].get("cookie_str", "")

        self.context_rec = ContextRecord(
            max_messages=config["plugin_settings"]["llm_chat_max_context"]
        )
        self.allow_message_type = {
            item.strip().lower()
            for item in self.config["plugin_settings"]["allow_message_type"].split(",")
        }
        self._danmaku_buffer: deque[str] = deque(maxlen=MAX_DANMAKU_BUFFER)
        self._process_task: asyncio.Task | None = None
        self._switch_lock: Optional[asyncio.Lock] = None
        self._http_runner: Optional[web.AppRunner] = None
        self._http_port: int = 0
        self._current_room_id: int = 0

    async def initialize(self):
        """初始化 — 仅启动 HTTP 服务，等待 BiliDanmu 通过 API 切房"""
        await self._start_http_server()

    async def _start_http_server(self):
        """启动 HTTP API 服务"""
        port = self.config["plugin_settings"].get("http_server_port", 0)
        if port is None:
            port = 0

        app = web.Application()
        app.router.add_post("/api/switch-room", self._handle_switch_room)
        app.router.add_post("/api/trigger", self._handle_trigger)
        app.router.add_get("/api/status", self._handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        for sock in site._server.sockets:
            self._http_port = sock.getsockname()[1]
            break

        self._http_runner = runner
        logger.info(f"BiliDanmu HTTP API 已启动: http://127.0.0.1:{self._http_port}")

    async def _handle_status(self, request):
        """GET /api/status"""
        return web.json_response({
            "room_id": self._current_room_id,
            "is_running": self.web_client is not None and self.web_client.is_running,
            "http_port": self._http_port,
        })

    async def _handle_switch_room(self, request):
        """POST /api/switch-room — 切换直播间"""
        try:
            data = await request.json()
            new_room_id = data.get("room_id")
            if not new_room_id or not isinstance(new_room_id, int):
                return web.json_response({"ok": False, "error": "room_id 必须是整数"}, status=400)

            async with self._get_lock():
                await self._do_switch_room(new_room_id)

            return web.json_response({"ok": True, "room_id": new_room_id})
        except Exception as e:
            logger.exception("切换房间失败")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_trigger(self, request):
        """POST /api/trigger — 手动触发 AI 回复/总结"""
        try:
            data = await request.json()
            action = data.get("action", "reply")

            if action not in ("reply", "summary"):
                return web.json_response({"ok": False, "error": "action 只支持 reply 或 summary"}, status=400)

            # 使用插件缓冲区中的弹幕作为上下文
            recent = "\n".join(self._danmaku_buffer)
            if not recent:
                recent = "（暂无弹幕数据）"

            if action == "summary":
                prompt = f"请对以下直播间弹幕内容进行简短总结：\n{recent}"
            else:
                prompt = f"你是一个直播间观众，请根据以下弹幕内容，用轻松自然的口吻回复一条弹幕（20字以内）：\n{recent}"

            resp = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                session_id=f"bilidanmu_{self._current_room_id}",
            )
            reply = resp.result_chain.get_plain_text()

            return web.json_response({"ok": True, "reply": reply})
        except Exception as e:
            logger.exception("手动触发失败")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    def _get_lock(self):
        """切房锁"""
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    async def _do_switch_room(self, new_room_id: int):
        """执行房间切换"""
        if self._process_task:
            self._process_task.cancel()
            try:
                await asyncio.wait_for(self._process_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._process_task = None

        if self.web_client:
            await self.web_client.stop_and_close()
            self.web_client = None

        self.context_rec.clear()
        self._danmaku_buffer.clear()

        self.web_client = WebClient(new_room_id, cookie_str=self.cookie_str)
        self.web_client.start()
        self._current_room_id = new_room_id
        self._process_task = asyncio.create_task(self._process_messages())

        logger.info(f"已切换到直播间 {new_room_id}")

    async def _process_messages(self):
        """获取消息并处理"""
        if self.web_client:
            async for message in self.web_client.get_messages():
                await asyncio.sleep(0.8)
                await self._handle_message(message)

    @staticmethod
    def _get_sender_id(message):
        """从消息中提取发送者ID"""
        return message.user_id if message.user_id != "0" else message.user_name

    async def _handle_message(self, message: bili_msg.BiliMessage):
        """处理消息分类"""
        if self.config["plugin_settings"]["random_drop"]["enable"]:
            if (
                random.random()
                < self.config["plugin_settings"]["random_drop"]["drop_rate"]
            ):
                logger.debug("Drop message")
                return

        sender = self._get_sender_id(message)

        if (
            isinstance(message, bili_msg.DanmakuMessage)
            and "danmaku" in self.allow_message_type
        ):
            # 缓存弹幕用于手动触发
            self._danmaku_buffer.append(f"{message.user_name}: {message.content}")
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[弹幕] {message.user_name}({message.user_id})说: {message.content}",
            )
        elif (
            isinstance(message, bili_msg.GiftMessage)
            and "gift" in self.allow_message_type
        ):
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[礼物] {message.user_name}({message.user_id})赠送了{message.gift_num}个{message.gift_name}",
            )
        elif (
            isinstance(message, bili_msg.SuperChatMessage)
            and "super_chat" in self.allow_message_type
        ):
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[醒目留言] {message.user_name}({message.user_id})说: {message.message}",
            )
        elif (
            isinstance(message, bili_msg.LikeMessage)
            and "like" in self.allow_message_type
        ):
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[点赞] {message.user_name}({message.user_id})点赞了",
            )
        elif (
            isinstance(message, bili_msg.EnterRoomMessage)
            and "enter_room" in self.allow_message_type
        ):
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[进入直播间] {message.user_name}({message.user_id})进入了直播间",
            )
        elif (
            isinstance(message, bili_msg.GuardBuyMessage)
            and "guard_buy" in self.allow_message_type
        ):
            guard_level_names = {1: "总督", 2: "提督", 3: "舰长"}
            guard_level_name = guard_level_names.get(message.guard_level, "未知")
            await self._send_message(
                sender=sender,
                sender_name=message.user_name,
                message=f"[上舰] {message.user_name}({message.user_id})成为了{guard_level_name}",
            )

    async def _send_llm_message(self, sender: str, message: str):
        """处理LLM聊天并更新上下文"""
        resp = await self.context.get_using_provider().text_chat(
            prompt=message,
            session_id=None,
            contexts=self.context_rec.get_messages(sender),
        )
        self.context_rec.put_message(sender, message, False)
        self.context_rec.put_message(sender, resp.result_chain.get_plain_text(), True)
        logger.debug(f"LLM Context: {self.context_rec.get_messages(sender)}")
        return resp

    async def _send_message(self, sender: str, sender_name: str, message: str):
        """发送消息（回调模式）"""
        logger.debug(f"bilibili_live message: {message}")

        callback_url = self.config["plugin_settings"]["llm_chat_callback"]["callback_url"]
        callback_method = self.config["plugin_settings"]["llm_chat_callback"]["callback_method"]

        resp = await self._send_llm_message(sender, message)

        payload = {
            "room_id": self._current_room_id,
            "sender": sender,
            "sender_name": sender_name,
            "message": resp.result_chain.get_plain_text(),
        }

        async with aiohttp.ClientSession() as session:
            if callback_method == "GET":
                async with session.get(callback_url, params=payload) as r:
                    if r.status != 200:
                        logger.error(f"回调失败: {r.status}, {await r.text()}")
            else:
                async with session.post(callback_url, json=payload) as r:
                    if r.status != 200:
                        logger.error(f"回调失败: {r.status}, {await r.text()}")

    async def terminate(self):
        """清理资源"""
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None

        if self._process_task:
            self._process_task.cancel()
            try:
                await asyncio.wait_for(self._process_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self.web_client:
            await self.web_client.stop_and_close()
            self.web_client = None
