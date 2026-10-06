# PIN 登录网页

入口：[https://agenticcall.coldt.uk](https://agenticcall.coldt.uk)。输入原先自行设置的 6–12 位固定 PIN。未登录只显示登录表单，设备、联系人、任务、通话记录、设置和 API 文档均要求认证。

网页使用原生 HTML / CSS / JavaScript，不引入前端框架或构建步骤。所有操作调用现有 HTTP API，关掉浏览器后后台任务继续执行。桌面和手机浏览器均可使用。

## 操作范围

| 页面 | 功能 |
| --- | --- |
| 手机 | 刷新 / 发现、配对与确认、连接 / 断开、设备详情、联系人和手机历史同步 |
| 联系人 | 手机 / 关键词查询、数量与偏移量、详情、选为拨号或任务目标 |
| 通话与记录 | 号码或联系人拨号、当前状态、接听、挂断、DTMF、通话详情与音频指标；按手机与来源查询历史 |
| AI 任务 | 全部任务输入、目标与背景、资料 JSON、完成条件、结果 Schema、最长时长、provider / 模型 / 声音 / 语言 / 专属参数覆盖、立即执行或保存；任务 JSON 导入、启动 / 取消、补充上下文、结果与对话 / 工具事件 |
| 事件 | 实时 SSE、历史游标、数量及手机 / 通话 / 任务 / 类型过滤；页面最多保留 200 条实时消息 |
| 设置 | 全部非秘密服务参数、默认模型配置、只写 API 密钥与 PIN |

手动拨号和任务的“立即执行 / 启动”会真实呼叫目标，请先核对目标。启动按钮自动生成并在当前页面内复用幂等键，也可以填写自定义键。电话和任务是否成功仍以真实状态与结果记录分别判定。

网页不接管浏览器麦克风；AI 任务的 SCO ↔ 模型音频桥接仍在 Linux 后台持续运行。需要手动音频客户端时继续使用音频 WebSocket。

## 配置保存与生效

后台需用 `--config PATH` 启动。设置页展示 `Config` 的所有服务字段和默认 provider 配置；不会读取或显示环境变量的秘密值。字段包括监听地址 / 端口 / API 前缀、蓝牙适配器 / 编码 / OBEX bus、数据库路径、重连间隔、API 密钥环境变量名称及全部通话超时。`pin_auth` 和 `token_env` 在网页中只读；入口保持 PIN 认证，PIN 本身通过凭据表单修改。

保存时先校验全部字段和 provider 参数，再原子写入 `PATH.ui.json`，权限 0600。这份完整配置优先于原 TOML；`Config.load(PATH)` 在重启时读取它。若要重新只使用 TOML，先备份后移走 sidecar 文件。模型默认配置、两家密钥环境变量名和四种通话超时同时更新运行配置；已保存任务保留创建时的有效模型配置，已建立的 provider 会话不切换模型。

其他服务字段保存后显示“需重启服务才能生效”。确认无活动通话和执行中任务后运行：

```bash
systemctl --user restart agentcall
```

改变 API 路径前缀、监听端口或地址时，还需相应调整 nginx Gateway 回源和公开 API 路径；这些部署参数适合保持当前默认值。

API 密钥原子保存至配置文件所在目录的 `environment`，PIN 保存至同目录的 `pin.env`，权限都是 0600，并更新服务环境。生产配置位于 `~/.config/agentcall/config.toml`，提供的 systemd 单元会在下次启动时加载这两个私有文件。自行前台启动服务时，需自行加载环境文件。

留空表示不修改凭据。PIN 修改后立即注销所有网页会话，重新输入新 PIN；已有通话继续运行。网页密码框在成功提交后清空，PIN / API 密钥不写入 URL、localStorage 或 sessionStorage，也不在 API 响应中回显。校验失败的凭据响应也不包含输入值。

实际 PIN 文件在仓库外；`.gitignore` 另覆盖 `pin.env`、`environment`、`config.toml`、`*.toml.ui.json`，防止把同名本地文件误复制进 Git。仓库中的数字 PIN 只出现在自动测试的模拟数据中。

## 会话与 API

| 接口 | 行为 |
| --- | --- |
| `GET /ui` | 未认证返回登录表单，持有有效会话才返回操作页面 |
| `POST /session/login` | JSON `{ "pin": "…" }`；通过现有 PIN 限流器验证后设置 opaque session cookie |
| `POST /session/logout` | 注销当前会话并删除 cookie |
| `GET /settings` | 返回运行 / 已保存非秘密配置、需重启字段、只读字段和密钥是否已配置 |
| `PUT /settings` | JSON `{ "service": {…}, "provider": {…} }`；先校验，成功后私有持久化 |
| `PUT /settings/credentials` | 可选 `openai`、`gemini`、`pin`；只写，PIN 更新返回 `login_required=true` |

公网以上路径均加 `/api`，根路径由 nginx 回源到 `/ui`。登录 cookie 设置 `HttpOnly`、`SameSite=Strict`，公网 HTTPS 使用 `Secure`，有效期 12 小时；最多 128 个会话，重启、退出、到期、PIN 变化后失效。

使用 cookie 的写请求要求 `X-AgentCall-CSRF: 1`，有 Origin 时必须同源。音频 WebSocket 的 cookie 认证必须有同源 Origin。现有 Bearer / Basic HTTP、SSE 和 WebSocket 客户端保持兼容。Swagger 只读查询可以直接用会话；要执行写操作，使用 Authorize 配置 Bearer 或 Basic PIN。

登录错误与原 API 共用每 IP 10 次 / 60 秒的失败窗口。UI 使用 CSP、不可缓存响应和 `textContent` 展示外部数据；不把联系人、转写或结果当作 HTML 执行。

## 软件与浏览器验证

```bash
python -m pip install -e '.[test]'
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
node --check src/agentcall/api/web/app.js
node --check src/agentcall/api/web/login.js

# 可选浏览器闭环，不连接真实手机、不调用模型、不修改生产配置。
python -m pip install -e '.[test,browser-test]'
python -m playwright install chromium
python scripts/verify_web_ui.py
```

已安装 Chromium 时可通过 `AGENTCALL_TEST_BROWSER=/absolute/path/to/chromium` 指定可执行文件。测试使用临时私有配置、内存 SQLite 和 socket HFP 模拟手机；结束时清理临时文件。验证结果见 [网页验证记录](web-ui-verification.md)。
