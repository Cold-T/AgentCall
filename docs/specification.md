# 项目规格

2026-10-06 补充：在保持 HTTP API 与 CLI 的基础上，增加用户要求的最简网页，先输入固定 PIN 再进入操作界面。网页覆盖现有业务操作与可配置项，凭据只写不读，后台继续独立运行。详见 [网页说明](web-ui.md)。

## 1. 模块职责

| 模块 | 职责 |
| --- | --- |
| 蓝牙适配 | 配对、连接、断开、自动重连；封装 handsfree-linux 蓝牙功能 |
| 通话控制 | HFP 拨号、接听、挂断、DTMF；跟踪实际通话状态 |
| PBAP 同步 | 联系人、手机允许访问的最近通话与历史记录 |
| 音频桥接 | SCO 与模型音频的编解码、重采样、格式转换和持续双向传输 |
| RTS provider | 统一两家 API 的会话配置、消息、音频事件和工具调用 |
| 任务管理 | 保存、排队执行、关联通话、记录进度与结果 |
| 数据管理 | SQLite 存储与查询 |
| HTTP API | 全部业务控制与查询功能 |
| CLI | HTTP API 客户端，支持易读输出与 `--json` |

## 2. 手机和通话

- Android 和 iPhone 分别进行真机验证，完整验收统一安排在 CP6（稳定性与真机验收）；CP2–CP5 以对应软件实现与自动验证为完成条件。
- 通过号码或联系人发起手动通话与 AI 任务通话。
- 支持接听、挂断、DTMF 和当前通话状态查询。
- 首次配对与 PBAP 权限允许在手机端确认。
- 第一版每部手机同时一通电话，其他任务排队。
- 第一版使用手机当前默认拨号 SIM，不承诺 HFP 双卡线路选择。
- 蓝牙自动重连不自动重新拨号，避免重复呼叫。
- PBAP 不可用或权限未授予时仍允许直接号码拨打。

## 3. 音频桥接与插话

- 对方音频持续输入模型，模型说话期间也保持输入。
- 模型音频收到后尽快转换并发送到 SCO。
- 不主动累积固定时长 PCM 块；必要分帧由编码与 API 要求决定。
- 仅保留编解码、重采样与传输需要的缓冲。
- 插话检测、轮次判断和停止生成交给 RTS API。
- 不实现独立 VAD；遵循模型 API 的插话事件停止本机播放、清除未发送音频，并在 OpenAI WebSocket 中同步截断未播放上下文。
- 接受不同长度的 API 音频块，不假定固定块时长。
- 记录输入格式、输出格式与待发送音频时长，用于排查真机音频问题。
- 转写用于展示与记录，不作为语音对话必须经过的处理步骤。

## 4. 统一 RTS provider

第一版实现 OpenAI Realtime 和 Gemini Live。

统一接口覆盖建立会话（加载配置、任务上下文与工具）、持续发送音频、接收事件（音频、转写、工具调用、错误、会话结束）、更新上下文、返回工具结果和关闭会话。

配置包括 provider、model、voice、language、provider 专属参数以及 API 密钥环境变量。服务读取凭据，不向客户端返回 API 密钥。默认配置可由单个任务覆盖。模型切换只在新通话生效；不做通话中切换，也不在失败时自动切换 provider。

## 5. 任务和模型工具

每个任务包含目标号码或联系人、任务目标与背景、可用资料、完成条件与返回字段、语言 / provider / 模型 / 声音、最长通话时间，以及立即执行或保存后手动启动的选项。

| 工具 | 行为 |
| --- | --- |
| `send_dtmf(digits)` | 通过 HFP 发送数字、`*`、`#` |
| `finish_task(status, result)` | 提交结构化任务结果 |
| `hangup(reason)` | 请求挂断；正常情况下等待已有结束语发送完毕 |

控制程序校验工具参数，并根据工具调用 ID 防止重复执行。模型报告任务完成，不直接决定手机是否已经接通或挂断。

## 6. 执行流程和异常处理

1. 保存任务并解析联系人号码。
2. 检查手机连接、可用状态和 provider 配置。
3. 建立模型会话并加载任务上下文。
4. 通过 HFP 拨号。
5. 通话接通且音频链路就绪后启动模型对话。
6. 持续桥接音频，执行模型工具。
7. 通话结束后关闭模型会话，保存通话记录和任务结果。

任务状态：待执行 → 准备中 → 拨号中 → 通话中 → 整理结果 → 结束。

结束结果必须区分：完成、部分完成、未完成、未接、忙线、拨号失败、用户取消、超时、蓝牙断开、模型连接失败、模型连接断开。

手机通话状态和任务完成状态分别保存。通话结束不代表任务成功；异常也不应被模型摘要覆盖。

## 7. SQLite 数据

| 数据 | 内容 |
| --- | --- |
| 手机 | 设备标识、连接信息、可用能力 |
| 联系人 | 姓名、号码、所属手机、同步时间 |
| 手机历史 | PBAP 实际返回记录及来源 |
| 项目通话 | 号码、方向、开始时间、时长、结束原因 |
| 任务 | 输入内容、配置、执行状态 |
| 执行结果 | 摘要、结构化信息、错误、关联通话 |
| 对话与工具事件 | provider 可提供的转写、工具调用与执行结果 |

手机历史和项目记录分别保留来源，查询时可合并展示。

## 8. HTTP API 和 CLI

以下是目标接口，当前尚未实现。

| 操作 | HTTP API | CLI |
| --- | --- | --- |
| 手机列表 | `GET /devices` | `phone devices` |
| 配对、连接、断开 | `POST /devices/{id}/...` | `phone pair/connect/disconnect DEVICE` |
| 联系人与手机历史同步 | `POST /devices/{id}/sync` | `phone sync DEVICE` |
| 搜索联系人 | `GET /contacts?q=...` | `phone contacts QUERY` |
| 查看历史 | `GET /calls` | `phone calls` |
| 手动拨号 | `POST /calls` | `phone dial NUMBER` |
| 接听 | `POST /calls/{id}/answer` | `phone answer CALL_ID` |
| 查询当前通话 | `GET /calls/current` | `phone calls --current` |
| 创建任务 | `POST /tasks` | `phone task create --file task.json` |
| 启动任务 | `POST /tasks/{id}/start` | `phone task start ID` |
| 查看任务与结果 | `GET /tasks/{id}` | `phone task show ID` |
| 取消任务 | `POST /tasks/{id}/cancel` | `phone task cancel ID` |
| DTMF | `POST /calls/{id}/dtmf` | `phone dtmf CALL_ID DIGITS` |
| 挂断 | `POST /calls/{id}/hangup` | `phone hangup CALL_ID` |
| 实时事件 | `GET /events`（SSE） | `phone watch` |

创建和启动立即返回 ID，通过查询或 SSE 查看进度。启动支持幂等键，重试不会重复拨号。远程访问支持简单 Bearer token。API 密钥由 Linux 服务读取，不返回给客户端。

## 9. 运行和交付

Python + FastAPI + Typer + SQLite。移除 GUI 依赖；若蓝牙代码依赖 GLib 主循环，通过独立线程或事件循环接入服务。通过 systemd 用户服务常驻运行，提供示例配置和安装说明。

最终交付：源代码、安装与运行说明、配置示例、CLI 帮助、API 文档，以及 Android / iPhone 实际验收结果。
