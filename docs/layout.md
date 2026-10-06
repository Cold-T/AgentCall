# 建议目录结构

以下为实现阶段的目标结构，当前仓库只初始化项目文档。

```text
src/
  bluetooth/   # handsfree-linux 适配、HFP、PBAP
  audio/       # SCO、编解码、重采样、双向桥接
  providers/   # 统一接口、OpenAI、Gemini
  tasks/       # 状态、执行、模型工具
  storage/     # SQLite 模型和查询
  api/         # HTTP API、SSE
  cli/         # 命令行客户端
  service/     # 启动、配置、生命周期
tests/
config.example.toml
```

Python 包命名和具体目录在实现时确定。蓝牙代码若依赖 GLib 主循环，通过独立线程或事件循环接入后台服务。
