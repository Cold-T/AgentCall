# AgentCall

通过 Linux 蓝牙连接 Android 或 iPhone，使用手机 SIM 拨打电话，让实时语音 AI 完成你设定的任务。

AgentCall 是独立运行的后台服务，提供网页、CLI 和 HTTP API。通话控制使用 HFP，双向音频使用 SCO/eSCO，联系人与手机历史通过 PBAP 同步；无需 ADB、手机 App 或屏幕自动化。

**当前版本：v1.0.0。** [发布下载](https://github.com/Cold-T/AgentCall/releases) · [安装指南](docs/install.md) · [任务配置](docs/tasks.md) · [API 文档](docs/api.md) · [报告问题](https://github.com/Cold-T/AgentCall/issues)

## 功能

- 手机发现、配对确认、连接、断开与蓝牙重连。
- 手动拨号、接听、挂断、DTMF，以及基于手机指示器的实际通话状态。
- OpenAI Realtime 与 Gemini Live 任务通话：目标、背景资料、完成条件、结构化结果与对话打断。
- 每部手机按顺序执行任务；关闭网页或 CLI 后，后台任务继续运行。
- 联系人、手机历史、任务事件、转写和结果保存到 SQLite；AI 通话录音与转写可下载。
- 网页查看历史与生成结果总结，CLI 支持 JSON 输出，API 提供实时事件和双向音频 WebSocket。
- systemd 用户服务、Bearer 认证，以及可选的 PIN 登录与 HTTPS 远程部署。

```text
网页 / CLI / HTTP 客户端
          │
      Linux 后台服务 ─── SQLite / 录音
          ├── OpenAI Realtime / Gemini Live
          └── 蓝牙 HFP + SCO/eSCO + PBAP
                         │
                   Android / iPhone
                         │
                      手机 SIM
```

## 运行要求与支持范围

- Linux、Python 3.11+、BlueZ、D-Bus，以及支持 HFP / SCO 的蓝牙适配器。
- 手机需要支持 HFP Audio Gateway；mSBC 还需要 `libsbc` 和控制器的 transparent SCO 支持。
- AI 通话需要对应服务商的 API 密钥、模型访问权限及网络连接。API 使用和手机通话可能产生费用。

| 平台 / 服务 | 当前验证范围 |
| --- | --- |
| Android | 配对、HFP、拨号、挂断与基础音频路径已验证；PBAP 传输已验证，内容核对与完整 AI 通话验收待完成。 |
| iPhone + OpenAI | 已完成一次真实 mSBC 双向 AI 通话与插话验证；来电、DTMF、PBAP、重连及完整任务结束流程仍需验收。 |
| Gemini Live | 已实现并通过本地协议与模拟通话测试；真实 API 和手机通话验收待完成。 |

以上结果不代表所有手机、系统版本或蓝牙控制器均兼容。详细证据见 [Android 记录](docs/android-acceptance.md)、[iPhone 记录](docs/iphone-acceptance.md) 与 [验收清单](docs/acceptance.md)。

每部手机同时只允许一通电话，使用手机当前默认拨号 SIM；不提供 HFP 双卡线路选择。蓝牙重连不会重新拨号，服务重启不会恢复正在执行的通话任务。同一适配器的 SCO 音频建立由服务串行协调；一部手机仍在响铃并等待音频时，可能延迟另一部手机的音频建立。v1.0 未验收多手机并发通话，应逐部手机发起通话；控制器也可能限制并行 SCO。

## 快速开始

以下以 Debian / Ubuntu 和源码安装为例。其他发行版、无桌面 PBAP 与 systemd 配置见 [安装指南](docs/install.md)。

```bash
sudo apt install git bluez bluez-obexd python3-venv libsbc1 dbus
sudo systemctl enable --now bluetooth

git clone https://github.com/Cold-T/AgentCall.git
cd AgentCall
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
```

修改 `config.toml` 中的蓝牙适配器、codec 与默认模型配置。默认监听 `127.0.0.1:8765`，codec 为 CVSD；使用 mSBC 时设置 `codec = "msbc"`。在启动服务的环境中设置 `OPENAI_API_KEY` 或 `GEMINI_API_KEY`，再启动：

```bash
.venv/bin/agentcall-service --config config.toml
```

另一个终端中检查服务：

```bash
.venv/bin/phone health
.venv/bin/phone --help
```

`health.ready=true` 表示蓝牙后端就绪；仅能打开网页并不证明 HFP 或通话音频已经可用。启动服务和连接手机不会自动拨号。

### 连接手机

打开手机蓝牙并保持可发现状态。配对时核对两端显示的数值，再确认请求：

```bash
.venv/bin/phone watch
# 在另一个终端执行；用手机蓝牙地址替换 DEVICE
.venv/bin/phone scan start
.venv/bin/phone devices
.venv/bin/phone pair DEVICE
.venv/bin/phone confirm REQUEST_ID
.venv/bin/phone scan stop
.venv/bin/phone connect DEVICE
.venv/bin/phone devices
```

等待设备的 `hfp_ready=true`。联系人同步可用 `phone sync DEVICE`，首次需要手机端授权；直接号码拨打不依赖联系人同步。

### 发起 AI 通话

编辑 [examples/task.json](examples/task.json)，填写手机地址、你有权拨打的号码、目标和完成条件。示例默认只保存，启动命令才会实际拨号：

```bash
.venv/bin/phone task create --file examples/task.json
.venv/bin/phone task start TASK_ID --key UNIQUE_REQUEST_KEY
.venv/bin/phone task show TASK_ID
.venv/bin/phone task cancel TASK_ID
```

使用返回的任务 ID 替换 `TASK_ID`。网页发起通话会立即创建并排队执行任务，提交前请核对号码。更多示例见 [任务指南](docs/tasks.md) 与 [Gemini 指南](docs/gemini.md)。

## 网页、CLI 与 API

| 入口 | 地址 / 命令 |
| --- | --- |
| 本机网页 | `http://127.0.0.1:8765/ui`；提供设备连接、发起通话与历史查看。 |
| CLI | `phone --help`、`phone task --help`；`phone --json …` 输出 JSON。 |
| HTTP API | `http://127.0.0.1:8765`；交互式文档位于 `/docs`，schema 位于 `/openapi.json`。 |
| 远程 CLI | 通过 `AGENTCALL_URL` 和 `AGENTCALL_TOKEN` 指定服务地址与认证。 |

自行部署可使用自己的域名，维护者部署实例不是安装或运行的必需入口。公网配置见 [Cloudflare / HTTPS 部署指南](docs/cloudflare.md)。

## 模型、语言与配置

默认配置见 [config.example.toml](config.example.toml)，Gemini 配置见 [config.gemini.example.toml](config.gemini.example.toml)。任务可选择 provider、模型、声音、语言与允许的模型参数；可用模型和参数以服务商及账户权限为准。

OpenAI 新建通话按语言自动设置语速：**英文 `1.0`，中文 `1.2`**（含普通话和粤语）。其他语言保留配置的语速；Gemini 不使用 OpenAI 的 `speed` 参数。已保存任务使用创建时的配置快照，默认设置变更只影响新任务。

后台区分模型报告结果、执行结束原因与手机实际状态。任务指令要求先完成必要的口头交流，再提交真实结果、致谢告别并挂断；模型摘要或转写不能独立证明目标达成，应结合原始记录核对。

## 认证与数据

默认仅监听本机回环地址。对外开放前配置认证与 HTTPS；非回环监听要求设置 `AGENTCALL_TOKEN`。HTTP API 和音频 WebSocket 支持 Bearer 认证，网页登录使用会话 cookie；固定 PIN 模式及限流配置见 [网页指南](docs/web-ui.md) 与 [部署指南](docs/cloudflare.md)。

模型密钥由后台读取，不返回客户端。请将密钥、token 和 PIN 放在私有环境或配置文件中，不提交到 Git；systemd 环境文件建议权限为 `0600`。数据库、联系人、历史、转写与录音存储在服务主机，需自行管理访问权限、备份和保留期限。

AI 通话会将对方语音和任务资料发送给选定的模型服务商，历史结果总结也可能调用模型 API。拨号、录音及上传资料前，请取得适用的授权，遵守当地规则与服务商条款。

## 文档

| 主题 | 文档 |
| --- | --- |
| 安装、配对、音频、systemd 与故障排查 | [安装指南](docs/install.md) |
| 网页、PIN、声音选择与通话历史 | [网页指南](docs/web-ui.md) |
| CLI 命令与 JSON 输出 | [CLI 指南](docs/cli.md) |
| HTTP、WebSocket、事件与任务接口 | [API 文档](docs/api.md) |
| OpenAI、任务规则、语言与语速 | [任务指南](docs/tasks.md) |
| Gemini 配置与协议适配 | [Gemini 指南](docs/gemini.md) |
| 自托管公网入口 | [Cloudflare / HTTPS 部署](docs/cloudflare.md) |
| 软件验证与手机兼容性 | [v1.0 发布审查](docs/release-v1.0.0.md) · [软件验证](docs/verification.md) · [真机验收](docs/acceptance.md) |
| 实现结构与上游来源 | [模块布局](docs/layout.md) · [复用说明](docs/upstream.md) |

## 开发与贡献

欢迎通过 [Issues](https://github.com/Cold-T/AgentCall/issues) 报告问题或提交 Pull Request。涉及蓝牙问题时，请提供 Linux / 内核、BlueZ、手机系统、适配器、codec 与可复现步骤，并删除日志中的号码、联系人、凭据和私人通话内容。

```bash
.venv/bin/pip install -r requirements-dev.lock -e '.[test]'
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts --output-format concise
.venv/bin/pytest -q
```

软件测试使用临时 D-Bus、模拟手机和本地 WebSocket，不实际拨号；mSBC 测试需要 `libsbc`。CI 覆盖 Python 3.11 和 3.13。真实 API 验证脚本会产生用量，应按对应指南显式运行。浏览器验证另需 `browser-test` extra 和 Chromium，见 [网页指南](docs/web-ui.md)。

## 许可证与第三方归属

AgentCall 自有代码采用 [MIT 许可证](LICENSE)，版权声明为 **Copyright (c) 2026 Cold-T**。

AgentCall 复用并改造了 [handsfree-linux](https://github.com/PavelTarlev1/handsfree-linux) 的部分实现，上游为 MIT 许可证，版权归 **Pavel Tarlev（2024）**。完整上游版权及许可保存在 [LICENSE.handsfree-linux](src/agentcall/vendor/LICENSE.handsfree-linux)，随 Python 包分发；复用范围与固定提交见 [复用说明](docs/upstream.md)。复制或分发相应代码时须保留该版权及许可声明。

再分发源代码、安装包或相应代码的实质部分时，请保留 AgentCall 与上游适用的版权声明及完整许可文本。第三方依赖仍适用各自许可证。
