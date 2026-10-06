# CP3 软件与协议验证记录

2026-10-06（UTC），Linux aarch64 / Python 3.13.7。本地完整测试 **62 项通过**；ruff 检查、格式检查、CLI 帮助、示例 TOML 加载和 git diff --check 通过。真实 libsbc 参与 mSBC 测试，没有以跳过编解码代替通过。

## 已验证

- CP2 的 37 项蓝牙 / D-Bus FD 交接 / PBAP / HFP / CVSD / mSBC / HTTP / SSE 基线保持通过。
- 本地实际 WebSocket 验证 OpenAI GA session.update、PCM 24kHz 格式确认、连续音频输入、可变长度音频输出、转写、工具结果和上下文更新；无效音频、配置拒绝和断开可识别。
- 真实 socket 上的模拟手机 AG 与 SCO，连同 HTTP 和 SQLite，验证任务创建 → 模型建立 → ATD → 手机确认接通 → 音频桥接 → DTMF / 结构化完成 / 挂断 → 查询结果。
- 模拟 200ms 模型音频以不同大小块发送，重采样为 CVSD 后完整 3200 字节在 CHUP 前发送；发送期间持续接收手机音频。8kHz / 16kHz ↔ 24kHz 重采样验证时长、440Hz 信号和分块边界。
- 每手机排队、重复启动不重拨、幂等键冲突、重复工具 ID、错误工具参数及结果 schema 校验。
- 独立保存模型 partial / completed、实际手机状态、忙线、未接、蓝牙断开、模型连接失败 / 断开、用户取消和超时。蓝牙断开不会被已提交完成结果覆盖。
- 联系人号码在创建时解析；客户端关闭后任务继续。活动上下文更新、音频就绪期间最长时限、手动拨号占用冲突、保存 / 排队取消均验证。
- SQLite 重新打开后，执行中任务以 service_restart 结束，不重新排队拨号；工具和启动幂等记录仍保留。未开始队列保留。
- 禁止远程结果 schema 引用、任务凭据和端点配置；服务密钥不出现在任务结果 / 事件中。

## 复现

```bash
python -m pip install -r requirements-dev.lock -e '.[test]'
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
phone task --help
phone task create --help
```

需要 dbus 和 libsbc1；CI 在 Ubuntu 上使用 Python 3.11 / 3.13 执行相同软件测试。依赖锁定 NumPy 2.3.5，保留 Python 3.11 支持。

## 待执行

- 真实 OpenAI Realtime API：按用户确认暂不执行。未配置密钥；本地协议服务器通过不等于云端模型、账户权限或计费验证通过。可选入口为 `scripts/verify_openai.py`，说明见 [任务文档](tasks.md)。
- Android / iPhone 真机与真实 SIM 通话闭环、音质、插话、PBAP 权限、锁屏及兼容性：统一在 CP6。没有真机验收结果。
