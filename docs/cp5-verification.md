# CP5 CLI / HTTP API 软件验收

2026-10-06（UTC），Linux aarch64 / Python 3.13.7。完整回归 **106 项测试通过**，包含真实 libsbc；ruff lint / format、CLI 帮助和编译检查通过。GitHub CI 使用 Ubuntu 与 Python 3.11 / 3.13。

## 已完成

| CP5 条件 | 实现与证据 |
| --- | --- |
| 全部业务 CLI / API | 手机扫描 / 配对 / 确认 / 连接 / 断开、PBAP 同步、联系人详情 / 搜索、通话来源 / 详情 / 当前状态 / 控制、任务创建 / 启动 / 取消 / 上下文 / 结果 / 工具、实时与持久事件均有对应命令和接口 |
| 异步与幂等 | 创建 / 启动立即返回 ID；真实 HTTP CLI 测试中，手机忙时任务排队，重复 key 不增加 ATD；两 provider 基线覆盖实际执行、排队与退出客户端后继续运行 |
| CLI 输出与认证 | 人类可读表格、完整 JSON、HTTP 错误 JSON stderr / 非零退出；真实子进程 phone watch 验证 Bearer、SSE 筛选、NDJSON 与 Ctrl-C 退出 |
| SQLite 与来源 | 保存设备快照、PBAP 实际内容、项目记录、任务结果、工具及事件；旧数据库增量建表 / 设备补齐保留既有数据；PBAP 来源不与项目记录混淆 |
| 文档与帮助 | docs/cli.md、docs/api.md 覆盖命令、过滤、分页、游标、来源、时长、认证、状态和错误；自动 OpenAPI 声明 BearerAuth 与请求 schema |

## 新增验证

- 11 项 API / 数据测试：来源、设备、联系人搜索和分页；PBAP 详情及禁止控制历史；项目时长；任务状态 / 结束原因筛选；结果与工具查询 / 去重；事件游标和筛选；配对请求 ID 不覆盖持久 event_id；工具调用 ID 不覆盖物理 call_id。
- 设备快照离线查询与明确 live 标记；旧 schema 升级后保留记录 / 重连意图，设备信息重新打开仍保留。
- 全部查询和文档受 Bearer 保护，401 返回 WWW-Authenticate；服务模型密钥不出现在公开响应中。无效分页 / 来源 / 任务状态返回 422。
- 4 项 CLI 测试：CLI 通过真实本地 HTTP server 操作模拟 HFP 手机，验证数字 / 联系人拨号、接听、DTMF、挂断、配对流程映射、同步、详情、列表、排队 / 重试 / 取消与事件查询；远程文本按字面显示；失败输出明确。
- 真正启动 CLI watch 子进程，经实际 TCP / SSE 接收筛选事件，与持久查询的 event_id 对应，Ctrl-C 成功退出；解析多行 data 与心跳。
- 两家任务闭环查询真实执行后的 /result、/tools 和按物理通话筛选的 tool.result 事件，保留任务、模型和手机状态的独立含义。

## 复现

```bash
python -m pip install -r requirements-dev.lock -e '.[test]'
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
phone --help
phone dial --help
phone task --help
phone events --help
```

测试保留 CP4 的 91 项基线，加 15 项新验证；没有新增第三方依赖。蓝牙 AG / PBAP 返回值 / 模型服务器是模拟，实际运行 HTTP、SSE、WebSocket、CLI 子进程、SCO socket、编解码、重采样与 SQLite。

## 边界

真实 OpenAI / Gemini API 未访问，仍待执行。Android / iPhone SIM 通话、实际音质、锁屏、插话、PBAP 权限和兼容性仍在 CP6 真机验收。CP5 不改变当前 API 自动插话配置，不新增本地打断策略或清空音频缓存。
