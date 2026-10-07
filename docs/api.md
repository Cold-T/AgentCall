# HTTP API

运行服务后，`/docs` 提供交互式 OpenAPI 文档，`/openapi.json` 提供 schema。配置 token 时，业务 API、文档和音频 WebSocket 要求 Bearer / PIN 认证或有效网页会话。`/ui`、白名单静态资源和 `/session/login` 是公开入口，未登录只显示 PIN 表单。

| 操作 | 接口 | 请求 / 说明 |
| --- | --- | --- |
| 就绪状态 | `GET /health` | ready / bluetooth_error |
| 发现设备 | `POST /discovery/start`、`POST /discovery/stop` | 显式扫描控制 |
| 手机列表 | `GET /devices` | BlueZ 连接、HFP 就绪、通话状态、音频、重连意图 |
| 取消配对 | `POST /devices/{device}/unpair` | 移除指定设备的蓝牙配对记录，清除自动重连；保留通话记录；进行中的通话或任务返回 409 |
| 配对 | `POST /devices/{device}/pair` | 数值确认通过 SSE + confirmation 接口 |
| 允许手机主动配对 | `POST /discoverability/start`、`POST /discoverability/stop` | 开启 180 秒可发现、可配对窗口；使用 AgentCall 确认代理；停止禁止新的主动配对 |
| 配对待确认 | `GET /pairing` | `pending_ids` 及 `requests`（设备、配对码）；`incoming_pairing_enabled` 表示主动配对窗口是否有效 |
| 配对确认 | `POST /pairing/{request_id}` | `{"accept":true}`；用户需先核对手机数值 |
| 连接 / 断开 | `POST /devices/{device}/connect`、`.../disconnect` | 连接启用持久化重连意图；手动断开清除意图 |
| PBAP 同步 | `POST /devices/{device}/sync` | 联系人数 / 历史数 / 各 phonebook 错误 |
| 联系人查询 | `GET /contacts?q=...&device=...` | 包含所属手机与原始 vCard |
| 拨号 | `POST /calls` | `{"device":"AA:BB:CC:DD:EE:FF","number":"+123"}` 或以 `contact_id` 替代 number |
| 项目记录 + 手机历史 | `GET /calls` | source 分别为 project / pbap |
| 合并历史 | `GET /history` | 按设备、limit / offset 分页，按时间倒序合并任务、项目通话和手机历史，关联通话只展示一次 |
| 当前通话 | `GET /calls/current` | 以项目 call ID 操作 |
| 通话详情 | `GET /calls/{id}` | 实际状态、开始 / 接通 / 结束时间、原因、音频指标 |
| 接听 / 挂断 | `POST /calls/{id}/answer`、`.../hangup` | accepted 表示 AT 成功，实际状态仍由手机指示器确认 |
| DTMF | `POST /calls/{id}/dtmf` | `{"digits":"12*#"}`；仅在 active 状态允许 |
| 实时事件 | `GET /events` | SSE，kind、device、时间、项目 call_id 等 |
| 音频 | `WS /calls/{id}/audio` | 首条 JSON 为格式和指标，随后二进制 s16le mono PCM 双向流 |

`GET /devices` 只读取 BlueZ 已知设备，不会启动搜索。搜索新手机时先调用 `POST /discovery/start`，每 2 秒读取设备列表，最多等待 30 秒（设备出现后可提前结束），最后调用 `POST /discovery/stop`。网页和 API 共用扫描状态；停止会同时停止网页扫描。手机需打开蓝牙并保持可发现状态。扫描未发现设备时不要把历史列表当作扫描结果。

device 支持 MAC 或 `dev_AA_BB_CC_DD_EE_FF`；API 路径用其中一种，不嵌套完整 D-Bus path。SSE 和记录里的 device 使用完整 D-Bus path。

号码严格允许可选前缀 `+`、数字、`*`、`#`，最多 64 个号码字符。联系人号码仅移除空格、括号、点和连字符，再做同样校验。DTMF 允许 1–64 个数字、`*`、`#`。

同一手机的拨号被串行保护：已存在请求中 / 进行中的通话时返回 409，不发送第二个 ATD。任务执行期间也保留手机占用，手动拨号返回 409；已启动任务按手机排队。

错误：400 参数 / 未知 ID，401 认证失败，409 手机状态或 AT 命令拒绝，422 schema 校验失败，503 蓝牙 / PBAP 不可用，504 超时。AT 响应超时会关闭该 HFP 连接，避免迟到 OK 被误当成下一个命令的回复；连接重建不会重拨。

音频 WebSocket 每个 SCO 链路只允许一个客户端。必须使用服务报告的采样率，不接受独立格式声明或隐式重采样；每条 PCM 消息最多 256 KiB，必须为完整 16-bit 样本。输入与输出独立运行。WebSocket 退出不挂断通话。

SSE 提供实时增量事件，没有断点重放。慢客户端超过有界事件队列时收到 `events.overflow` 并结束，应重新连接并查询当前状态。后台将事件持久化到 SQLite，可通过 GET /events/history 查询。


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

## CP5 查询与分页

新增接口均使用相同 Bearer 认证，OpenAPI 在 API 操作中声明 BearerAuth，便于携带 token 调用。所有业务先提供 HTTP API，CLI 映射见 [完整 CLI 帮助](cli.md)。

| 接口 | 查询与响应 |
| --- | --- |
| `GET /devices/saved` | SQLite 设备标识、last_observed 快照、updated_at；不代表目前仍然连接 |
| `GET /devices/{device}` | 能查询到当前设备时 live=true；否则返回已存快照 live=false |
| `GET /contacts/{contact_id}` | 姓名、号码、所属设备、同步时间、原始 vCard |
| `GET /contacts` | q、device、limit、offset |
| `GET /calls` | device、source=project / pbap、limit、offset；省略 source 合并展示 |
| `GET /calls/current` | 可带 device 筛选当前通话 |
| `GET /calls/{id}` | 可带 source 指定项目 / PBAP；历史 PBAP 不附加当前通话音频 |
| `GET /tasks` | state、device、outcome、limit、offset |
| `GET /tasks/{id}/result` | id、state、outcome、model_result、error、call_id、call、ended_at |
| `GET /tasks/{id}/tools` | limit、offset；模型工具 call_id、name、原始 arguments JSON、result、state |
| `GET /tasks/{id}/events` | after_id、kind、limit；递增 id、time、kind、data |
| `GET /events/history` | after_id、limit、kind、device、call_id、task_id；按持久 event_id 递增 |
| `GET /events` | 同样支持 kind、device、call_id、task_id 筛选，始终发送 ready / overflow 控制事件 |

列表保持数组响应，默认 limit=100，允许 1–1000，offset ≥ 0；分页结束为空数组。事件 after_id ≥ 0，为排他游标，取上一页最后一项的游标继续查询。非法 limit / offset / source / task state 返回 422，未知业务 ID 返回 400。

项目 duration_seconds 根据手机确认接通时间到记录结束时间计算；活动通话计算到查询时刻，未确认接通为 null。service_restart / unknown 的记录截止时间不证明手机已挂断。PBAP 不推断缺失时长；保留手机原始时间（可能没有时区）及 vCard。合并列表按项目 started_at / PBAP synced_at 倒序，不将手机未带时区的通话时间当 UTC 排序。记录不跨来源去重，source 始终保留。

全局事件新增整数 event_id，避免配对确认的 id（请求 UUID）覆盖数据库游标。历史与实时的 event_id 一致；原有配对 id 继续用于 confirmation 接口。任务工具事件中的 tool_call_id 是模型调用 ID，call_id 是实际项目电话 ID；工具查询列表的 call_id 仍是原始工具去重 ID。

SQLite 增量建表并从旧联系人 / 通话 / 重连意图 / 任务补齐设备标识，保留已有记录。快照包含观测到的 HFP 能力、连接与音频信息以及最近 PBAP 同步结果；设备不可用时只返回明确标记的历史数据，不推断当前能力。PBAP 无权限仍保留已有联系人，直接拨号不受影响。

```bash
curl -H "Authorization: Bearer $AGENTCALL_TOKEN" \
  'http://127.0.0.1:8765/calls?source=project&limit=20'
curl -H "Authorization: Bearer $AGENTCALL_TOKEN" \
  'http://127.0.0.1:8765/events/history?after_id=0&limit=100'
```

401 返回 WWW-Authenticate: Bearer（PIN 模式为 Basic）；业务 HTTP、SSE、OpenAPI / docs 和音频 WebSocket 均受认证保护。OpenAI / Gemini 凭据不返回客户端。认证后可读写非秘密服务配置，见 [网页设置与会话 API](web-ui.md)。真实 API 和真机兼容性验收边界见 checkpoint 验证记录。

API 创建任务无需填写 `background`；省略时使用后台默认通话说明，显式填写时使用该内容替换说明，空字符串表示清空。网页预填写同一默认说明，允许修改；修改只影响本次通话。PIN 即 API token，Bearer / Basic 客户端不需要先登录或换取其他 token。

## 下载通话资料

`GET /tasks/{task_id}` 新增 `downloads.transcript` / `downloads.recording` 可用性标记。以下下载与其他 API 使用相同的 PIN/Bearer/网页登录认证，并禁止缓存：

- `GET /tasks/{task_id}/transcript`：UTF-8 TXT 附件，包含目标、时间、发言人及完整分页转写；合并同一项目的连续 delta。没有转写返回 404。被打断的完整文本可能包含未播放内容。
- `GET /tasks/{task_id}/recording`：双声道 WAV 附件，左声道为对方，右声道为实际送入 SCO 的 AI 语音。任务尚未结束返回 409；没有录音返回 404。

新 AI 通话自动录音。文件以任务 UUID 命名，存放在 SQLite 数据库同级的 `recordings/`，目录默认权限 0700，录音 0600；只通过受认证的下载路由提供。使用 `:memory:` 数据库时不持久录音。旧通话、手机同步历史和未接通任务没有可追溯生成的录音。录音保留到手动删除，不自动轮换；双声道 16-bit PCM 约占每分钟 1.92 MB（CVSD）或 3.84 MB（mSBC）。录音写入失败会舍弃该录音并记录 `task.recording_failed`，通话继续运行。

## 结果总结

`POST /tasks/{task_id}/summary` 使用指定的 `gpt-5.6-luna`（[官方模型文档](https://developers.openai.com/api/docs/models/gpt-5.6-luna)）与 OpenAI Responses API，总结已结束任务的目标、完成条件、结构化结果、转写及实际电话状态。复用服务的 OpenAI API Key，即使通话使用 Gemini，总结仍由 GPT-5.6 Luna 完成。未结束任务返回 400；所有调用均受现有认证及网页 CSRF 保护。

响应包含 `model`、`status`（generating / completed / failed）、`text`、`error`、`updated_at`。成功或失败结果均保存在数据库，`GET /tasks/{task_id}` 的 `summary` 返回缓存，不触发模型调用。成功缓存不重复生成，并发请求合并为一次；失败后用 `POST /tasks/{task_id}/summary?retry=true` 显式重试。失败不会改变原始任务结果或通话状态，也不会自动替换为其他模型。接口错误信息不会包含 OpenAI 原始响应或凭据。

请求 `store: false`，不启用工具。输入超过 500,000 字符时拒绝总结，不静默删减记录。服务关闭时取消未完成请求并保留可重试失败状态。总结使用模型 API，会产生相应用量；模型权限以当前账户为准。

`service.default_max_call_seconds` 为持久化、无需重启即可应用的默认最长通话秒数（初始 300，范围大于 0 且不超过 3600）。通过 `PUT /settings` 保存。创建任务省略 `max_call_seconds` 时使用该值；显式参数优先。网页选择的模型和声音也保存到默认 `provider`，API 省略 `config` 时共同继承。

## 浏览器认证与跨站保护

使用网页会话 cookie 或 HTTP Basic 的写请求（POST / PUT / PATCH / DELETE）必须携带 `X-AgentCall-CSRF: 1`；提供 `Origin` 时必须与服务同源。缺少标记或跨站请求返回 403。Cookie / Basic 音频 WebSocket 必须携带同源 `Origin`。显式 Bearer 客户端不需要 CSRF 标记；CLI 和自动化客户端推荐使用 Bearer，Swagger 的写操作也可选择 Bearer。
