# Checkpoint 2：无界面蓝牙服务验收

这里的 checkpoint 2 对应聊天清单中的第二项、原实施计划的阶段①。软件实现与自动验证已完成；Android / iPhone 实际验收按下列步骤单独执行，当前均未标记通过。

## 记录测试环境

每种平台分别记录手机型号、OS 版本、Linux / 内核、BlueZ / obexd 版本、蓝牙适配器型号、服务 git commit、codec、默认 SIM、测试号码和日期。首次在手机上确认配对与 PBAP 授权属于允许的人工步骤。

| 验收项 | Android | iPhone | 判定标准 |
| --- | --- | --- | --- |
| 发现 / 首次配对 | 待验证 | 待验证 | CLI 可发起；两端数值核对后配对成功 |
| HFP 连接 | 待验证 | 待验证 | devices.hfp_ready=true，不以普通 Connected 代替 |
| 拨号 / 状态 | 待验证 | 待验证 | 使用默认 SIM；实际依次观察 requested、dialing / alerting、active；AT OK 不算接通 |
| 挂断 | 待验证 | 待验证 | hangup 请求后手机状态回到 idle，记录结束时间 |
| 接听 | 待验证 | 待验证 | 来电生成项目 call ID；answer 后手机确认 active |
| DTMF | 待验证 | 待验证 | 在自有测试 IVR 上发送数字、*、#，由对端确认收到 |
| 双向 CVSD 音频 | 待验证 | 待验证 | 双方分别能听懂；同时说话时输入不中断；rx_bytes / tx_bytes 增长 |
| mSBC（支持时） | 待验证 | 待验证 | codec=2、16kHz，libsbc 编解码且双向清晰；能力不支持则如实记录 |
| PBAP 联系人 | 待验证 | 待验证 | 姓名、多个号码、所属手机与原始 vCard 可查询 |
| PBAP 历史 | 待验证 | 待验证 | ich / och / mch 实际返回与手机记录相符；不可用明确显示错误，不伪造成功 |
| PBAP 拒绝后直接拨号 | 待验证 | 待验证 | 保留旧同步数据；按号码仍可拨号 |
| 蓝牙重连 | 待验证 | 待验证 | ConnectProfile 自动恢复、无 ATD / BLDN、无新的手机拨号 |
| CLI 退出 | 待验证 | 待验证 | 服务继续持有 HFP；关闭 audio CLI 只结束本地音频附件、不挂断 |
| 断开 / 连续通话 | 待验证 | 待验证 | 原通话存储 bluetooth_disconnected，重连后能发起新的显式通话 |

## 运行步骤

1. 按 [安装说明](install.md) 启动后台服务，以 `phone watch` 和 journal / 服务日志保存事件。
2. 用 `phone scan start`、`phone devices` 找到测试手机；`phone pair DEVICE` 后核对手机数值，用 `phone confirm ID` 确认。
3. `phone connect DEVICE`；等 `phone devices` 的 hfp_ready 为 true。
4. `phone dial TEST_NUMBER --device DEVICE`；对端接听。记录返回 call ID，观察 active 和 audio.ready，检查手机实际拨号 SIM。
5. `phone audio CALL_ID --live --seconds 60`。双向分别说出随机短句并由对方复述；同时说话，验证一侧发送时另一侧仍能收到。
6. 在测试 IVR 上执行 `phone dtmf CALL_ID '12*#'`，由对端确认每个按键。不能只用 AT OK 作为 DTMF 到达证据。
7. `phone hangup CALL_ID`；验证手机与 GET /calls 的实际状态一致。
8. 从对端呼入手机，`phone calls --current` 获取 ID，`phone answer CALL_ID` 验证接听与双向音频。
9. 分别允许和拒绝 PBAP：`phone sync DEVICE`、`phone contacts`、`phone calls`。拒绝时再次验证直接号码拨打。
10. 建立显式 connect 意图后，关闭 / 开启手机蓝牙，观察重连；确认没有新拨号。再显式 dial 完成第二通电话。
11. `phone disconnect DEVICE`，确认重连意图被清除，不再后台自动连接。
12. 对另一种手机完整重复以上步骤；验收结果不能从另一平台推断。

每个项目记录通过 / 失败 / 不支持 / 未授权、证据文件与复现步骤。异常要保留原始 HFP / PBAP / SCO 错误，不用摘要替代。

## 验收结果

截至本次实现，两个平台均 **未执行实际验收**。用户已确认两种设备可用于之后的验收；本阶段的自动测试不代表真机通过。软件验证记录见 [verification.md](verification.md)。
