# astrbot_plugin_bilibili_live

> [!note]
> 本项目所使用的blivedm经过二次开发，如有问题请勿直接向原作者提交issue。
>
> 此分支专为 [BiliDanmu](https://github.com/nichuanfang/bilidanmu) 优化，已移除开放平台接入和自动 LLM 回调，简化配置。

## 简介
AstrBot B站直播插件，用于接入 Bilibili 直播，接收弹幕并缓存，供 BiliDanmu 手动触发 AI 回复/总结。

## 功能特性
- Web 接入 B 站直播
- 接收多种直播间消息类型并缓存：
    - 实时弹幕 (danmaku)
    - 礼物赠送 (gift)
    - 醒目留言 (super_chat)
    - 点赞 (like)
    - 进场 (enter_room)
    - 上舰通知 (guard_buy)
- 可配置过滤纯表情弹幕
- 可配置缓冲区大小
- HTTP API 支持动态切换直播间和手动触发 AI

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 返回当前房间号、运行状态、端口号 |
| `/api/switch-room` | POST | 切换直播间 `{"room_id": 12345}` |
| `/api/trigger` | POST | 手动触发 AI `{"action":"reply"|"summary"}` |

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| B站web接入 — 是否启用 | - | 开启以连接直播间 |
| B站web接入 — cookie | - | B站 Cookie（需包含 SESSDATA） |
| 缓存的消息类型 | `danmaku, super_chat` | 逗号分隔，可选：danmaku/gift/super_chat/like/enter_room/guard_buy |
| 过滤纯表情弹幕 | `true` | 过滤整条消息就是一个表情的弹幕 |
| 弹幕缓冲区大小 | `500` | 缓冲区最大条数 |
| HTTP API 端口 | `0` | 供 BiliDanmu 调用的端口，0=随机 |
| 随机丢弃 — 是否启用 | `false` | 开启后按概率丢弃消息以降低噪音 |
| 随机丢弃 — 丢弃概率 | `0` | 0.0~1.0 |
