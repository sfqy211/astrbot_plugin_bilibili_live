import asyncio
import random
from typing import Optional

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from .blivedm import WebClient, OpenLiveClient
from .blivedm.models import message as bili_msg
from .context_rec import ContextRecord


@register("astrbot_plugin_bilibili_live", "Raven95676", "接入Bilibili直播", "0.3.0")
class BilibiliLive(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.web_client: Optional[WebClient] = None
        self.open_live_client = None
        if config["blivedm_web"]["enable"]:
            self.web_client = WebClient(
                config["blivedm_web"]["room_id"],
                cookie_str=config["blivedm_web"]["cookie_str"],
            )
        if config["blivedm_open_live"]["enable"]:
            self.open_live_client = OpenLiveClient(
                config["blivedm_open_live"]["access_key_id"],
                config["blivedm_open_live"]["access_key_secret"],
                config["blivedm_open_live"]["app_id"],
                config["blivedm_open_live"]["room_owner_auth_code"],
            )
        self.context_rec = ContextRecord(
            max_messages=config["plugin_settings"]["llm_chat_max_context"]
        )
        self.allow_message_type = {
            item.strip().lower()
            for item in self.config["plugin_settings"]["allow_message_type"].split(",")
        }
        self._process_task: asyncio.Task | None = None
        self._switch_lock: Optional[asyncio.Lock] = None
        self._http_runner: Optional[web.AppRunner] = None
        self._http_port: int = 0
        self._current_room_id: int = config["blivedm_web"]["room_id"] if config["blivedm_web"]["enable"] else 0

    async def initialize(self):
        """初始化"""
        if self.web_client:
            self.web_client.start()
        elif self.open_live_client:
            self.open_live_client.start()
        self._process_task = asyncio.create_task(self._process_messages())
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

        # 获取实际端口（port=0 时系统分配）
        for sock in site._server.sockets:
            self._http_port = sock.getsockname()[1]
            break

        self._http_runner = runner
        logger.info(f"BiliDanmu HTTP API 已启动: http://127.0.0.1:{self._http_port}")

    async def _handle_status(self, request):
        """GET /api/status — 返回当前状态"""
        return web.json_response({
            "room_id": self._current_room_id,
            "is_running": self.web_client is not None and self.web_client.is_running,
            "http_port": self._http_port,
        })

    async def _handle_switch_room(self, request):
        """POST /api/switch-room — 切换直播间
        Body: { "room_id": 12345 }
        """
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
        """POST /api/trigger — 手动触发 AI 回复/总结
        Body: { "action": "reply" | "summary", "context": "最近弹幕文本..." }
        """
        try:
            data = await request.json()
            action = data.get("action", "reply")
            context = data.get("context", "")

            if action == "summary":
                prompt = f"请对以下直播间弹幕进行简短总结（30字以内）：\n{context}"
            else:
                prompt = f"你是一个直播间观众，请根据以下弹幕内容，用轻松自然的口吻回复（20字以内）：\n{context}"

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
        """切房锁，防止并发切换"""
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    async def _do_switch_room(self, new_room_id: int):
        """执行房间切换"""
        # 停止当前消息处理任务
        if self._process_task:
            self._process_task.cancel()
            try:
                await asyncio.wait_for(self._process_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._process_task = None

        # 关闭旧连接
        if self.web_client:
            await self.web_client.stop_and_close()
            self.web_client = None

        # 清空上下文记录
        self.context_rec.clear()

        # 创建新连接
        cookie_str = self.config["blivedm_web"].get("cookie_str", "")
        self.web_client = WebClient(new_room_id, cookie_str=cookie_str)
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
        elif self.open_live_client:
            async for message in self.open_live_client.get_messages():
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
        """发送消息"""
        logger.debug(f"bilibili_live message: {message}")
        work_mode = self.config["plugin_settings"]["work_mode"]

        if work_mode == "forward_only":
            for dest in self.config["plugin_settings"]["forward_destinations"]:
                await self.context.send_message(dest, MessageChain([Plain(message)]))
        elif work_mode == "llm_chat_forward":
            resp = await self._send_llm_message(sender, message)
            for dest in self.config["plugin_settings"]["forward_destinations"]:
                await self.context.send_message(dest, resp.result_chain)
        elif work_mode == "llm_chat_callback":
            method = self.config["plugin_settings"]["llm_chat_callback"][
                "callback_method"
            ]
            url = self.config["plugin_settings"]["llm_chat_callback"]["callback_url"]
            resp = await self._send_llm_message(sender, message)

            payload = {
                "room_id": self._current_room_id,
                "sender": sender,
                "sender_name": sender_name,
                "message": resp.result_chain.get_plain_text(),
            }

            async with aiohttp.ClientSession() as session:
                if method == "GET":
                    async with session.get(url, params=payload) as r:
                        if r.status != 200:
                            logger.error(f"回调失败: {r.status}, {await r.text()}")
                else:
                    async with session.post(url, json=payload) as r:
                        if r.status != 200:
                            logger.error(f"回调失败: {r.status}, {await r.text()}")

    async def terminate(self):
        """清理资源"""
        # 关闭 HTTP 服务
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None

        if self._process_task:
            self._process_task.cancel()
            try:
                await asyncio.wait_for(self._process_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                if self.web_client:
                    await self.web_client.stop_and_close()
                if self.open_live_client:
                    await self.open_live_client.stop_and_close()
