# Cloudflare Tunnel 与身份白名单

公网域名：`https://agenticcall.coldt.uk`。本机 Gateway：`http://127.0.0.1:8766`；API 服务仍只监听 `127.0.0.1:8765`。公网使用 Cloudflare HTTPS，本机回源为 HTTP，不需要本机证书。

- `/`：只返回 UI 位置预留信息，未开发网页界面。
- `/api/`：全部业务 HTTP API；`/api/docs` 为 Swagger，`/api/openapi.json` 为规范。
- `/api/events`：SSE；`/api/calls/{id}/audio`：音频 WebSocket。
- 本机原有 `/health`、`/devices` 等路径保持可用。

## Access 应用与白名单（控制台操作）

1. Cloudflare Zero Trust → Access → Applications → 添加 Self-hosted 应用，名称 AgentCall，域名 **整个** `agenticcall.coldt.uk`，不设置路径限制。
2. 添加 Allow 策略，只 Include 明确允许的邮箱；需要时启用 One-time PIN 登录方式。不要添加 Everyone / Bypass 策略。
3. 如需脚本 / CLI，创建指定的 Service Token，另加 Service Auth 策略，只 Include 该令牌。邮箱策略负责浏览器身份，服务策略负责机器身份。
4. 记录应用 AUD 和 Zero Trust team name（`TEAM.cloudflareaccess.com` 中的 TEAM）；它们不是秘密。Client Secret 是秘密，只写入客户端环境，不发聊天或提交 Git。
5. 设置 `agenticcall.coldt.uk` 的 HTTP → HTTPS 重定向，使登录与 API 使用 HTTPS。

## 主机部署

1. 在 AgentCall `[service]` 设置 `root_path="/api"`，保持 loopback 监听。
2. 将 `deploy/agentcall.nginx.conf` 安装到 `/etc/nginx/conf.d/agentcall.conf`；执行 `sudo nginx -t` 后 reload nginx。
3. 在 `/etc/cloudflared/config.yml` 现有 catch-all 前增加示例 hostname 规则，填入真实 team name / AUD，保留其他 hostname。`access.required=true` 使 cloudflared 在回源前验证 Access JWT。
4. `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate`，并检查 `ingress rule https://agenticcall.coldt.uk/api/health` 与原有 hostname 的路由。
5. **先建立 Access 白名单及回源校验，再发布 DNS / ingress**。本机现有 vaultwarden Tunnel 可复用；复用时新增域名路由，不覆盖 vaultwarden 域名。DNS 示例：`cloudflared tunnel route dns vaultwarden agenticcall.coldt.uk`。DNS 操作需已有 Tunnel 授权。
6. 重启 AgentCall / cloudflared，验证匿名请求不能取得联系人或执行控制、白名单浏览器可登录、获准服务令牌请求可访问；分别验证 SSE 和 WebSocket。未验证身份控制前不能标记公网部署完成。

配置当前主机之外的环境时，请根据实际安装路径及 Tunnel 名称调整。公开部署不能使用无鉴权的 Quick Tunnel。

## HTTP / CLI

浏览器登录后使用 `/api/docs`。机器客户端使用两个 Access 请求头：

```bash
curl --fail 'https://agenticcall.coldt.uk/api/health' \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"

export AGENTCALL_URL='https://agenticcall.coldt.uk/api'
# 将两个 CF_ACCESS_* 环境变量配置在客户端，phone 自动读取。
phone --json health
phone watch
```

本地仍可用 `phone --url http://127.0.0.1:8765 health`。若后台额外配置了 AGENTCALL_TOKEN，远程客户端也需提供 Bearer token；Access 凭据和 Bearer 凭据独立。

Cloudflare 文档：[发布应用](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)、[回源 Access 验证](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/)、[服务令牌](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)。

## 当前验证状态

本地主机 nginx 已加载仅 loopback 的 8766 Gateway，AgentCall 使用 `/api` root_path。已通过真实 HTTP 验证根路径预留、重定向、HFP health、Swagger / OpenAPI 前缀、206 条联系人查询与即时 SSE。123 项软件测试通过；cloudflared 示例规则校验及域名匹配通过。

Access 白名单类型、允许身份、team name / AUD 待确认。公网 DNS / ingress 尚未发布；匿名拒绝、获准身份成功以及公网 SSE / WebSocket 验证待执行。不能据本机验证推断公网认证已完成。已准备的网关分支依赖 PBAP 修复 PR。
