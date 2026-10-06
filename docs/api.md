# 蓝牙阶段 HTTP API

运行服务后，`/docs` 提供交互式 OpenAPI 文档，`/openapi.json` 提供 schema。若配置 Bearer token，所有 HTTP 路径（包括文档）和音频 WebSocket 都要求 Authorization header。

| 操作 | 接口 | 请求 / 说明 |
| --- | --- | --- |
| 就绪状态 | `GET /health` | ready / bluetooth_error |
| 发现设备 | `POST /discovery/start`、`POST /discovery/stop` | 显式扫描控制 |
| 手机列表 | `GET /devices` | BlueZ 连接、HFP 就绪、通话状态、音频、重连意图 |
| 配对 | `POST /devices/{device}/pair` | 数值确认通过 SSE + confirmation 接口 |
| 配对待确认 | `GET /pairing` | 待确认请求 ID |
| 配对确认 | `POST /pairing/{request_id}` | `{"accept":true}`；用户需先核对手机数值 |
| 连接 / 断开 | `POST /devices/{device}/connect`、`.../disconnect` | 连接启用持久化重连意图；手动断开清除意图 |
| PBAP 同步 | `POST /devices/{device}/sync` | 联系人数 / 历史数 / 各 phonebook 错误 |
| 联系人查询 | `GET /contacts?q=...&device=...` | 包含所属手机与原始 vCard |
| 拨号 | `POST /calls` | `{"device":"AA:BB:CC:DD:EE:FF","number":"+123"}` 或以 `contact_id` 替代 number |
| 项目记录 + 手机历史 | `GET /calls` | source 分别为 project / pbap |
| 当前通话 | `GET /calls/current` | 以项目 call ID 操作 |
| 通话详情 | `GET /calls/{id}` | 实际状态、开始 / 接通 / 结束时间、原因、音频指标 |
| 接听 / 挂断 | `POST /calls/{id}/answer`、`.../hangup` | accepted 表示 AT 成功，实际状态仍由手机指示器确认 |
| DTMF | `POST /calls/{id}/dtmf` | `{"digits":"12*#"}`；仅在 active 状态允许 |
| 实时事件 | `GET /events` | SSE，kind、device、时间、项目 call_id 等 |
| 音频 | `WS /calls/{id}/audio` | 首条 JSON 为格式和指标，随后二进制 s16le mono PCM 双向流 |

device 支持 MAC 或 `dev_AA_BB_CC_DD_EE_FF`；API 路径用其中一种，不嵌套完整 D-Bus path。SSE 和记录里的 device 使用完整 D-Bus path。

号码严格允许可选前缀 `+`、数字、`*`、`#`，最多 64 个号码字符。联系人号码仅移除空格、括号、点和连字符，再做同样校验。DTMF 允许 1–64 个数字、`*`、`#`。

同一手机的拨号被串行保护：已存在请求中 / 进行中的通话时返回 409，不发送第二个 ATD。当前阶段没有任务启动、排队和 Idempotency-Key 接口，它们在后续任务模块实现。

错误：400 参数 / 未知 ID，401 认证失败，409 手机状态或 AT 命令拒绝，422 schema 校验失败，503 蓝牙 / PBAP 不可用，504 超时。AT 响应超时会关闭该 HFP 连接，避免迟到 OK 被误当成下一个命令的回复；连接重建不会重拨。

音频 WebSocket 每个 SCO 链路只允许一个客户端。必须使用服务报告的采样率，不接受独立格式声明或隐式重采样；每条 PCM 消息最多 256 KiB，必须为完整 16-bit 样本。输入与输出独立运行。WebSocket 退出不挂断通话。

SSE 提供实时增量事件，没有断点重放。慢客户端超过有界事件队列时收到 `events.overflow` 并结束，应重新连接并查询当前状态。后台还会将事件持久化到 SQLite；完整事件查询属于后续 API checkpoint。
