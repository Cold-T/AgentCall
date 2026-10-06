# 最简网页验证

日期：2026-10-06（America/Chicago）。生产入口 `https://agenticcall.coldt.uk`，沿用固定 PIN、Cloudflare Tunnel 和 loopback nginx Gateway。

## 自动验证

- 146 项 pytest 测试通过；Ruff、格式检查、两份 JavaScript 的 `node --check` 通过。
- 新增网页相关测试覆盖匿名页面与业务隔离、cookie 属性、退出 / 过期 / 伪造会话、会话数量上限、PIN 变化失效、与 API 共享限流、CSRF、完整配置保存 / 重载、非法配置不落盘、凭据不回显与私有文件权限。
- 真实 TCP 验证会话 cookie 下的 HTTP / SSE 和全双工音频 WebSocket；错误 / 缺失 Origin、退出后的 cookie 不能连接音频。
- Wheel 打包验证包含所有网页 HTML / CSS / JavaScript 文件；没有前端框架或构建依赖。

## Chromium 操作闭环

可复现脚本：`scripts/verify_web_ui.py`，使用内存数据库、真实 socket HFP 模拟手机和临时配置。验证：

1. 未登录只能看到 PIN 表单，输入正确 PIN 进入。
2. 读取设备、搜索联系人，选联系人填入任务。
3. 填写目标、背景、资料、完成条件，创建保存任务，查询任务与结果。
4. 修改默认 provider 为 Gemini，修改语言、参数和超时，保存并从文件重载验证。
5. 设置页不显示环境变量名，直接填写两家的模拟 API Key 并保存；页面只显示已配置状态，输入框清空。保存普通配置后内部凭据映射保持有效。
6. SSE 收到 events.ready 与主动发出的 verification.probe。
7. 模拟号码拨号，模拟手机报告 active 后发送 DTMF，再挂断；检查实际 ATD / AT+VTS 指令。
8. 修改模拟 PIN，被注销，使用新 PIN 重新登录，再退出。

全部通过，JavaScript 运行错误为 0。仅模拟通话，不代表新的真机验收或模型 API 验证。

## 实际公网检查

使用正常域名解析、真实 HTTPS、TLS 证书校验和 Cloudflare Tunnel 完成 HTTP 与 Chromium 检查：

| 项目 | 结果 |
| --- | --- |
| 根页面匿名访问 | HTTP 200，仅 PIN 表单 |
| 匿名业务 / 设置查询 | HTTP 401 |
| 正确用户 PIN 登录 | HTTP 200，opaque HttpOnly / Secure / SameSite=Strict cookie |
| 已认证页面 | 展示完整操作界面 |
| 设置查询 | 16 个服务字段、5 个 provider 字段；未回显 PIN 或密钥 |
| 网页 API Key 设置 | 隐藏环境变量名，独立 OpenAI / Gemini 密钥输入框和保存按钮；公网只读检查通过 |
| 服务状态 | ready=true |
| 联系人 | 206 条号码记录；浏览器查询正常 |
| 公网 SSE | 收到 events.ready |
| 浏览器秘密存储 | document.cookie 不可读会话；localStorage / sessionStorage 无 PIN |
| 手机视口 390px | 设置页无水平页面溢出，联系人和事件操作正常 |
| 退出登录 | 回到 PIN 表单，业务查询再次 HTTP 401 |
| 浏览器运行错误 | 0 |
| 原有 vw.coldt.uk | HTTP 200 |

公网浏览器检查只读业务信息、登录和退出；未额外拨打真实电话、修改生产 PIN 或模型密钥。完整 CP6 手机和真实 provider 验收仍按原计划执行。

生产 PIN 文件位于仓库外的 `~/.config/agentcall/pin.env`，权限 0600。Git 未跟踪该文件，并验证防误复制的 `pin.env`、`environment`、`config.toml` 与 `*.toml.ui.json` 忽略规则。
