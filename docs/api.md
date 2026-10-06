# HTTP API

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

同一手机的拨号被串行保护：已存在请求中 / 进行中的通话时返回 409，不发送第二个 ATD。任务执行期间也保留手机占用，手动拨号返回 409；已启动任务按手机排队。

错误：400 参数 / 未知 ID，401 认证失败，409 手机状态或 AT 命令拒绝，422 schema 校验失败，503 蓝牙 / PBAP 不可用，504 超时。AT 响应超时会关闭该 HFP 连接，避免迟到 OK 被误当成下一个命令的回复；连接重建不会重拨。

音频 WebSocket 每个 SCO 链路只允许一个客户端。必须使用服务报告的采样率，不接受独立格式声明或隐式重采样；每条 PCM 消息最多 256 KiB，必须为完整 16-bit 样本。输入与输出独立运行。WebSocket 退出不挂断通话。

SSE 提供实时增量事件，没有断点重放。慢客户端超过有界事件队列时收到 `events.overflow` 并结束，应重新连接并查询当前状态。后台还会将事件持久化到 SQLite；完整事件查询属于后续 API checkpoint。


## AI 任务

| 操作 | 接口 | 请求 / 说明 |
| --- | --- | --- |
| 保存任务 | `POST /tasks` | 201 返回完整记录和 ID，格式见 examples/task.json；可立即排队 |
| 任务列表 / 详情 | `GET /tasks`、`GET /tasks/{id}` | 输入、有效配置、状态、结果、错误、关联实际通话；活动任务含音频指标 |
| 启动 | `POST /tasks/{id}/start` | 202 立即返回；可带 `Idempotency-Key`；重复启动不重复拨号 |
| 取消 | `POST /tasks/{id}/cancel` | 202；排队直接结束，运行中异步清理和挂断 |
| 持久事件 | `GET /tasks/{id}/events` | 按顺序返回转写、工具、任务状态与错误事件 |
| 补充上下文 | `POST /tasks/{id}/context` | `{"text":"补充资料"}`，仅活动对话允许 |

状态为 saved → queued → preparing → dialing → in_call → finalizing → ended。`outcome` 是执行结束原因，`model_result.status` 是模型报告结果，`call.state` 是手机实际状态；三者分别保存。任务 / 工具事件也通过现有 SSE 发布。密钥仅由服务环境读取，任务不接受凭据或自定义模型端点。任务结果 schema 仅允许本地片段引用。详见 [任务说明](tasks.md)。

`config.provider` 支持 `openai` / `gemini`，两家共用上述任务接口。模型、声音、专属 options 与凭据选择规则见 [Gemini 文档](gemini.md)。转写事件中 Gemini 的 `delta=true` 表示文本分片；按事件顺序展示即可，不用于替代音频输入。
