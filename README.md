# astrbot_plugin_bilibili_live

> [!note]
> 本项目所使用的blivedm经过二次开发，如有问题请勿直接向原作者提交issue。
>
> 此分支专为 [BiliDanmu](https://github.com/nichuanfang/bilidanmu) 优化，已移除开放平台接入和多工作模式，简化配置。

## 简介
AstrBot B站直播插件，用于接入 Bilibili 直播，接收弹幕、礼物、醒目留言、点赞、进场和上舰消息，通过 LLM 处理后回调到 BiliDanmu。

## 功能特性
- Web 接入 B 站直播
- 接收多种直播间消息类型：
    - 实时弹幕 (danmaku)
    - 礼物赠送 (gift)
    - 醒目留言 (super_chat)
    - 点赞 (like)
    - 进场 (enter_room)
    - 上舰通知 (guard_buy)
- LLM 聊天并回调模式（自动将消息发送给 LLM 处理后通过回调接口发送）
- 支持上下文记录
- 支持随机丢弃消息
- **HTTP API**：支持动态切换直播间和手动触发 AI 回复/总结

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 返回当前房间号、运行状态、端口号 |
| `/api/switch-room` | POST | 切换直播间 `{"room_id": 12345}` |
| `/api/trigger` | POST | 手动触发 AI `{"action":"reply","context":"弹幕..."}` |

## 回调格式

```json
{
    "room_id": 4284205,
    "sender": "发送者ID",
    "sender_name": "发送者昵称",
    "message": "消息文本"
}
```

## 配置说明

| 配置项 | 说明 |
|--------|------|
| B站web接入 — 是否启用 | 开启以连接直播间 |
| B站web接入 — 直播间ID | 初始直播间号（之后由 BiliDanmu 动态切换） |
| B站web接入 — cookie | B站 Cookie（需包含 SESSDATA） |
| 允许的消息类型 | 以逗号分隔，如 `danmaku, gift, super_chat` |
| LLM 聊天最大上下文长度 | 上下文记录条数（×2），默认 15 |
| LLM 聊天回调 — 回调地址 | BiliDanmu 的回调地址，如 `http://127.0.0.1:12345/astrbot/callback` |
| LLM 聊天回调 — 回调方法 | POST 或 GET |
| HTTP API 端口 | 供 BiliDanmu 调用的端口，0=随机 |
| 随机丢弃 — 是否启用 | 开启后按概率丢弃消息以降低噪音 |
| 随机丢弃 — 丢弃概率 | 0.0~1.0 |
