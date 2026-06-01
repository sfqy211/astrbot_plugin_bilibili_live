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

DEFAULT_BUFFER_SIZE = 500
DEFAULT_AUTO_SUMMARY_THRESHOLD = 100


@register("astrbot_plugin_bilibili_live", "Raven95676", "接入Bilibili直播", "0.3.0")
class BilibiliLive(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.web_client: Optional[WebClient] = None
        self.cookie_str: str = config["blivedm_web"].get("cookie_str", "")

        raw_types = self.config["plugin_settings"].get("allow_message_type", "")
        if not raw_types or not raw_types.strip():
            raw_types = "danmaku, gift, guard_buy, super_chat, like, enter_room"
        self.allow_message_type = {
            item.strip().lower() for item in raw_types.split(",") if item.strip()
        }
        self.filter_emoticon_only: bool = self.config["plugin_settings"].get(
            "filter_emoticon_only", True
        )
        buffer_size = self.config["plugin_settings"].get(
            "buffer_size", DEFAULT_BUFFER_SIZE
        )
        self._danmaku_buffer: deque[str] = deque(maxlen=buffer_size)
        self._process_task: asyncio.Task | None = None
        self._switch_lock: Optional[asyncio.Lock] = None
        self._http_runner: Optional[web.AppRunner] = None
        self._http_port: int = 0
        self._current_room_id: int = 0
        self._callback_url: str = ""

        # 自动总结
        self._auto_summary_threshold: int = self.config["plugin_settings"].get(
            "auto_summary_threshold", DEFAULT_AUTO_SUMMARY_THRESHOLD
        )
        self._messages_since_summary: int = 0
        self._last_summary: str = ""

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
        app.router.add_post("/api/disconnect", self._handle_disconnect)
        app.router.add_post("/api/trigger", self._handle_trigger)
        app.router.add_post("/api/learn", self._handle_learn)
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
        return web.json_response(
            {
                "room_id": self._current_room_id,
                "is_running": self.web_client is not None
                and self.web_client.is_running,
                "http_port": self._http_port,
            }
        )

    async def _handle_switch_room(self, request):
        """POST /api/switch-room — 切换直播间"""
        try:
            data = await request.json()
            new_room_id = data.get("room_id")
            if not new_room_id or not isinstance(new_room_id, int):
                return web.json_response(
                    {"ok": False, "error": "room_id 必须是整数"}, status=400
                )

            # 存储回调地址（BiliDanmu 用于接收自动总结等结果）
            callback_url = data.get("callback_url", "")
            if callback_url:
                self._callback_url = callback_url

            async with self._get_lock():
                await self._do_switch_room(new_room_id)

            return web.json_response({"ok": True, "room_id": new_room_id})
        except Exception as e:
            logger.exception("切换房间失败")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_disconnect(self, request):
        """POST /api/disconnect — 断开当前直播间"""
        try:
            async with self._get_lock():
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

                self._danmaku_buffer.clear()
                self._messages_since_summary = 0
                self._last_summary = ""
                self._current_room_id = 0
                self._callback_url = ""

            logger.info("[BiliDanmu] 已断开连接")
            return web.json_response({"ok": True})
        except Exception as e:
            logger.exception("断开连接失败")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_trigger(self, request):
        """POST /api/trigger — 手动触发 AI 回复/总结"""
        try:
            data = await request.json()
            action = data.get("action", "reply")

            if action not in ("reply", "summary"):
                return web.json_response(
                    {"ok": False, "error": "action 只支持 reply 或 summary"}, status=400
                )

            recent = "\n".join(self._danmaku_buffer)
            if not recent:
                recent = "（暂无弹幕数据）"

            if action == "summary":
                prompt = self._build_summary_prompt(recent)
            else:
                prompt = (
                    "你是一个直播间观众，请根据以下弹幕内容，生成3条不同的回复选项。"
                    "每条回复不超过30字，风格可以略有不同（如幽默、友好、简短）。"
                    "请严格按以下格式输出，每行一条，不要编号：\n"
                    "回复1\n回复2\n回复3\n\n"
                    f"弹幕内容：\n{recent}"
                )

            resp = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                session_id=f"bilidanmu_{self._current_room_id}",
            )
            raw = resp.result_chain.get_plain_text()

            if action == "summary":
                self._last_summary = raw
                self._messages_since_summary = 0
                return web.json_response({"ok": True, "replies": [raw]})

            # 解析多条回复
            options = [
                line.strip() for line in raw.strip().splitlines() if line.strip()
            ]
            if not options:
                options = [raw.strip()]

            return web.json_response({"ok": True, "replies": options})
        except Exception as e:
            logger.exception("[BiliDanmu] 手动触发失败")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_learn(self, request):
        """POST /api/learn — 后台分析用户选择偏好"""
        try:
            data = await request.json()
            chosen = data.get("chosen", "")
            options = data.get("options", [])

            if not chosen:
                return web.json_response({"ok": True})

            prompt = (
                f"用户在直播间弹幕互动中，从以下选项中选择了一条回复：\n"
                f"选项：{options}\n"
                f"用户选择：{chosen}\n\n"
                f"请简要分析用户的表达偏好和风格特征（一句话），仅供记忆。"
            )

            asyncio.create_task(self._silent_chat(prompt))
            logger.info("[BiliDanmu] 后台学习已触发")
            return web.json_response({"ok": True})
        except Exception as e:
            logger.error(f"[BiliDanmu] 学习触发失败: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _auto_summary(self):
        """自动触发总结"""
        self._messages_since_summary = 0
        recent = "\n".join(self._danmaku_buffer)
        if not recent:
            return

        prompt = self._build_summary_prompt(recent)

        try:
            resp = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                session_id=f"bilidanmu_{self._current_room_id}",
            )
            summary = resp.result_chain.get_plain_text()
            self._last_summary = summary

            # 回传给 BiliDanmu
            if self._callback_url:
                try:
                    import time
                    async with aiohttp.ClientSession() as session:
                        payload = {
                            "type": "summary",
                            "roomId": self._current_room_id,
                            "message": summary,
                            "timestamp": int(time.time()),
                        }
                        async with session.post(self._callback_url, json=payload) as r:
                            if r.status != 200:
                                logger.error(f"[BiliDanmu] 回传失败: {r.status}")
                except Exception as e:
                    logger.error(f"[BiliDanmu] 回传异常: {e}")
            else:
                logger.warning("[BiliDanmu] callback_url 为空，跳过回传")
        except Exception as e:
            logger.error(f"[BiliDanmu] 自动总结失败: {e}")

    async def _silent_chat(self, prompt: str):
        """静默调用 LLM，仅更新 session 历史"""
        try:
            await self.context.get_using_provider().text_chat(
                prompt=prompt,
                session_id=f"bilidanmu_{self._current_room_id}",
            )
            logger.info("[BiliDanmu] 后台学习完成")
        except Exception as e:
            logger.error(f"[BiliDanmu] 后台学习失败: {e}")

    def _build_summary_prompt(self, recent: str) -> str:
        """构建总结 prompt，包含上一次总结作为上下文"""
        if self._last_summary:
            return (
                f"请对以下直播间弹幕内容进行简短总结。\n\n"
                f"【上一次总结】\n{self._last_summary}\n\n"
                f"【新增弹幕】\n{recent}\n\n"
                f"请结合上一次总结，对新增弹幕进行增量总结。"
            )
        return f"请对以下直播间弹幕内容进行简短总结：\n{recent}"

    def _get_lock(self):
        """切房锁"""
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    async def _do_switch_room(self, new_room_id: int):
        """执行房间切换"""
        # 如果已经在同一个房间，跳过
        if (
            self._current_room_id == new_room_id
            and self.web_client is not None
            and self.web_client.is_running
        ):
            return

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

        self._danmaku_buffer.clear()
        self._messages_since_summary = 0
        self._last_summary = ""

        self.web_client = WebClient(new_room_id, cookie_str=self.cookie_str)
        self.web_client.start()
        self._current_room_id = new_room_id
        self._process_task = asyncio.create_task(self._process_messages())

        logger.info(f"[BiliDanmu] 已切换到直播间 {new_room_id}")

    async def _process_messages(self):
        """获取消息并处理"""
        if self.web_client:
            await asyncio.sleep(2)
            async for message in self.web_client.get_messages():
                try:
                    await asyncio.sleep(0.8)
                    await self._handle_message(message)
                except Exception as e:
                    logger.error(f"[BiliDanmu] 处理消息异常: {e}", exc_info=True)

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
                return

        msg_type = type(message).__name__

        if msg_type == "DanmakuMessage" and "danmaku" in self.allow_message_type:
            # 过滤纯表情弹幕（dm_type=1）
            if self.filter_emoticon_only and getattr(message, "dm_type", 0) == 1:
                return
            self._danmaku_buffer.append(f"{message.user_name}: {message.content}")
            self._messages_since_summary += 1
            # 达到阈值时自动触发总结
            if self._messages_since_summary >= self._auto_summary_threshold:
                asyncio.create_task(self._auto_summary())
        elif msg_type == "GiftMessage" and "gift" in self.allow_message_type:
            self._danmaku_buffer.append(
                f"[礼物] {message.user_name} 赠送了 {message.gift_num}个{message.gift_name}"
            )
        elif msg_type == "SuperChatMessage" and "super_chat" in self.allow_message_type:
            self._danmaku_buffer.append(
                f"[醒目留言] {message.user_name}: {message.message}"
            )
        elif msg_type == "LikeMessage" and "like" in self.allow_message_type:
            self._danmaku_buffer.append(f"[点赞] {message.user_name}")
        elif msg_type == "EnterRoomMessage" and "enter_room" in self.allow_message_type:
            self._danmaku_buffer.append(f"[进场] {message.user_name}")
        elif msg_type == "GuardBuyMessage" and "guard_buy" in self.allow_message_type:
            guard_level_names = {1: "总督", 2: "提督", 3: "舰长"}
            guard_level_name = guard_level_names.get(
                getattr(message, "guard_level", 0), "未知"
            )
            self._danmaku_buffer.append(
                f"[上舰] {message.user_name} 成为了{guard_level_name}"
            )

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
