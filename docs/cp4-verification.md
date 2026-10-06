# CP4 软件与协议验证

2026-10-06（UTC），Linux aarch64 / Python 3.13.7。完整测试 **91 项通过**，包含真实 libsbc；ruff lint / format、编译、CLI 帮助与配置示例加载通过。Ubuntu CI 在 Python 3.11 / 3.13 验证。

## 已验证

- CP3 63 项基线保留；12 项共用任务 / HTTP 闭环测试分别运行在 OpenAI 和 Gemini 协议服务器上。
- 两家都覆盖可变长度模型音频 → 实际 SCO socket、持续输入、200ms 完整结束语在 CHUP 前发送、转写、工具参数校验、重复工具 ID、完成 / 部分完成、排队、启动幂等、忙线 / 未接、模型失败、蓝牙断开、取消、超时与 CLI 客户端退出。
- Gemini setup / setupComplete、16kHz 输入 / 24kHz 输出、全部 content parts、分片转写、工具结果 ID 与名称、BLOCKING 工具后自动继续；首轮文本只发送一次。
- generationComplete、turnComplete + IN_PROGRESS / IDLE 区分；插话、工具取消、goAway、运行中错误与实际断开事件。
- 格式 / Base64 / 不完整样本错误、缺失密钥、配置拒绝和建连超时；错误中不回显密钥。
- 同一手机的相同任务可以覆盖 provider，使用生产 factory 选择各家的环境凭据；默认模型 / 声音正确、旧 options 不串到新 provider，已保存任务不受服务默认值后续更改影响。
- API 取消待执行挂断后保持通话，已接收音频继续发送；模型已保存完成结果仍保留，之后用户取消记录独立 outcome。
- provider 失败只建立一次会话，不自动切换或重新拨号。

## 复现

```bash
python -m pip install -r requirements-dev.lock -e '.[test]'
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
```

没有增加依赖；继续使用 CP3 锁定的版本。测试的模型服务器、HFP AG 为本地模拟，WebSocket / SCO socket / SQLite / 重采样 / mSBC 为实际实现。

## 待执行

真实 OpenAI / Gemini 云端 API 均未访问，模型可用性、账户权限、云端配置接受程度和实际音频质量仍待验证。可选验证命令见 [Gemini 文档](gemini.md) 与 [OpenAI 文档](tasks.md)。本地模拟不证明云端 API 验收通过。

Android / iPhone 的 SIM 通话、两家模型闭环、音质、插话、锁屏、PBAP 与兼容性统一在 CP6 真机验收。当前没有真机通过的结果。
