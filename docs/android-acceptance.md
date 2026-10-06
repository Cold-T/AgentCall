# Android 真机验收进度

2026-10-06 UTC，设备显示名称为 Xiaomi 17 Pro Max。手机 Android / HyperOS 版本、默认 SIM 尚待用户提供。主机为 zeroclaw，Ubuntu 25.10 / aarch64、Linux 6.17.0-1021-raspi、BlueZ 5.83、板载 hci0。运行源码为 CP5 main `353466bfa66dc3929853252949c4c966ed96fae3`。

| 项目 | 实际结果 |
| --- | --- |
| 发现 / 配对 | 通过；首次确认超时后显式重新配对成功 |
| HFP 连接 | 通过；`hfp_ready=true`，手机实际状态 `idle` |
| 显式号码拨号 | 通过；使用用户提供的授权号码，观察 `dialing → alerting → active` |
| SCO 建立 | 通过；CVSD、s16le 单声道 8kHz、MTU 64 |
| Linux → 接听方音频 | 三组提示音；用户确认“听到提示音了，音质可接受” |
| 接听方 → Linux 音频 | 已采集约 25.83 秒非静音 PCM；未单独确认语音可懂性 |
| 持续双向数据 | 同一探针期间 rx_bytes=413400、tx_bytes=320000；不据字节增长推断同时说话音质 |
| 音频 CLI 退出 | 通过；附件退出后手机仍处于 `active` |
| HFP 挂断 | 通过；手机状态回到 `idle`，保存结束时间与 `phone_ended` 原因，接通时长约 65.21 秒 |
| PBAP | 未通过；用户 obexd 无活动 seat 时启动失败，system bus 修复部署待批准及实测 |
| 接听 / DTMF / mSBC / 重连 / AI / 稳定性 | 尚未执行 |

通话 ID：`8eda1e15-0809-4132-9db0-13e1aeb52f68`。原始事件、通话详情和音频证据保存在本地主机被 Git 忽略的 `recordings/android/`，包含私人号码及录音，不提交仓库。该记录只证明上表列出的实际项目，CP6 尚未完成，不能推断 iPhone 兼容性。
