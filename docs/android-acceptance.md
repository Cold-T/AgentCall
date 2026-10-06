# Android 真机验收进度

2026-10-06 UTC，设备显示名称为 Xiaomi 17 Pro Max。手机 Android / HyperOS 版本、默认 SIM 尚待用户提供。主机为 zeroclaw，Ubuntu 25.10 / aarch64、Linux 6.17.0-1021-raspi、bluetoothd 5.83、板载 hci0；PBAP 改用独立构建的官方 obexd 5.87（system bus）。首通电话运行 CP5 main `353466bfa66dc3929853252949c4c966ed96fae3`；PBAP 使用本 PR 的修复。

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
| PBAP 联系人 | 传输、解析、SQLite 持久化和 API 查询通过；206 条号码记录，单条详情含原始 vCard。手机内容逐项人工核对待执行 |
| PBAP 历史 | 传输、持久化及独立来源查询通过；ich / och / mch 各 50 条，共 150 条，错误为空。与手机内容逐项人工核对待执行 |
| 服务重启后的 HFP 恢复 | 实际恢复到 `hfp_ready=true` / `idle`，仍只有原来一条项目通话记录；手机蓝牙关闭 / 开启的重连验收待执行 |
| 接听 / DTMF / mSBC / 重连 / AI / 稳定性 | 尚未执行 |

通话 ID：`8eda1e15-0809-4132-9db0-13e1aeb52f68`。原始事件、通话详情和音频证据保存在本地主机被 Git 忽略的 `recordings/android/`，包含私人号码及录音，不提交仓库。该记录只证明上表列出的实际项目，CP6 尚未完成，不能推断 iPhone 兼容性。

## 真机发现并修复的问题

- 用户 obexd 在没有活动 seat 的主机上无法初始化 Bluetooth transport。用户已明确授权部署 system bus OBEX 服务和只针对服务用户的 D-Bus 策略；AgentCall 仍以普通用户运行。
- 发行版 obexd 5.83 把 PBAP 接口注册到私有 bus 连接，CreateSession 成功但 Select 不存在。采用含上游修复的官方 5.87，只构建并安装独立 OBEX 二进制，bluetoothd 和发行版包不变。
- 完成的传输可能在第一次状态轮询前消失。先订阅可信 OBEX 所有者的 Transfer1 事件，保存完成 / 错误状态；不把文件存在当成功，清理会话时清除观察缓存，缓存数量有限。
- 系统 obexd 默认新建 root 所有的 0600 文件。AgentCall 在私有临时目录预建自己所有的 0600 目标文件，OBEX 原位写入，完成后普通用户可读；读取失败按电话簿返回错误并清理会话。

修复后本地 114 项测试通过，涵盖两种 bus、传输对象删除前后完成 / 错误事件、不可读文件；真实 SQLite 联系人和历史行数分别为 206 / 150，与 HTTP 查询一致。部署与回退步骤见 [安装说明](install.md)。
