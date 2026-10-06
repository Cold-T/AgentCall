# CP3：OpenAI 任务通话

后台持有任务和模型会话；CLI / HTTP 客户端退出不会取消任务。同一手机按创建顺序执行已启动任务，每次只允许一个任务或手动通话。保存时解析联系人号码，避免之后同步改变目标。Gemini 使用相同的任务操作，参见 [Gemini 配置与协议说明](gemini.md)。

## 配置与操作

在 Linux 服务环境配置 `OPENAI_API_KEY`，不要写入任务 JSON 或发送给客户端。systemd 使用 `~/.config/agentcall/environment` 中的 `OPENAI_API_KEY=...`，建议文件权限 0600；配置后重启用户服务。手动启动则在启动服务的 shell 环境设置变量。`config.example.toml` 的 `[provider]` 是默认配置，任务的 `config` 可覆盖模型、声音、语言与允许的专属参数。凭据变量名和超时设置属于服务配置。

```bash
# 修改示例的手机地址、号码和目标后执行
phone task create --file examples/task.json
phone task start TASK_ID --key UNIQUE_REQUEST_KEY
phone task show TASK_ID
phone task events TASK_ID
phone task context TASK_ID '补充资料'
phone watch
phone task cancel TASK_ID
phone --json task show TASK_ID
```

也可用 `contact_id` 替代 `number`；二者只能提供一个。`start_immediately: true` 在创建后排队，默认仅保存。`max_call_seconds` 从手机确认接通起计时，拨号等待和音频就绪另有服务端超时。已结束任务不能重新启动；需要重试业务时创建新任务。重复启动不会重复拨号，同一幂等键不能用于不同任务。排队取消不拨号；运行中取消会请求挂断。

## 协议与音频

实现 [OpenAI Realtime GA WebSocket 协议](https://developers.openai.com/api/docs/guides/realtime-conversations)，以 `session.updated` 确认会话配置，然后拨号。手机确认 active 且 SCO 就绪后才发起第一轮。两端为 s16le mono：SCO CVSD 8kHz / mSBC 16kHz 与模型 24kHz，通过持续 soxr 重采样连接。

输入和输出分别执行，对方输入在模型说话期间持续发送。接收任意长度的完整 PCM 样本块，按 SCO MTU / mSBC 编码要求发送并按采样时间节奏传输；没有固定时长拼块或独立 VAD。模型 API 负责语音检测；收到 OpenAI speech_started 或 Gemini interrupted 时，后台停止当前播放，丢弃尚未发送的旧回复音频并重置输出重采样状态。OpenAI 同步发送 conversation.item.truncate，按本机已发送及播放时钟估算已播放时长；Gemini 由 Live API 管理被中断轮次。已送入手机或蓝牙控制器的短尾音无法撤回。输出序列结束只刷新重采样尾部和必要的 mSBC 最后帧。日志 / 任务详情报告格式、采样率、待发送时长与字节数。

`send_dtmf`、`finish_task`、`hangup` 的参数由 JSON Schema 校验，工具 ID 在 SQLite 持久去重。`finish_task` 保存 completed / partial / incomplete 和匹配任务 `result_schema` 的对象。提交结果后，模型必须先生成实际结束语音频，再请求正常 `hangup`；只有工具参数中的“致谢 / 再见”不会播放给对方。缺少这段音频时拒绝挂断工具并要求模型先口头告别，电话继续保持。接受挂断后仍等待对应 response.done、重采样尾部和 SCO 发送结束才执行。异常 / 用户取消 / 超时立即进入清理，不等待结束语。挂断请求和实际手机状态分别记录；无法确认挂断时保留实际未结束通话，阻止后续重复拨号。已经主动向手机发送挂断指令后，SCO 先于 HFP idle 关闭属于正常清理；在发出挂断前发生的音频故障仍单独记录，不被模型结果覆盖。

记录包含状态、模型结果、转写（若 API 提供）、工具调用与结果、物理通话和独立的结束原因。蓝牙断开、模型故障、忙线等不会被模型完成摘要覆盖。启动服务时把中断的执行标为 service_restart，绝不恢复拨号；未开始的队列仍可继续。模型配置固定在保存任务时，新任务读取新的默认值；运行中不切换 provider。

## 验证边界

软件测试使用模拟手机 AG、真实本地 WebSocket 和 SCO socket，验证协议、音频和任务闭环；这不证明 OpenAI 云端或手机兼容性。按用户约定，真实 OpenAI API 验证保留待执行，Android / iPhone SIM 通话验收统一在 CP6。

配置密钥后，可先运行 `.venv/bin/python scripts/verify_openai.py`。该命令实际访问 API（会产生用量），确认会话配置、PCM 输入和音频输出，不拨号。可用 `AGENTCALL_OPENAI_MODEL` 覆盖模型；可用模型取决于账户权限。它不能替代真机任务验收。

## 默认通话说明

服务提供委托 AI 助理的完整默认通话说明，适用于 OpenAI 与 Gemini。`POST /tasks` 可省略 `background`；省略时任务记录保存默认说明，显式填写时替换说明，空字符串表示清空。网页预填写默认说明，可修改后用于本次通话，刷新页面恢复默认。任务执行时使用保存的说明。默认说明包含身份与来意开场、直接与接听者交谈、自然礼貌且每次一个主要问题、不朗读内部资料、不编造事实或擅自承诺、电话菜单 DTMF、提交结构化结果、致谢说完结束语后再挂断。

### 插话与播放队列

模型事件读取不再等待电话按实时速度播放。播放队列同时限制为最多 4096 个条目和 60 秒模型 PCM；超限明确结束任务，不通过阻塞事件读取等待队列腾空。API 的插话事件会停止当前 CVSD 包之后的发送，或等待当前完整 mSBC 帧边界再停止；输入方向始终保留。被打断回复的迟到音频及工具调用被丢弃，尚未真正执行的正常挂断被取消。正常未被打断的结束语仍完整发送后才挂断。

`task.audio_interrupted` 记录播放中断；被打断回复的后续转写带 `interrupted=true`，这份完整转写可能包含未播放内容，不能据此声称对方听到了全部文字。参考：[OpenAI WebSocket 插话与截断](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation)。

### 噪声触发与检测阈值

2026-10-06 的电话配置采用 `server_vad`、`threshold: 0.6` 和 `noise_reduction: {type: "near_field"}`，保留 `prefix_padding_ms: 300`、`silence_duration_ms: 500`、自动回复及插话中断。`config.example.toml` 包含相同配置；未提供覆盖参数时，provider 实现仍使用 `semantic_vad`。这是针对静音误触发的小幅调整起点，尚未通过真实电话校准。

[官方 VAD 文档](https://developers.openai.com/api/docs/guides/realtime-vad)说明 `server_vad` 的阈值范围是 0–1，较高值需要更响的输入才能触发。[API 参数参考](https://developers.openai.com/api/reference/resources/realtime/client-events)给出的默认阈值是 0.5，并说明降噪在 VAD 和模型之前处理输入，有助于减少误触发。`near_field` 面向近距离麦克风；免提或远距离麦克风可尝试 `far_field`。该阈值不是 dB，也不存在适合所有电话环境的固定最优值。

建议从 0.6 开始真实电话复测；安静时仍触发可按 0.05 步长提高到 0.65，轻声说话或插话漏检则降至 0.55。这些数值是本项目的调参起点，并非官方推荐值。若误触发主要随助手播放出现，应检查回声或音频串入，不能仅靠继续提高阈值。`semantic_vad` 的 `eagerness` 控制判断说完后的等待策略，不提供 `threshold`，不能用它替代噪声阈值。

通过认证的 `PUT /settings` 保存默认 provider 的 `options` 后，无需重启即可影响新建任务，重启后也会保留。网页重新加载以读取新参数；已保存任务固定使用创建时的配置，应新建任务复测。API 显式覆盖 `turn_detection` 或 `noise_reduction` 时使用调用者的值。

本次验证了本地及公网设置读回、持久配置重载和生成的 OpenAI 会话参数；没有调用真实模型或拨打电话。

### 自然结束语

固定模型说明和工具描述要求静默提交结果与挂断，不对接听者解释工具调用、结果提交、重试或“结束流程”。`finish_task` 成功后仅自然致谢、告别，例如“谢谢您的帮助，再见。”，说完再调用 `hangup`。缺少结束语导致挂断被推迟时，工具反馈也要求直接补充告别并静默重试。仍保留结束语音频播放完成后才实际挂断的保护。提示调整需要实际电话复测，不能保证模型每次都严格遵循。

### 开场静默

电话确认接通且 SCO 音频就绪后，输出播放先静默 1 秒，再播放开场白。等待仅发生在通话开头，后续轮次不额外延迟；输入音频与模型事件读取继续运行，期间插话仍可清除排队回复，取消和最长通话时限照常生效。模型生成可与等待并行，实际开口也可能因网络或生成耗时晚于 1 秒。`task.opening_pause` 事件记录本次开场等待秒数。

### API 与网页一致的默认值

新建任务省略或留空 `completion_criteria` 时，后台自动使用 `goal`；显式填写的具体完成条件保留。OpenAI 默认启用 `transcription: {model: "gpt-4o-mini-transcribe"}`，Gemini 默认启用 `inputAudioTranscription: {}` 和 `outputAudioTranscription: {}`，切换 provider 时也使用对应默认值。网页不再自行补齐转写参数，两种入口共同使用后台默认配置。显式指定的转写参数保持有效（OpenAI 可显式传 `transcription: null` 关闭）；原有任务的配置快照不改写。API 创建后默认仅保存，不自动拨号。


## HFP 服务发布与音频建立

本机以 Hands-Free UUID `0000111e-0000-1000-8000-00805f9b34fb` 注册 BlueZ profile，保留默认的客户端与服务端能力。不要限制为 `Role=client`：该设置会关闭本机 SDP 免提服务发布及接收手机 RFCOMM 连接的监听，虽能主动连接手机控制通道，手机却无法发现本机的免提服务。依据 [BlueZ profile 实现](https://github.com/bluez/bluez/blob/master/src/profile.c)，注册后应在适配器 UUID 列表看到 Hands-Free 服务。

SCO 监听在响铃期间保持等待，不以固定 20 次监听轮询提前结束。接通后才开始计算配置的音频建立时限，并尝试主动连接 SCO。连接失败事件 `audio.connect_failed` 保存系统 errno 与失败阶段，便于区分拒绝、控制器错误和超时。`hfp_ready` 只表示控制通道握手成功；实际通话音频还需要 SCO 就绪。服务发布及模拟测试通过并不证明 iPhone 已选择蓝牙音频路由，仍需用户在手机音频列表确认，并在授权的通话中验证声音。

接通后的任务音频就绪判定还必须成功读取首包 PCM；仅有响铃阶段的 SCO socket 不够。首包会保留给输入桥接及录音，不重复计数。若首读收到连接重置或 EOF，记录 `task.audio_reconnecting`，在原音频就绪时限内替换 SCO 连接，保持同一次通话，不重新拨号。收到可用音频后才开始模型对话，仍保留开场 1 秒静默。重建持续失败、超过音频或最长通话时限、取消、手机已结束等情况正常退出；已经开始对话后的音频故障仍保留失败记录。

音频监听先于 `AT+BCC` / `AT+BCS` 编码协商启动，使用 Linux `BT_DEFER_SETUP` 暂缓接受手机的 SCO 请求。接受正确手机的连接后，依据协商结果设置 `BT_VOICE`，再执行授权读取；授权读取返回零不是音频 EOF，不送入转写或录音。这样避免协商后才监听而漏掉立即到达的 mSBC 连接，并保留 CVSD 回退。`audio.listening` 表示监听已启动；只有 `audio.ready` 和任务开始收发音频后才能认为模型对话已启动。此修改经模拟回归验证，双向声音与转写仍需通过服务 HTTP API 发起获授权的真机通话验证。
