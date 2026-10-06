# CLI 帮助与操作

`phone` 是 HTTP 客户端，Linux 后台服务独立持有手机和任务。默认 URL 为 `http://127.0.0.1:8765`，可用 `--url` 或 `AGENTCALL_URL` 设置。`AGENTCALL_TOKEN` 是客户端 Bearer token；OpenAI / Gemini 密钥仅由服务读取。

默认列表显示表格，详情显示格式化 JSON。`phone --json COMMAND` 的成功结果在 stdout 为单个 JSON 对象 / 数组；`watch` 是每行一个 JSON 对象。HTTP / 网络 / 文件 JSON 读取错误为退出码 1，`--json` 的错误写入 stderr：`{"error": ..., "status": 401}`，无 HTTP 状态时 status 为 null。命令用法错误由 Typer 显示帮助并返回 2；Ctrl-C 退出 watch / audio 返回 0。

所有命令支持 `--help`，包括 `phone task --help` 和 `phone task create --help`。

## 手机与通话

| 命令 | 行为 |
| --- | --- |
| `phone health` | 查询后台及蓝牙就绪状态 |
| `phone devices` | 查询 BlueZ 当前设备、HFP 和音频状态 |
| `phone devices --saved` | 查询 SQLite 上次观测的设备快照，蓝牙不可用时仍能使用 |
| `phone device DEVICE` | 单个设备详情，live 区分当前状态与历史快照 |
| `phone scan [start\|stop]` | 扫描手机，默认 start |
| `phone pair DEVICE` | 请求配对；手机端可确认首次配对 |
| `phone pairing` | 查看待确认请求 ID |
| `phone confirm REQUEST_ID [--reject]` | 核对手机 passkey 后确认，或拒绝 |
| `phone connect DEVICE` | 连接并启用自动蓝牙重连，不重新拨号 |
| `phone disconnect DEVICE` | 断开并取消自动重连 |
| `phone sync DEVICE` | PBAP 联系人及手机实际可提供的历史同步 |
| `phone contacts [QUERY] [--device DEVICE] [--limit N] [--offset N]` | 按名字 / 号码搜索联系人 |
| `phone contact CONTACT_ID` | 联系人、号码、所属手机、原始 vCard 与同步时间 |
| `phone dial NUMBER --device DEVICE` | 手动拨打号码 |
| `phone dial --contact CONTACT_ID --device DEVICE` | 通过联系人拨号 |
| `phone calls [--device DEVICE] [--source project\|pbap] [--limit N] [--offset N]` | 项目与手机历史记录，保留来源 |
| `phone calls --current [--device DEVICE]` | 当前实际通话 |
| `phone call CALL_ID [--source project\|pbap]` | 项目通话或手机历史详情 |
| `phone answer CALL_ID` | 接听来电 |
| `phone dtmf CALL_ID '12*#'` | 活动通话发送 DTMF |
| `phone hangup CALL_ID` | 请求挂断，随后查询实际状态 |
| `phone audio CALL_ID --live --seconds 60` | 使用 pacat 进行手动双向音频验收 |
| `phone audio CALL_ID --input send.pcm --output received.pcm --seconds 30` | 原生 PCM 文件探针 |

DEVICE 使用 MAC 或 dev_XX_XX_XX_XX_XX_XX。PBAP 历史不能用于接听 / DTMF / 挂断；这些操作使用当前项目 call ID。`calls --current` 只接受 device 筛选。手动音频独占当前 SCO，AI 任务占用时不能附加；音频格式通知写 stderr，PCM 写文件 / pacat，不写普通命令的 stdout。

## 任务与事件

| 命令 | 行为 |
| --- | --- |
| `phone task create --file task.json` | 保存任务并返回 ID；JSON 可设置 start_immediately |
| `phone task start TASK_ID [--key REQUEST_KEY]` | 异步排队；重复 key / 重复启动不重复拨号 |
| `phone task list [--state STATE] [--device DEVICE] [--outcome OUTCOME] [--limit N] [--offset N]` | 筛选任务 |
| `phone task show TASK_ID` | 输入、有效配置、进度、结果、错误与关联通话 |
| `phone task result TASK_ID` | 结构化结果、独立结束原因和实际手机状态 |
| `phone task tools TASK_ID [--limit N] [--offset N]` | 持久去重的工具 ID、参数、执行状态及结果 |
| `phone task events TASK_ID [--after ID] [--kind KIND] [--limit N]` | 持久任务 / 转写 / 工具事件 |
| `phone task context TASK_ID '补充资料'` | 给活动对话补充上下文 |
| `phone task cancel TASK_ID` | 取消排队，或清理活动任务并请求挂断 |
| `phone watch [--device DEVICE] [--task TASK_ID] [--call CALL_ID] [--kind KIND]` | 实时 SSE，可同时筛选 |
| `phone events [--after EVENT_ID] [--limit N] [--device DEVICE] [--task TASK_ID] [--call CALL_ID] [--kind KIND]` | 查询全局持久事件，包括错过的实时事件 |

列表默认 limit=100，范围 1–1000；offset ≥ 0。事件游标以 after 指定，不使用 offset。全局事件使用 event_id，任务事件使用独立的 id；不能混用。SSE 不自动重放，可先查 events 补齐，再连接 watch；切换期间如果需要避免漏读，再用持久游标查询并去重。

任务 JSON 示例为 `examples/task.json` / `examples/task.gemini.json`，provider 配置见 [OpenAI 任务说明](tasks.md) 和 [Gemini 说明](gemini.md)。客户端退出后任务继续；取消任务必须显式调用 cancel。
