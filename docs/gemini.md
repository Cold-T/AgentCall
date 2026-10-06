# CP4：Gemini Live

Gemini 和 OpenAI 共用任务、模型工具、SQLite、CLI / HTTP API 与 SCO 音频桥接。Gemini 接入 Developer API v1beta，直接通过 WebSocket 通信，不增加 SDK 或手机依赖。默认模型 `gemini-3.8-live`、声音 `Aoede`；可以按账户权限覆盖具体模型和声音。

## 使用

在 Linux 服务环境设置 `GEMINI_API_KEY`，systemd 用户服务仍使用 `~/.config/agentcall/environment`，建议权限 0600。配置环境后重启服务。`service.gemini_api_key_env` 可修改凭据变量名，OpenAI 保留原来的 `service.api_key_env`；凭据不写入任务或返回给客户端。两家使用各自的密钥。

可用 `config.gemini.example.toml` 将服务默认 provider 设为 Gemini。服务默认仍是 OpenAI 时，也可以直接创建 Gemini 任务：

```bash
# 先修改手机地址、测试号码与目标
phone task create --file examples/task.gemini.json
phone task start TASK_ID --key UNIQUE_REQUEST_KEY
phone task show TASK_ID
phone task events TASK_ID
phone watch
```

任务 JSON 的 `config.provider` 取 `openai` 或 `gemini`。切换 provider 时，自动选择该家的默认模型 / 声音，并重置另一家的 options；语言要求仍可继承。显式 model / voice / options 再覆盖这些默认值。同一家 provider 的 options 按字段合并默认值。任务保存有效配置，修改服务默认值只影响之后创建的任务，运行中不切换，也不在失败时重试另一家 provider。

Gemini `options` 支持 `temperature`、`topP`、`topK`、`maxOutputTokens`、`thinkingConfig`、`inputAudioTranscription`、`outputAudioTranscription`、`realtimeInputConfig` 和 `contextWindowCompression`。输入和输出转写现在默认启用，无需重复填写；需要其他选项时使用原始 API camelCase 配置，例如：

```json
{"provider":"gemini","options":{"inputAudioTranscription":{},"outputAudioTranscription":{}}}
```

具体选项能否使用取决于模型。禁止自定义端点、凭据、关闭服务端活动检测或关闭 API 插话。`thinkingConfig` 对默认模型不设置；高级模型行为需要真实 API 验证。

## 协议适配

以 `setupComplete` 确认建连，手机与 SCO 就绪后才发送第一轮触发。Gemini PCM 输入 16kHz、输出 24kHz，与 SCO 原生 8kHz / 16kHz 自动重采样。输出事件中的所有内容 part 都处理，转写分片独立记录，不作为音频前置步骤。工具声明使用 `parametersJsonSchema` 和 BLOCKING；工具结果用 ID 与名称匹配，Gemini 自动继续，后续统一接口的 start_response 不再次发送首轮提示。

Gemini 没有 OpenAI response ID，适配器为每轮生成内部 ID。generationComplete 只记录事件，正常挂断等待 turnComplete；IN_PROGRESS 不代表轮次已空闲，继续等待 IDLE。API 报告 interrupted 后清除本机未播放音频、停止当前发送并结束被打断轮次；新输出使用新的内部轮次 ID。Gemini 上下文由 Live API 管理，没有 OpenAI 的客户端 truncate 消息。API 取消工具时，取消尚在等待的挂断请求；已发送的 DTMF、已保存的模型结果或已完成的挂断无法撤销。goAway 记录即将关闭通知，不自动恢复会话或切换 provider，实际断开后按模型断开结束。

协议依据：[WebSocket 参考](https://ai.google.dev/api/live)、[能力说明](https://ai.google.dev/gemini-api/docs/live-api/capabilities)、[工具说明](https://ai.google.dev/gemini-api/docs/live-api/tools)。JSON Schema 字段参照 [v1beta discovery schema](https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta)；认证头使用 [官方 Python SDK 的方式](https://github.com/googleapis/python-genai/blob/main/google/genai/_api_client.py)。

## 验证范围

本地实际 WebSocket + 模拟 HFP AG + SCO socket 已验证 Gemini 和 OpenAI 使用同样的任务输入完成通话闭环。详见 [CP4 验证记录](cp4-verification.md)。Gemini 的真实 API 验证仍待执行；OpenAI 的 iPhone 真机音频结果见 [验收记录](iphone-acceptance.md)，两种手机的完整验收仍在 CP6。

配置密钥后可选择执行 `.venv/bin/python scripts/verify_gemini.py`，验证云端建连、PCM 输入和音频输出（产生 API 用量，不拨号）；`AGENTCALL_GEMINI_MODEL` 可覆盖模型。未执行此命令时，不得将本地协议测试当作真实 API 通过。
