# 三个标签页的通话网页

入口：[agenticcall.coldt.uk](https://agenticcall.coldt.uk)。使用当前 4 位 PIN 登录，所有操作通过 `/api/` 调用后台服务。关闭浏览器后，通话任务继续执行。

| 标签页 | 功能 |
| --- | --- |
| 连接设备 | 搜索、配对确认、连接 / 断开手机、查看蓝牙与通话就绪状态、同步联系人和手机历史 |
| 发起通话 | 选择设备、从下拉列表选择联系人或手动输入号码、设置最长通话秒数、目标、模型与语言，查看固定背景并填写可选补充资料，立即发起 AI 通话；查看状态、取消任务或挂断 |
| 查看历史 | 按设备分页查看 AI 通话、手动通话和手机历史；详情展示目标、通话状态、模型、语言、结果与双方 transcript |

联系人列表仅加载所选设备的联系人；选择联系人后清空手动号码，输入号码后取消联系人选择。没有联系人时可先在连接设备页同步，也可直接输入号码。最长通话时间为 1–3600 秒，默认 300 秒。目标同时提交为 `goal` 与 `completion_criteria`，不再单独填写完成条件。

完整委托 AI 助理的通话说明由后台永久提供，网页只读展示；API 无需填写 `background`。可选补充背景只用于这次通话的额外资料，不能移除固定说明。语言通过下拉列表选择。模型下拉列表使用当前默认模型和另一家 provider 的默认模型；未配置 API Key 的 provider 不可选择。新任务自动启用 OpenAI 输入转写，或 Gemini 双向转写。模型 API Key 继续由后台保存，可通过设置 API 更新，不在网页展示密钥。

发起通话会立即创建并排队执行任务，实际拨号前请核对联系人或号码。历史将关联的任务与通话合并成一条记录，也包含尚未拨号就失败的任务。历史按时间倒序，每页 50 条；transcript 会逐页读取全部转写事件并以双方角色展示，外部文本不会被当作 HTML 执行。旧通话未启用转写或未接通时可能没有 transcript；手动通话与手机历史通常没有 AI 结果。

## PIN 与会话

生产 PIN 私有文件为 `~/.config/agentcall/pin.env`，权限 0600。对于通过 `--config` 启动且启用 PIN 的服务，配置目录内的 `pin.env` 为认证权威来源，优先于启动时的环境变量。文件原子更新后，新 PIN 在下次请求中生效，旧网页会话因 PIN 指纹变化失效，无需重启服务。文件内容无效或不能读取时认证失败，不会因为空值开放 API。

```bash
cd ~/AgentCall
.venv/bin/python scripts/set_pin.py
```

网页登录使用 opaque cookie，公网 HTTPS 设置 `Secure`、`HttpOnly`、`SameSite=Strict`，有效期 12 小时。cookie 写请求要求同源与 `X-AgentCall-CSRF: 1`。PIN 错误认证与 API 共用每 IP 10 次 / 60 秒的限流；网页分别提示错误 PIN、剩余等待时间、同源拒绝和服务故障。PIN 不写入 URL 或浏览器存储，也不提交 GitHub。

## 相关 API

- `GET /history?device=…&limit=50&offset=0`：合并任务与各类通话，按时间倒序分页。
- `GET /tasks/{id}`：目标、配置、关联通话和最终结果。
- `GET /tasks/{id}/events?kind=model.transcript&limit=1000&after_id=…`：分页读取转写。
- `POST /tasks`：网页请求将目标同时作为完成条件，并设置 `start_immediately=true`。
- `/session/login`、`/session/logout`、`/settings` 与 `/settings/credentials`：现有登录和配置 API 保持可用，公网路径均加 `/api`。

## 验证

```bash
pytest -q
ruff check .
ruff format --check .
node --check src/agentcall/api/web/app.js
node --check src/agentcall/api/web/login.js
# 安装项目 browser-test extra 和 Chromium 后运行；可指定已有 Chromium。
python scripts/verify_web_ui.py
```

浏览器脚本使用临时配置、内存数据库和模拟手机，禁用任务调度，不拨号也不调用模型。覆盖私有 PIN 与旧环境变量不一致的登录、三个标签页、联系人与号码、目标 / 完成条件、语言 / 时长、转写启用、结果与超过一页的 transcript、外部文本安全展示、手机布局、PIN 文件更新后的重新登录与退出。
