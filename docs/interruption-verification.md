# 模型插话与电话播放中断

日期：2026-10-06（America/Chicago）。

问题：OpenAI 已取消生成，但本机仍继续发送之前收到的语音；模型事件读取还会等待一个只有 8 个条目的播放队列，延迟处理 speech_started。实现现在按 API 插话事件停止本机播放，并为 OpenAI 发送 conversation.item.truncate。

- OpenAI 音频保留 item_id / content_index，按 SCO 已发送字节和播放时钟估算 audio_end_ms；未播放的已入队项目截断到 0。
- 模型事件读取不会等待实时播放；队列最多 4096 个条目和 60 秒 PCM，超限明确报错。
- CVSD 在当前包之后停止；mSBC 等当前完整编码帧完成，避免破坏包边界。清除旧排队音频、编码残留和重采样状态，输入方向保留。已经送进蓝牙控制器或手机的短尾音无法撤回。
- 被打断回复的迟到音频与工具调用丢弃；未实际发送手机挂断指令的待处理挂断取消。正常结束语仍等待完整播放再挂断。
- Gemini interrupted 清除播放，并把被中断轮次记为 cancelled；后续回复使用新的内部轮次 ID，上下文由 Live API 管理。
- 被打断回复的后续转写标记 interrupted=true；完整转写可能包括未播放内容。

173 项 pytest、Ruff、格式检查通过。新增真实 SOCK_SEQPACKET 测试在 4 秒语音排队时插话，覆盖生成仍在进行和生成已完成两种状态，并在 200ms 内完成模拟中断处理；旧语音不会完整播放，迟到音频被丢弃，新回复可继续播放。验证 CVSD / mSBC 包边界、保留输入、队列字节上限、OpenAI truncate 消息与 Gemini 轮次取消。既有正常挂断、结束语、双向音频与 provider 测试通过。

本次未发起真实手机通话或调用真实模型。自动测试耗时不能当作手机端实际打断延迟；实际效果仍需下一次用户授权的真机通话验证。

参考：[OpenAI WebSocket interruption and truncation](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation)。

部署前确认没有未结束通话或执行中任务，随后重启 AgentCall。新服务进程已加载修复代码，真实公网 `/api/health` 使用当前 PIN 认证返回 200。
