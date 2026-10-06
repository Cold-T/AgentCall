# 上游和复用范围

上游：[PavelTarlev1/handsfree-linux](https://github.com/PavelTarlev1/handsfree-linux)

审查与复用固定于提交 `8e6026ca9b1bb4e63f5f6955369d22df7fdb09dd`，许可为 MIT。原始版权和许可保存在 `src/agentcall/vendor/LICENSE.handsfree-linux`，随包交付。

| 上游 | AgentCall | 改造 |
| --- | --- | --- |
| `bluetooth/at_handler.py` | `vendor/at.py` | 严格校验号码 / DTMF；移除 GUI 配置读取；只声明实际实现的 HF 特性；增加 BUSY / NO ANSWER 解析 |
| `audio/msbc.py` | `vendor/msbc.py` | 保留 libsbc 编解码；要求完整编码帧；关闭操作幂等 |
| `audio/sco_bridge.py` 的 libc SCO helper | `audio/socket.py` | 只保留 socket 操作；绑定所选适配器；增加接收手机发起 SCO 的 listener |
| `bluetooth/hfp_profile.py` / `slc.py` 的流程 | `bluetooth/dbus.py` / `hfp.py` | 重新实现 asyncio D-Bus / SLC，保留 HFP 注册、AT 握手与实际指示器状态流程 |
| `bluetooth/pbap_client.py` 的同步流程 | `bluetooth/pbap.py` | 沿用 obexd CreateSession / Select / PullAll 和 vCard 处理；保存多个 TEL；严格等待传输完成；显式返回各 phonebook 错误 |

没有引入 UI、PyQt6、GLib、VoIP 进程检测、铃声或上游桌面音频路由。Bluetooth D-Bus 边界集中在 `bluetooth/dbus.py`，之后可以替换设备管理后端；HFP 与 SCO 协议核心保持独立。

BlueZ 用于 Linux 配对、设备管理、HFP ProfileManager 和 obexd PBAP。选择它是本次实现决策，不是项目必须永久采用的架构约束。

协议参考：[BlueZ ProfileManager](https://github.com/bluez/bluez/blob/master/doc/org.bluez.ProfileManager.rst)、[Agent](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Agent.rst)、[PBAP PhonebookAccess](https://github.com/bluez/bluez/blob/master/doc/org.bluez.obex.PhonebookAccess.rst)。具体真机行为以本项目验收结果为准。
