# AgentCall

在 Linux 上通过 Android 或 iPhone 的手机 SIM 拨打电话，让实时语音模型完成预设任务。所有业务通过 HTTP API 提供，CLI 和最简网页作为客户端；后台独立运行。

项目基于 handsfree-linux 扩展，复用蓝牙与联系人实现并将核心功能从 GUI 解耦。通话控制和音频使用纯蓝牙 HFP / SCO，不依赖 ADB、手机 App 或屏幕操作；联系人和手机历史另用 PBAP。

**当前状态：CP2 蓝牙服务、CP3 OpenAI、CP4 Gemini 和 CP5 完整 CLI / API 已完成软件实现与自动验证。Android 已完成基础通话与 PBAP 部分验收；iPhone 已验证 OpenAI mSBC 双向音频和打断。Gemini 真机验证与 CP6 完整验收仍待完成，详见 [验收记录](docs/acceptance.md)。**

## 架构

```text
CLI / 其他设备 HTTP 请求
          ↓
Linux 后台服务
  ├── 蓝牙适配与手机连接
  ├── HFP：拨号、接听、挂断、DTMF、真实通话状态
  ├── SCO/eSCO：双向通话音频 → 音频桥接
  ├── PBAP：联系人及手机允许访问的历史
  ├── RTS provider：OpenAI Realtime / Gemini Live
  ├── 任务管理与模型工具
  └── SQLite：设备、联系人、历史、任务、结果与事件
```

后台服务独立运行并持有蓝牙连接，CLI 退出后任务继续执行。第一版每部手机同时一通电话，其他任务排队；使用手机当前默认拨号 SIM，不承诺 HFP 双卡线路选择。蓝牙重连不自动重新拨号。

## 规划文档

- [完整项目规格](docs/specification.md)
- [实施顺序和验收清单](docs/roadmap.md)
- [建议模块结构](docs/layout.md)

实现采用 Python、FastAPI、Typer、SQLite，通过 systemd 用户服务常驻运行。已固定 handsfree-linux 上游提交并保留 MIT 许可，见 [复用说明](docs/upstream.md)。

最终交付包括源代码、安装与运行说明、配置示例、CLI 帮助、API 文档，以及 Android / iPhone 的实际验收结果。provider 协议与配置见下方使用文档。

## 运行与验证

- [安装、配对、手动通话与双向音频](docs/install.md)
- [HTTP API / 音频 WebSocket](docs/api.md)
- [完整 CLI 帮助](docs/cli.md)
- [Cloudflare Tunnel 与 4 位 PIN](docs/cloudflare.md)
- [PIN 登录网页与全部设置](docs/web-ui.md)
- [OpenAI 任务配置与操作](docs/tasks.md)
- [Gemini 配置与使用](docs/gemini.md)
- [CP3 软件与协议验证](docs/cp3-verification.md)
- [CP4 软件与协议验证](docs/cp4-verification.md)
- [CP5 CLI / API 验证](docs/cp5-verification.md)
- [软件验证记录](docs/verification.md)
- [CP6 Android / iPhone 真机验收步骤](docs/acceptance.md)

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/agentcall-service --config config.example.toml
# 另一个终端
.venv/bin/phone health
.venv/bin/phone --help
```

HTTP 默认地址 `http://127.0.0.1:8765`，OpenAPI 文档在 `/docs`，网页在 `/ui`。公网入口为 [agentcall.coldt.uk](https://agentcall.coldt.uk)，业务 API 位于 `/api/`，身份验证使用严格 4 位数字 PIN，支持前导零；根路径保留网页入口，网页操作也通过 API。Linux 蓝牙与 PBAP 系统依赖见安装说明。服务通过 asyncio D-Bus 持有 HFP，无桌面 GUI 或 GLib 依赖。
