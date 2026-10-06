# AgentCall

在 Linux 上通过 Android 或 iPhone 的手机 SIM 拨打电话，让实时语音模型完成预设任务。所有业务通过 HTTP API 提供，CLI 作为客户端；不开发图形界面。

项目计划基于 handsfree-linux 扩展，复用蓝牙与联系人实现并将核心功能从 GUI 解耦。通话控制和音频使用纯蓝牙 HFP / SCO，不依赖 ADB、手机 App 或屏幕操作；联系人和手机历史另用 PBAP。

**当前状态：规格与实施规划阶段。尚无可运行服务，Android / iPhone 真机验收均待完成。**

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

建议技术栈：Python、FastAPI、Typer、SQLite，通过 systemd 用户服务常驻运行。上游代码集成前需要确认 handsfree-linux 的具体仓库、版本、许可与复用边界。

最终交付包括源代码、安装与运行说明、配置示例、CLI 帮助、API 文档，以及 Android / iPhone 的实际验收结果。具体 provider 模型、协议与配置将在实现阶段以官方文档核实。
