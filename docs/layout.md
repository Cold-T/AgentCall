# 当前目录结构

```text
src/agentcall/
  bluetooth/   # BlueZ D-Bus、配对、HFP SLC、PBAP
  audio/       # SCO socket、双向 PCM、mSBC 分帧
  storage/     # SQLite 联系人、手机历史、项目通话、事件
  api/         # HTTP API、SSE、音频 WebSocket、PIN 网页会话与设置
    web/       # 原生 HTML / CSS / JavaScript，无前端构建依赖
  cli/         # HTTP 客户端、实时音频探针
  service/     # 配置、生命周期、设备重连
  providers/   # OpenAI Realtime / Gemini Live
  tasks/       # 执行状态、队列、模型工具和结果
  vendor/      # 固定上游 AT / mSBC 模块及 MIT 许可
tests/         # 协议、PBAP、持久化、D-Bus、真实网络验证
deploy/        # systemd 用户服务示例
docs/          # 规格、安装、API、软件验证、真机验收
config.example.toml
requirements-dev.lock
```

音频模块包含格式转换与重采样。当前使用 asyncio 和 dbus-next，无 Qt / GLib 依赖。蓝牙设备管理适配可替换，协议核心与 HTTP / CLI / 网页分离。
