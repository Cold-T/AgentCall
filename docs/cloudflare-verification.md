# 固定 PIN 公网部署验证

日期：2026-10-06（America/Chicago）。域名 `agenticcall.coldt.uk`，复用现有 vaultwarden Tunnel；`vw.coldt.uk` 保持原回源和正常 HTTP 200。

## 部署

- systemd 用户服务 `agentcall` 已启用，加载私有 0600 PIN 文件；配置 `pin_auth=true`、`root_path="/api"`、`obex_bus="system"`。
- nginx Gateway 只监听 `127.0.0.1:8766`；后台只监听 `127.0.0.1:8765`。根路径预留 UI，业务 API 放在 `/api/`。
- cloudflared 已增加新 hostname，DNS CNAME 已创建；部署前配置备份为 `/etc/cloudflared/config.yml.pre-agentcall`。
- 固定 PIN 取代 Cloudflare Access；不需要 team name / AUD / 邮箱白名单。PIN 本身未记录或提交。

## 实际公网检查

Cloudflare 公共 DNS 已返回代理地址；验证程序只替换该域名的本机 DNS 解析地址，URL / Host / TLS SNI 仍为真实公网域名，证书校验开启。通过实际 Cloudflare Tunnel 验证：

| 项目 | 结果 |
| --- | --- |
| 匿名 API / 错误 PIN | HTTP 401 |
| 未认证 POST /api/calls | HTTP 401，未触发拨号 |
| 正确 Bearer PIN | health 返回 ready=true |
| 正确 Basic（用户名 pin） | API 文档 HTTP 200 |
| 受保护联系人查询 | 返回 206 条号码记录 |
| 公网 SSE | HTTP 200，即时收到 events.ready |
| 根路径 | 返回 UI 预留信息 |
| 公网 HTTP | 重定向到同路径 HTTPS |
| 原有 vaultwarden | HTTP 200 |

验证时主机默认 DNS 仍有负缓存，普通域名请求暂未解析成功；不能据替换解析的检查宣称所有递归 DNS 已生效。等缓存刷新后再次执行普通 curl。公网音频 WebSocket 的真正手机通话需后续接通电话验证；软件测试已验证 PIN 下的真实 TCP HTTP、SSE 和全双工 WebSocket。

## 软件验证

128 项本地测试通过，含 PIN 缺失 / 非法时启动失败、Basic / Bearer、失败次数限制、客户端隔离、窗口恢复、缓存数量上限、私有 PIN 文件权限、OpenAPI 双认证描述及 PIN 音频链路。Ruff、格式、nginx 语法和 cloudflared ingress 验证通过。

部署和调用方式见 [cloudflare.md](cloudflare.md)。CP6 完整手机验收尚未完成，模型密钥仍由后台管理。

## 4 位 PIN 更新验证

同日将 PIN 改为严格 4 位 ASCII 数字，支持前导零；同步更新启动校验、设置接口、私有设置脚本、网页表单和测试数据。确认无当前通话后生成随机 PIN，原子写入仓库外的私有 `pin.env`（0600），重启 AgentCall。保留现有 Tunnel 和 nginx 路由，无需更改 DNS。

使用普通 DNS 解析和启用证书校验的实际公网 HTTPS 请求验证：匿名 / 错误 PIN 返回 401；正确 Bearer PIN 返回 200、`ready=true`；正确 Basic PIN 的 API 文档返回 200；认证 SSE 即时收到 `events.ready`；根路径返回 4 位 PIN 表单；4 位 PIN 登录创建 Secure cookie，退出后 API 返回 401。公网 HTTP 返回 301 并重定向到同路径 HTTPS，原有 Vaultwarden 返回 200。

159 项测试通过，包括非法长度 / 非 ASCII PIN 拒绝、前导零、认证限流、PIN 轮换与会话失效、真实 TCP HTTP / SSE / 双向音频 WebSocket。Ruff、格式检查、nginx 语法与 Cloudflare ingress 验证通过。可选 Playwright 浏览器检查因未安装依赖未执行，网页登录协议已通过实际公网请求验证；本次未执行真实手机音频公网验收。
