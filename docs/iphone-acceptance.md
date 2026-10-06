# iPhone 真机音频验收与修复

2026-10-06，America/Chicago 06:08–06:10。设备名称为 ColdT’s iPhone；具体型号、iOS 版本与默认 SIM 未确认。主机 zeroclaw，Linux 6.17.0-1021-raspi / aarch64，BlueZ 5.83，板载 hci0。基于提交 `f9246c3` 加本次工作区修复，使用服务已配置的 OpenAI Realtime，通过用户明确授权的号码、外部音频处理和本机录音执行。

## 原因和修复

原实现将 `AT+BCS` 的最终 OK 作为开始接受 SCO 的前提。抓包中存在手机先发 SCO 请求、最终 OK 在 SCO 请求被拒绝后才到达的时序；等待造成监听超时退出。

更直接的故障是 iPhone 在响铃阶段反复替换音频连接。原实现首条 SCO 建立后关闭监听，只在接通后读取首包，因此无法及时发现响铃链路已经被手机关闭。手机再次发送 `+BCS:2` 并请求 mSBC 时，没有新的 deferred listener；HCI 自动接受使用了 CVSD `0x0060`，控制器报告 `Invalid HCI Command Parameters (0x12)`。后续 `AT+BCS` 或 `AT+CIND?` 回复延迟，程序因 AT 超时关闭 HFP，Agent 未开始对话。接听方在失败通话中报告无 Agent 声音和自己的回声；不能从抓包确定回声本身产生在哪个环节。

修复将 codec 选择与最终确认分开：接到手机 codec 选择后，可先用匹配的 `BT_VOICE` 授权 deferred SCO；发布 `audio.ready` 仍要求最终 codec 确认。每次再次确认手机的 codec 前，恢复 SCO listener，关闭旧音频 socket，让手机替代链路被正确接收。保留 AT 超时关闭控制通道的规则，并补充超时命令名和被拒服务 UUID 的诊断事件。

另外，之前 `unsupported service` 的 UUID 已确认是 AVRCP `0000110e-0000-1000-8000-00805f9b34fb`。该拒绝发生后 HFP 仍能完成拨号，不能把它当作本次音频故障的已证实原因。

## 实际结果

| 项目 | 结果与证据 |
| --- | --- |
| HFP / 拨号 / 接通 | 通过；观察 idle → dialing → alerting → active |
| 响铃 SCO 替换 | 通过；三次连接都以 transparent `0x0003` 成功建立，而非 CVSD 参数 |
| codec / PCM | mSBC（codec 2）、s16le mono 16kHz、MTU 64 |
| Agent → 接听方 | Agent 实际开口；接听方回答“声音还挺不错的” |
| 接听方 → Agent | 正确转写“蓝牙音频测试一二三四五”；这通末尾未完成完整复述确认 |
| 回声 | 接听方在通话内回答“没有”，并在聊天中明确确认“Agent 声音清楚，没有回声” |
| 打断 | 接听方说“停”，日志含 interrupted 音频轮次；随后反馈“好像打断也是可以的” |
| 持续双向音频 | 最后观测 rx_bytes=2129520、tx_bytes=1240320；抓包含实际 HCI SCO TX / RX |
| 结束 / 挂断 | 达到配置的 65 秒通话上限，播放结束语后请求挂断；手机确认 idle，任务 outcome=timeout，不是音频失败 |
| 录音 | 双声道 WAV，16kHz，约 66.60 秒；左为对方，右为实际发送的 Agent PCM |

成功复验任务 `0e895ba9-99ef-4bd4-87f8-ee7e65f77647`，实际通话 `7a0f9839-b61a-48a9-b32a-e0a3cc15e003`，接通时长约 65.33 秒。包含私人号码、转写和录音的原始证据保存在被 Git 忽略的 `recordings/iphone/2026-10-06/`，权限为目录 0700、文件 0600；蓝牙原始 btsnoop 位于本机 `/tmp/agentcall-iphone/`，不提交仓库。

增加了真实本地 RFCOMM socket 的定向回归，模拟 AG 在 SCO 授权前不发送 BCS OK，以及在响铃期间再次选择 codec、更换 SCO。模拟链路不替代上述真机结果。一次相关回归中 Gemini 排队用例在极短时限下失败，单独重跑通过；停止真机通话负载后，完整相关回归复跑 83 项全部通过；Ruff 和 git diff --check 通过。

本次仅证明这台 iPhone 在当前主机、配置和一次修复后的通话下音频可用。PBAP、接听来电、DTMF、跨设备重连、CVSD 对照与长期稳定性未在本次验收，CP6 仍未全部完成。
