# GPT-Live 通话

选择 OpenAI 的 `gpt-live-1` 模型后，服务自动采用 GPT-Live 专用通话流程；OpenAI Realtime 与 Gemini Live 保留原有流程。无需额外打开打断、后台推理或告别模式，也不提供这些实现细节的用户开关。

**已进行获授权的真实模型、本地模拟及小米手机测试，但完整通话体验验收尚未全部通过；1.1.2 自动收尾修复尚未进行新增真机验证。** 使用前需要账户具备相关模型权限。真实模型请求、语音合成和电话通话可能产生费用，必须获得相应授权后再测试。

协议参考：[Live delegation](https://developers.openai.com/api/docs/guides/live-delegation)、[Live conversations](https://developers.openai.com/api/docs/guides/live-conversations)、[语音 WebSocket](https://developers.openai.com/api/docs/guides/voice-websockets) 与 [语音合成](https://developers.openai.com/api/docs/guides/text-to-speech)。具体消息和播放策略以当前实现及验证记录为准。

## 对话与任务分工

GPT-Live 使用连续音频会话和 client delegation，负责听说、自然让话与对话表达。它的提示词描述语言、AI 身份、表达方式和让话规则。任务目标、背景资料、完成条件、结构化结果要求及工具权限放在独立后台提示词中。

后台使用 Responses API 的 `gpt-6-luna`（`reasoning.effort=low`）推理并请求工具。接通并启动会话后主动准备任务首问，后续按明确的答案、纠正、工具和结束通话委派条件继续工作。应用校验工具参数和请求版本，串行执行工具，记录执行结果，再把当前版本的回答交给 Live 表达。停止说话本身不会取消正在执行的任务。

收到新的用户转写或任务上下文时，请求版本递增。旧版本的后续工具请求不再执行，迟到的推理答案不再直接发送给 Live；后台结合后续转写、Live 委派或显式任务上下文更新，再按最新意图与操作记录推理；IVR 完整性由 Luna 判断。已执行的操作保留其结果，不能因插话而声称回滚，也不能因为答案迟到而自动重做。真实副作用不会自动重试。

## 连续音频和播放延迟

Live 的输入和输出使用单声道、24 kHz、16-bit PCM；音频桥与手机 SCO 的 8/16 kHz PCM 流式重采样。正常对话由 Live 判断自然让话，不依赖旧 Realtime 的轮次完成事件、item 截断或本地关键词/VAD 打断。

应用保留本地立即停止输出的能力，用于任务结束、通话断开和撤销告别。连续输出不累计每个 response/item 的播放记录。SCO 按独立的稳定发送时间轴调度数据包，避免定时器唤醒误差逐包累积；实际播放完成时间另行保守跟踪。总播放积压上限为 2 秒，超限明确失败，不静默丢弃中间音频。`task.audio` 事件包括 `playback_latency_ms`、`playback_high_water_ms` 与 `max_playback_ms`，用于观察队列、重采样、传输和待播放音频造成的延迟。

手机/蓝牙链路的回声表现仍须真机验证；离线播放测试不能证明声学回声消除或附和识别效果。尤其告别 TTS 由本地播放，并非 Live 自身生成的输出；当前保守地把告别期间任何用户转写都视为撤销信号，因此必须检查告别回声是否被误识别为用户插话。

## 接通、告别与挂断

中文开场模板为“您好，我是一名AI电话助手。给您打电话是想【来电目的】。”后台根据任务目标提供一句简短来意，Live 使用固定前缀表达，说完等待对方回应，再继续任务；被插话后不重播整段介绍。其他语言使用同等含义。此处通过提示词约束模型表达，尚未真机验证逐字遵循，并非预录音频。

应用先等待手机报告接通，并确认 SCO 已能接收 PCM，再启动会说话的 Live 会话。启动时同时采集输入，最多缓存两秒，并按采样速率发送，避免首句静默丢失；超限明确失败。响铃期间不启动交流。启动提示指定语言并要求先开口，后台同时准备首问；真实合成场景已观察到主动开场，但官方明确提示词不保证每次都主动说话，仍需多语言及真人接听验证。

后台通过 `finish_task` 同时提交任务结果与明确告别文字；成功后应用自动安排收尾，无需模型再调用 `hangup`。应用使用 `gpt-4o-mini-tts` 获取完整的 24 kHz PCM 告别片段，限制片段最大时长为 15 秒；完成下载后阻止 Live 输出进入播放队列，先排空此前已进入播放队列的回答，再播放该片段。播放结束时显式刷新重采样尾部与 SCO 尾帧，并等待实际播放时间结束，才请求手机挂断。

告别准备或播放期间收到用户转写、新委派或更新的任务上下文，会撤销尚未发出的挂断，并恢复对话。撤销告别不会撤销已经执行的工具操作。应用一旦已经向手机请求挂断，就无法承诺恢复该通话；语音识别和转写传输延迟造成的边界情况属于真实体验验收范围。

## 验证与授权

离线测试使用模拟 WebSocket、HTTP 响应、手机状态及本地音频 socket，不调用真实模型、不拨号。运行仓库测试环境中的测试：

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

音频专项覆盖连续输出无需回复完成事件、元数据不增长、积压超限报错、完整告别的采样尾部与播放等待、播放中断撤销和延迟统计。协议与任务测试覆盖模型路由、提示词拆分、后台调用、版本变化、工具记录和挂断状态。Realtime、Gemini 及既有功能应一同回归。

### 2026-10-18 真实 API 验证

经用户授权，在 10 分钟 Live 额度内使用真实 `gpt-live-1`、`gpt-6-luna` 和 `gpt-4o-mini-tts`。电话状态和双向 SCO 均为本地 socket 模拟，未拨号或部署。合成来电方先告知周五九点，再纠正为周四十点并确认。

- 初始完整模拟约 33 秒触发 2 秒积压上限；定位并修复 SCO 定时误差逐包累积。修复后连续 69 秒的峰值约 176 毫秒。
- 后续发现首问未主动准备、后台等待过长，以及答案确认未触发委派。补上连接后的首问调度、低推理强度和具体委派条件；这些测试曾因时限结束，不能计为任务成功。
- 修复后完整场景用量 46 秒，主动开场、纠正、确认和结构化结果保存成功；最终结果为周四上午十点。事件确认完整告别播放后才请求模拟挂断；峰值播放积压约 654 毫秒（含告别排队）。

- 告别准备期间插入“先别挂断，改为周三下午两点”，事件确认旧结果失效、待执行挂断撤销；重新确认后保存周三下午两点，再完整告别后挂断。该轮用量 66 秒，峰值播放积压约 335 毫秒。
- 七轮 Live 会话服务端均确认关闭，总用量 **383 秒（6 分 23 秒）**；未超过 10 分钟授权。后台 Responses 与 TTS 用量单独计费，不计入该 Live 秒数。

后续小米真机进行了两次获授权的拨号尝试，均在接通约 1.2 秒后结束，尚未输出 AI 音频。事件显示应用先请求挂断；不能仅凭 `phone_ended` 归因为接听方挂断。两次启动在约 0.4 秒内已读取接近两秒 PCM，与响铃期间 SCO 积压相符。已补上 Live 响铃期间持续读取、接通首包保留和原始启动错误保留，并添加离线复现；修复后尚未重新拨号验证。两次没有取得服务端最终用量，因此不计入上面 383 秒的已确认用量，也不能当成零用量。测试用临时服务覆盖已撤销，原服务已恢复。

修复后的第三次真机测试成功进入对话：手机通话约 69 秒，Live 服务端确认用量 67 秒，最终保存“十月三号早上六点”，告别播放事件先于挂断请求，播放积压高水位约 876 毫秒。已确认 Live 总用量增至 **450 秒（7 分 30 秒）**，另有前两次启动失败的未确认用量。接听方反馈没有卡顿，但语速偏慢、开场时无法打断；因此本轮仅证明主通话流程运行，不能判定自然让话体验通过。已缩短开场、避免后台重复自我介绍，并提示更利落的语速、开场期间遇到明确抢话及时让话；这些提示调整尚未真机复测，不是本地关键词打断逻辑。测试结束已恢复原服务。

第四次真机测试通话约 80 秒，Live 服务端确认用量 78 秒；已确认累计 **528 秒（8 分 48 秒）**，另有两次启动失败的未确认用量。转写显示开场问题因接听方询问身份而中止并转向回答，任务时间从早上六点改为晚上六点；提交结果后收到语速调整请求，应用撤销待执行挂断并恢复交流。接听方确认末尾由其主动结束，并表示开场打断暂保持现状。系统却把结束记录为 `audio_failed / Bad file descriptor`，音频关闭与手机空闲通知之间的错误归类仍待修正，不能将本轮记为完整正常结束。原服务已恢复，未自动重拨。接听方进一步要求固定开场文案，内容待确认。

`scripts/verify_live.py` 是需显式运行的付费协议冒烟脚本，读取环境变量 `OPENAI_API_KEY`，单轮 Live 连接限时 60 秒。它仅模拟工具返回，不能代替完整任务状态机或真机验收；后台和 TTS 另产生用量。脚本不读取服务密钥文件、不调用电话接口，报告和合成 PCM 默认写入 `/tmp`。

以下矩阵为获得真实模型及指定电话测试授权后执行的验收计划，**不代表已通过**。授权应明确模型用量、测试号码、目的、可使用信息及录音范围；不得自动重拨。

| 场景 | 离线可验证内容 | 真实模型与电话验收标准 |
| --- | --- | --- |
| 响铃与接通 | 会话启动受手机接通和音频就绪约束 | 响铃时不说话，接通后开场可听清，首句不丢失 |
| 附和误打断 | 不把普通转写映射为本地正常对话截断 | 对方说“嗯”“好的”时自然继续，需要让话时及时停止 |
| 回声 | 双向音频路径与记录完整性 | 不把自身输出误认作对方新请求；告别回声不会撤销挂断；检查不同音量、codec 与手机 |
| 任务中途纠正 | 版本失效、迟到答案抑制及操作记录 | 新要求进入当前推理；旧答案不误播；已执行操作如实说明 |
| 工具执行中插话 | 事件接收不等待工具执行、工具执行不因语音停止而取消 | 能继续听取新意图；工具结果不丢失、不重复执行；有歧义时询问 |
| 告别时插话 | 撤销待执行挂断、立即停止本地告别 | 及时恢复交流；测量插话到停止播放的延迟及转写滞后边界 |
| 提前挂断 | 结束路径中止输出并释放资源 | 对方挂断、用户取消、模型断线与音频失败均停止播放，不继续执行新的操作 |
| 连续音频延迟 | 2 秒上限、总延迟与高水位观测 | 长通话不逐渐积压；超限明确记录失败；无线抖动下行为可解释 |
| 旧模型回归 | 原有 Realtime/Gemini 测试 | 原有开场、打断、DTMF、结果保存、告别与挂断体验无回归 |

验收时结合任务事件、操作记录、转写、录音与手机实际状态判断。模型声称完成或转写出现告别，不足以证明电话另一端已经听到。

客户端上下文更新保持为完整短摘要，以 UTF-8 字节数保守限制在 500 以内，不再拆成独立字符片段。官方上限是 500 tokens，字节限制是本实现更严格的安全界限，并非两者等价；后台若违反短摘要约束，当前会明确终止通话并记录失败，不自动截断内容或重新调用模型。后台收到短暂连续转写时合并调度，但仍保守地使每个新用户片段令旧请求失效；附和或持续讲话可能增加后台延迟和用量，需实测评估。

撤销告别后通过本地代际丢弃先前已接收但尚未处理的音频。官方[播放控制说明](https://developers.openai.com/api/docs/guides/voice-server-controls#control-playback-when-needed)没有提供服务端输出清空或可证明新音频边界的命令，且指令确认不是恢复播放信号。因此重新放行后才到达的服务端在途音频仍无法可靠区分新旧；当前恢复策略及其回声、旧音频风险必须实测，不能据离线代际测试宣称完全消除。

## 2026-10-18 部署与 1.1.1 发布

已将 GPT-Live 工作树改动合并至运行目录，保留原有 MCP / GitHub OAuth 改动。
后端与 MCP 已重启，health.ready=true。MCP 初始化和 30 个工具发现通过，
创建任务、请求纠正和取消说明已同步 GPT-Live；现有 App ID 和访问权限保持不变。
完整离线回归 368 项通过，随后 MCP 专项 79 项通过；Ruff 与 diff 检查通过。
未进行新增拨号或付费模型测试，前述真机限制仍适用。

市场与 ZIP 发布为 1.1.1，公网内容及市场克隆已核验。
包 SHA-256：95abd8f481f1c9bb3975e6652a533433a799d1efaaf071cc3a1860418736f010。

## 1.1.2 自动收尾

GPT-Live 的 finish_task 同时接收结束语；成功后服务端自动合成、播放并挂断，
不再依赖模型随后调用 hangup。结束语不写入结构化 model_result。
新输入仍可撤销待执行收尾；已执行操作保留。成功提交后停止当前后台推理，
不会继续执行同一响应中剩余工具。Realtime/Gemini 也补充漏调 hangup 的兜底：等待提交结果后的结束语音频及轮次完成、播放排空后再挂断；插话或工具取消撤销待执行收尾。

### 1.1.2 部署验证（2026-10-18 America/Chicago）

已部署后端与 MCP；运行中 OpenAPI 版本为 1.1.2，health.ready=true。
完整回归 378 项通过；涵盖 Realtime/Gemini 漏调挂断、完整音频播放、插话撤销、
Gemini 工具取消，以及 Live 自动收尾、重复收尾与后台停止后续工具。
修正两个测试时序问题：收尾集成测试的 150ms 接通期限、协议测试遗漏启动缓存输入包。
Ruff、skill 校验、公开 ZIP / skill / MCP 文档与 Git 市场校验均通过。
市场版本 1.1.2，提交 daa02a6；保留原 App ID 与 OAuth 配置。
本轮未拨号或调用付费模型；新收尾路径的电话端听感仍需获授权真机复测。


## DTMF menu navigation

Luna owns IVR recognition, menu completeness and key selection. GPT-Live supplies
continuous audio/transcripts and conversation output; navigation does not depend
on a Live delegation event. New recipient transcript fragments wake one serial
backend worker, batched until 500 ms of quiet or at most 3 seconds of continuous
input. These are scheduling limits, never evidence that a menu is complete.

Before task reasoning, a tool-free Luna assessment receives accumulated input
since the last DTMF, history and operations. It returns `menu` for a complete current
menu, `information` for a complete task-relevant recorded answer, `dialogue` for ordinary conversation,
or `waiting` for incomplete menus/recordings and uncertainty. A relevant option,
transcript boundary, punctuation or silence alone is not sufficient. Complete
cycles and final instructions are assessed semantically by Luna; no local keyword
parser or key whitelist is used. Model judgments still require real-call validation.

`waiting` keeps collecting without a task/tool request. `dialogue` and `information` allow task
reasoning with DTMF unavailable. `menu` supplies the assessed snapshot to the task
reasoning request and makes DTMF available; Luna chooses the key. Unchanged input
is not reassessed, and context/navigation changes invalidate pending assessments.
Each attempt clears the menu buffer. Errors default to waiting, and the overall
call deadline bounds collection. Ordinary speech has no 45-second menu wait.

`model.ivr_assessment` records the verdict, input length and navigation epoch.
Startup and explicit task context updates still trigger backend work, and Live
can still delegate other task work. Incoming transcripts also trigger progress
when Live never delegates. The extra tool-free assessments use paid Luna requests.

Streaming transcript revisions do not invalidate a Live `send_dtmf` decision.
The provider and task executor both check the task control epoch, delegation,
and navigation epoch. The task executor reserves a navigation step under its tool
lock before sending; another tool ID cannot repeat the same step. Context changes
invalidate queued decisions. Other tools retain strict transcript revision checks.

A DTMF attempt ends that backend reasoning round. Phone acceptance starts a
feedback-only phase with no executable tools: coalesce input, then let Luna
assess whether information or a complete new menu has begun, without waiting
for Live delegation. Only a confirmed NEW actionable menu restores `ready`.
Information playback enters `listening`: DTMF is omitted from the reasoning tool
list and rejected again at the provider/task execution boundaries. Ordinary
reasoning can collect answers and finish the task. Later feedback is accumulated
across fragments and may unlock keys only on an explicit new-menu verdict;
information alone, silence, old options, or repetitions never unlock another key.
Reviews are serial and bounded by 45 seconds after phone acceptance; silence,
ambiguous initial feedback, or unknown/partial transmission ends navigation as
incomplete without resending. Once information playback is confirmed, the initial
45-second navigation deadline no longer cuts off the recording; the overall call
deadline still applies. Subsequent feedback reviews are bounded to 5 seconds. Feedback reviews use
the existing paid task reasoning model; automated tests mock all model requests.

Events `task.dtmf_requested`, `task.dtmf_rejected`, `task.dtmf_sending`,
`task.dtmf_accepted`, `task.dtmf_unknown`, `task.dtmf_feedback`, and
`task.dtmf_no_progress` distinguish the execution stages. AT acceptance is not
proof that the remote IVR received a key. Real-phone reception requires a separately
authorized test IVR call; simulated HFP tests only verify the emitted AT commands.


### Regression and rollout checks

The Costco replay test completes the opening before the first menu, supplies no
Live delegation/context update, navigates `2` then `1`, and injects a mistaken
second `1` while hours are still playing. It verifies one AT command per menu,
continued listening, structured completion and phone hangup. Separate regressions
cover nonstop transcript scheduling, unavailable-tool output, fragmented new menus,
control changes, partial sends and feedback timeouts. Model decisions are mocked;
these tests establish application behavior, not production model accuracy.

Before deployment, run the Live suites, full pytest suite and lint for changed
files; check active calls through the API before restarting. Preserve the previous
provider files for rollback. Do not automatically redial the business for acceptance:
any new live call needs user authorization.

The Luna-owned IVR change passed 420 automated tests. A separate authorized
Costco recording replay used real GPT Live transcription and real Luna requests,
with phone tools simulated: Luna kept waiting through partial options, recognized
the complete repeating menu at 53.4 seconds, then requested `send_dtmf("2")` at
54.3 seconds without any Live delegation event. Real Luna text checks also
classified ordinary dialogue, a partial menu and a complete menu as expected.
These checks do not establish remote IVR reception; no new phone call was placed.

The subsequent real call navigated `2` then `1`, but returned to navigation after
hearing hours and timed out. The correction gives Luna's input assessment the
task context: a complete relevant answer takes priority over a trailing partial
menu. Feedback containing residual options followed by the requested information
is treated as information playback. Task reasoning checks completion before
choosing a key and reports unconfirmed details rather than implying confirmation
or repeating navigation. Menu completeness still gates key selection.

Exact repeated text from Luna-confirmed recordings does not increment the task
revision or revoke pending closure. Matching requires a 64-character preceding
anchor plus the entire incoming fragment to occur in the assessed
recording after normalizing case, punctuation and whitespace. Word order and
values must still match. Novel or changed text still invalidates work; each DTMF clears this
reference. This only deduplicates known input and never recognizes IVRs or selects
keys. A complete task-relevant recorded answer is assessed as `information`,
separate from human `dialogue`, and cannot authorize DTMF.

Final authorized audio replay with real GPT Live and Luna submitted `finish_task`
at 62.4 seconds while the menu continued, without another key or spoken result
summary. It reported regular weekday closing at 8:30 PM and an unknown date-specific
exception (`partial`). Phone tools were simulated. Regression coverage verifies
that repeated recordings preserve closure, novel corrections revoke it, and
recorded-answer dialogue guidance is not forwarded to Live. No new business call
was placed during this fix.

The subsequent authorized real Costco call ended normally after 71 seconds. The
recording provided regular hours directly, so no DTMF was sent. The service saved
a partial result (8:30 PM regular closing; date-specific exception unknown),
played the English farewell and confirmed the phone idle. One redundant finish
request was rejected by the ending guard. This verifies completion and hangup,
not another DTMF navigation round.
