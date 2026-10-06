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
