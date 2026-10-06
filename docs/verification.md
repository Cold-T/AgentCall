# Checkpoint 2 软件验证记录

验证日期：2026-10-05（America/Chicago）。环境：Linux aarch64，Python 3.13.7。依赖版本保存在 `requirements-dev.lock`。

## 已完成的本地验证

- `ruff check src tests`：通过。
- Python 语法编译和服务 / CLI 帮助：通过。
- 实际服务启动：通过；真实 CLI `phone --json health` 返回 ready=false 和 BlueZ ServiceUnknown，devices 返回 503；OpenAPI schema 可访问。未启动或安装系统 BlueZ。
- 自动测试：37 项通过，覆盖下表；使用真实 libsbc 时无跳过。

| 范围 | 验证方法与结论 |
| --- | --- |
| HFP SLC | 真实 socket 上的 AG 模拟器；握手、可选命令拒绝 / 超时、分段 AT 行、初始已有通话、接听、DTMF、挂断、busy / no answer / CME、超时 / 取消断开、codec 确认、held 和 EOF |
| 实际状态 | AT OK 不把请求认定为 active；phone CIND / CIEV / RING 确定状态；断开保留明确原因 |
| API 闭环 | HTTP 请求 → HFP AT → 手机事件 → SQLite；并发 dial 只产生一个 ATD |
| 重连 | 只调用 ConnectProfile，无 ATD；显式 disconnect 清除持久化意图；BlueZ 首次不可用后能恢复 |
| PBAP | Select / PullAll / Transfer status；多号码、稳定 UID、设备隔离、历史来源、局部不可用、失败与超时；部分文件不当成成功 |
| 真实 D-Bus | 临时私有 dbus-daemon；真实 wire signature、UNIX FD 交接到 SLC、配对确认 / 拒绝、Profile RequestDisconnection |
| 真实网络 | 启动 uvicorn；HTTP Bearer、SSE、WebSocket Bearer、双向 PCM、单音频客户端所有权 |
| 音频 | SOCK_SEQPACKET 双向收发，可变长度输入，CVSD 短输入立即发送、MTU 分帧，真实 libsbc mSBC 编解码、分段 / 合并 H2 帧、手机发起 SCO 的原生 MAC 地址处理 |
| 持久化 | restart 将未结束记录标记 unknown / service_restart，保留重连意图，不重拨；手机历史与项目记录保持来源 |

系统没有安装 libsbc 时，两项 mSBC 测试会 skip。本次将 Ubuntu 的 `libsbc1 2.1-1` 下载并解包到临时目录，用 `LD_LIBRARY_PATH` 加载测试，未修改系统包。

```bash
LD_LIBRARY_PATH=/tmp/agentcall-libsbc/runtime/usr/lib/aarch64-linux-gnu .venv/bin/pytest -q
```

如果系统已安装 libsbc1，直接 `pytest -q` 即可。dbus-next 0.2.3 在 Python 3.13 上发出 `typing.no_type_check_decorator` 的弃用警告；当前测试通过，支持的 Python 版本为 3.11–3.13，升级 3.15 前需处理依赖兼容性。

## 不能由自动测试证明的内容

当前能看到 `hci0`，但系统 bus 没有运行 `org.bluez`，没有在本次会话中连接测试手机。因此没有执行手机 SIM 拨号、实际 SCO 无线传输、对端 DTMF 接收或手机 PBAP 授权验收。

真实 Android / iPhone 验收状态为待执行，步骤见 [acceptance.md](acceptance.md)。不把模拟 AG、真实 D-Bus、libsbc 编解码和 loopback 网络验证写成手机兼容性通过。

GitHub Actions 配置了 Python 3.11 / 3.13 测试矩阵，包含 dbus 和 libsbc1；远程运行结果以对应 commit 的 Actions 页面为准。
