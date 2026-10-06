# Cloudflare Tunnel 与 4 位 PIN

公网域名：`https://agenticcall.coldt.uk`。本机网关为 `http://127.0.0.1:8766`，后台只监听 `127.0.0.1:8765`。公网使用 HTTPS，本机回源使用 HTTP；不需要 Cloudflare Access、邮箱白名单或本机证书。

- `/`：PIN 登录页；认证后显示最简操作网页，见 [网页说明](web-ui.md)。
- `/api/`：全部业务 API，固定 PIN 认证。
- `/api/docs`：API 文档；网页登录后可直接访问，也支持 Basic 用户名 `pin` / 密码 PIN。
- `/api/events`：SSE；`/api/calls/{id}/audio`：双向音频 WebSocket，均验证 PIN。

## 设置与轮换 PIN

```bash
cd ~/AgentCall
.venv/bin/python scripts/set_pin.py
```

输入并确认 4 位数字，不回显，不进入 shell 历史。脚本原子写入用户私有的 `~/.config/agentcall/pin.env`，权限 0600；不把 PIN 写进配置或 Git。该文件由 systemd 用户服务加载。重新运行脚本并执行 `systemctl --user restart agentcall` 可轮换 PIN；重启会结束现有后台会话，通话期间不执行。

PIN 严格使用 4 个 ASCII 数字，支持前导零；启动、设置接口和网页表单使用相同长度约束。当前部署已生成新的随机 4 位 PIN，保存在上述私有文件中，旧的 6 位 PIN 已失效。

配置 `[service]` 设置 `pin_auth=true`、`token_env="AGENTCALL_TOKEN"`、`root_path="/api"`，保持 loopback 监听。PIN 缺失或格式错误时服务启动失败，不允许空 PIN 开放 API。API 接受 `Authorization: Bearer PIN`，或 HTTP Basic 用户名 `pin` / 密码 PIN。每个客户端 IP 在 60 秒内累计 10 次错误认证后暂时返回 429 和 Retry-After；被限流期间正确 PIN 也需等待窗口结束。错误观察缓存最多保留 1024 个客户端，内存状态在重启后清空；不是分布式账号系统。

## 主机与 Tunnel 部署

1. 部署 `deploy/agentcall.service` 到用户 systemd，准备用户配置与私有 PIN 文件，启用后台服务。
2. 部署 `deploy/agentcall.nginx.conf` 到 `/etc/nginx/conf.d/agentcall.conf`，先 `sudo nginx -t` 再 reload。网关仅监听 loopback，保留 SSE 流和 WebSocket Upgrade。
3. 在 `/etc/cloudflared/config.yml` 的 catch-all 前加入示例 hostname 规则。当前使用已有 vaultwarden Tunnel，不修改原来的 `vw.coldt.uk → localhost:8000`。
4. 验证匿名和错误 PIN 被拒绝、正确 PIN 成功，再验证 `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate` 和 `ingress rule` 对两个域名的路由。
5. `cloudflared tunnel route dns vaultwarden agenticcall.coldt.uk`，安装新配置、重启 cloudflared，再通过公网执行认证与流式验证。正常 cloudflared CLI 使用既有 Tunnel 授权，不提取证书内的 token。

公网 HTTP 请求由网关重定向到 HTTPS。本机 HTTP 连接保持不变。网关仅信任本机 Tunnel 传来的 CF-Connecting-IP，把原始客户端 IP 覆盖写入 X-Forwarded-For，后台仅信任 loopback 代理。不能直接向外开放网关端口，也不能添加删除 IP 头的 Cloudflare Transform 后仍假定每个访问者有独立限流身份。

## 请求示例

```bash
# curl 会提示输入 PIN，不要把 PIN 直接放进命令行。
curl --fail -u pin https://agenticcall.coldt.uk/api/health

# 本机 CLI：加载已设置的私有环境变量。
set -a
. ~/.config/agentcall/pin.env
set +a
export AGENTCALL_URL=https://agenticcall.coldt.uk/api
phone --json health
phone watch
```

其他设备客户端自行配置 AGENTCALL_TOKEN 为 PIN；请求头用于 HTTP、SSE 和音频 WebSocket。无需 OpenAI / Gemini API 密钥。API 密钥仍由 Linux 后台持有。已认证业务请求按既有语义处理，访问者拥有全部电话控制及查询能力，当前不区分用户角色。

## 回退

先从 Tunnel 删除该 hostname 或恢复部署前备份配置，再删除对应 DNS 记录，停止公开网关。后台可保留 PIN 认证。不要在保留公网映射时禁用 pin_auth 或移除 PIN。原有 vaultwarden 路由与凭据保持不变。

参考：[Cloudflare Tunnel HTTP 回源](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/)、[Cloudflare 请求头](https://developers.cloudflare.com/fundamentals/reference/http-headers/)。

实际部署及验证记录见 [cloudflare-verification.md](cloudflare-verification.md)。
