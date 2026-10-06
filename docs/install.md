# 安装与运行

要求 Linux、Python 3.11+、支持 HFP / SCO 的蓝牙适配器、BlueZ 和用户 session D-Bus。手机需要作为 HFP Audio Gateway；不依赖 ADB、手机 App 或屏幕自动化。

## Debian / Ubuntu

```bash
sudo apt install bluez bluez-obexd python3-venv libsbc1 pulseaudio-utils dbus
sudo systemctl enable --now bluetooth
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
.venv/bin/agentcall-service --config config.toml
```

`libsbc1` 供 mSBC 使用；默认 `codec="cvsd"`。`pulseaudio-utils` 的 pacat 只用于 CLI 本地麦克风 / 扬声器验收，服务直接使用 SCO socket。包名在不同发行版上可能不同。

如果系统缺少 ensurepip，安装匹配 Python 版本的 venv 包。也可以先 `python3 -m venv --without-pip .venv`，再用已有系统 pip：`python3 -m pip --python .venv/bin/python install -e .`，不会写系统 site-packages。

`phone health` 查看真实后台状态。BlueZ 不可用时 HTTP 服务仍运行，但 `ready=false`，设备控制返回 503；服务会重试蓝牙注册。不要将 HTTP 能访问当成蓝牙已经就绪。

PBAP 通过当前用户的 session bus 访问 obexd，可先执行 `systemctl --user start obex`；不同发行版可能使用 `obexd` / `bluez-obexd` 单元。不可用和权限拒绝会返回具体错误，直接号码拨打不依赖 PBAP。

### 无桌面主机的 OBEX

BlueZ 5.83 的 obexd Bluetooth 服务插件通过 logind 检查用户是否有活动 seat。在只有 SSH / linger 的无桌面主机上，可能出现 `No transport driver registered` 和 `obex_server_init failed`；只启动用户服务不能解决。它的本地电话簿服务插件还可能需要 Evolution 数据源，和读取手机联系人无关。

可显式部署 system bus 模式；AgentCall 仍以普通用户运行。默认 `obex_bus="session"` 不变，也不会自动尝试提权或切换 bus。以下步骤需要管理员授权：

1. 检查 `/usr/libexec/bluetooth/obexd --help` 支持 `--system-bus`；若安装路径不同，修改 `deploy/agentcall-obex.service`。BlueZ 5.83 还存在 PBAP 使用私有 D-Bus 连接的上游缺陷，即使 `CreateSession` 成功，`PhonebookAccess1.Select` 仍返回 UnknownMethod；需使用包含 [上游修复](https://github.com/bluez/bluez/commit/df0036d9e41fc4bb0fe8839b7833bac16359396b) 的 obexd。本项目真机环境使用独立构建的 5.87，系统 bluetoothd 保持 5.83。
2. 将 `deploy/agentcall-obex.conf.example` 中 `AGENTCALL_USER` 替换为实际服务用户。该策略只允许 root 持有 OBEX 名称、该用户向 OBEX 发送请求；这也授予该用户访问 obexd 其他可用客户端功能的权限。
3. 将替换后的文件安装到 `/etc/dbus-1/system.d/agentcall-obex.conf`，将单元安装到 `/etc/systemd/system/agentcall-obex.service`，然后执行：

   ```bash
   sudo systemctl reload dbus
   sudo systemctl daemon-reload
   sudo systemctl enable --now agentcall-obex
   busctl --system introspect org.bluez.obex /org/bluez/obex org.bluez.obex.Client1
   ```

4. 在服务配置 `[service]` 下设置 `obex_bus="system"`，重启 AgentCall，再执行 `phone sync DEVICE`。手机 PBAP 授权仍需确认。OBEX 客户端接口就绪不代表手机同步成功，应检查同步返回及实际联系人。

示例单元仅加载 Bluetooth、Object Push 和 filesystem 服务插件以满足 obexd 初始化要求，不依赖 Evolution、不启用自动接受文件。它仍注册 Object Push 服务，应仅在受控主机上使用。回退时将 `obex_bus` 改回 `session`、停用 `agentcall-obex`、删除上述策略及单元、重新加载 D-Bus / systemd，再重启 AgentCall。

若发行版只有受影响的 5.83，可单独构建官方 5.87 obexd，不覆盖包管理器文件，也不执行 `make install`。以下命令在 Ubuntu 25.10 aarch64 上构建：

```bash
sudo apt install libglib2.0-dev libdbus-1-dev libical-dev libreadline-dev libtool autoconf automake
curl -fLO https://www.kernel.org/pub/linux/bluetooth/bluez-5.87.tar.xz
echo '26bdcf2cebd7310c6f598850606b037ef0c515fe6608ebc54d22c50c4c32b35f  bluez-5.87.tar.xz' | sha256sum -c -
tar -xf bluez-5.87.tar.xz
cd bluez-5.87
./configure --disable-systemd --disable-udev --disable-tools --disable-client --disable-monitor --disable-manpages --disable-cups --disable-mesh --disable-midi --disable-testing --disable-datafiles
make obexd/src/builtin.h
make -j4 obexd/src/obexd
sudo install -D -m 755 obexd/src/obexd /usr/local/libexec/bluetooth/obexd-5.87
```

将系统单元的 ExecStart 路径改为 `/usr/local/libexec/bluetooth/obexd-5.87`，保留原参数，再重新加载单元并重启 `agentcall-obex`。`--disable-client` 指 BlueZ 的交互式命令行程序，不会移除 obexd 的 PBAP 客户端。回退时停用自建服务并删除独立二进制即可，发行版 obexd 未被覆盖。SHA256 是本次下载工件的固定哈希，下载使用官方 HTTPS 源。

## 配对、连接与手动通话

```bash
.venv/bin/phone watch
# 另一个终端
.venv/bin/phone scan start
.venv/bin/phone devices
.venv/bin/phone pair AA:BB:CC:DD:EE:FF
# 配对事件包含 id 和六位 passkey；核对手机显示的数值后，第三个终端确认
.venv/bin/phone confirm REQUEST_ID
# 拒绝时加 --reject
.venv/bin/phone scan stop
.venv/bin/phone connect AA:BB:CC:DD:EE:FF
.venv/bin/phone devices
.venv/bin/phone dial TEST_NUMBER --device AA:BB:CC:DD:EE:FF
.venv/bin/phone calls --current
.venv/bin/phone dtmf CALL_ID '12*#'
.venv/bin/phone hangup CALL_ID
.venv/bin/phone sync AA:BB:CC:DD:EE:FF
.venv/bin/phone contacts
.venv/bin/phone calls
```

用自己的测试号码替换 `TEST_NUMBER`；拨号请求会实际使用手机当前默认 SIM。`ConnectProfile` 返回表示请求被手机接受，`devices.hfp_ready=true` 才表示 HFP 握手完成。拨号的 AT `OK` 也不表示接通；接通必须看到实际 `call.state=active`。

来电时用 `phone calls --current` 获取 ID，再 `phone answer CALL_ID`。首次 PBAP 权限在手机端授予；iPhone 的蓝牙设备详情可能需要开启联系人同步。

## 双向音频

接通后 `GET /calls/CALL_ID` 的 `audio.ready=true` 表示已建立 SCO，包含 codec、sample_rate、format、mtu、pending_ms、rx_bytes、tx_bytes。音频链路失败会发出 `audio.error`；不要把仅有通话控制的成功当作音频成功。

```bash
.venv/bin/phone audio CALL_ID --live --seconds 60
```

CLI 启动 pacat，将当前系统麦克风和扬声器接到服务的全双工音频 WebSocket；退出音频命令不会挂断电话。服务本身不依赖 pacat、PipeWire 或 PulseAudio。

也可使用原始 s16le 单声道 PCM 文件探针，采样率必须与 `audio.sample_rate` 相同：

```bash
.venv/bin/phone audio CALL_ID --input outbound.pcm --output inbound.pcm --seconds 30
```

手动音频 WebSocket 使用 SCO 原生 PCM 格式；AI 任务由服务自动桥接至模型 24kHz 音频，配置与命令见 [OpenAI 任务说明](tasks.md)。服务接受可变长度块，只按实际 MTU 或 mSBC 必要编码帧分帧，不增加固定时长 PCM 批次。不实现独立 VAD、插话策略或主动清空音频缓存。

## HFP 冲突与适配器

handsfree-linux、WirePlumber 或其他进程若已占用 HFP HF UUID，BlueZ 注册可能返回 AlreadyExists / NotPermitted。停止重复运行的 handsfree-linux；根据上游 [WirePlumber 配置说明](https://github.com/PavelTarlev1/handsfree-linux/blob/main/docs/wireplumber-setup.md) 调整 HF 角色。服务不会自动停止桌面音频服务。

若适配器未启用，可用 `busctl --system set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Powered b true`。配置中的 `adapter` 必须对应实际控制器。

mSBC 需要 libsbc 和支持 transparent SCO 的控制器。只有成功 HFP codec 确认后才使用相应 BT_VOICE；协商后出错不会静默按另一种 codec 解码。多部手机的音频 socket 建立会串行协调，实际并行 SCO 能力取决于控制器，真机验收需另行确认。

## 远程访问

通过 `agenticcall.coldt.uk` 的 Cloudflare Tunnel、固定 PIN 和未来 UI 路径预留部署，见 [Cloudflare 部署说明](cloudflare.md)。该方案本机 HTTP 回源，公网 HTTPS；必须先启用并验证固定 PIN 后再发布。

默认仅监听 `127.0.0.1:8765`。若改为非 loopback 地址，必须设置 `AGENTCALL_TOKEN`（或配置 `token_env` 指定的环境变量）。HTTP 和音频 WebSocket 均使用 `Authorization: Bearer TOKEN`。客户端也读取 `AGENTCALL_TOKEN`；其他业务配置和模型 API 密钥不返回客户端。

```bash
export AGENTCALL_TOKEN='YOUR_TOKEN'
export AGENTCALL_URL='http://LINUX_HOST:8765'
.venv/bin/phone --json devices
```

OpenAI / Gemini 密钥由后台服务读取，配置见 [OpenAI 任务](tasks.md) / [Gemini](gemini.md)。远程 CLI 只需要 AGENTCALL_TOKEN，不需要模型密钥。

## systemd 用户服务

`deploy/agentcall.service` 默认使用 `%h/AgentCall/.venv`。若目录不同，先修改 WorkingDirectory / ExecStart。

```bash
mkdir -p ~/.config/agentcall ~/.config/systemd/user
cp config.example.toml ~/.config/agentcall/config.toml
cp deploy/agentcall.service ~/.config/systemd/user/agentcall.service
# 按需将 AGENTCALL_TOKEN、OPENAI_API_KEY、GEMINI_API_KEY 写入 ~/.config/agentcall/environment
# 建议 chmod 600 ~/.config/agentcall/environment
systemctl --user daemon-reload
systemctl --user enable --now agentcall
journalctl --user -u agentcall -f
```

需要退出用户会话后继续运行时，配置 `loginctl enable-linger "$USER"`。CLI 关闭不影响独立服务及手机连接。

## 软件验证

```bash
.venv/bin/pip install -r requirements-dev.lock -e '.[test]'
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts --output-format concise
.venv/bin/pytest -q
```

D-Bus 测试使用独立的临时 dbus-daemon，不接触系统 BlueZ，也不会实际拨号。mSBC 测试要求 libsbc；缺少时会明确 skip。测试验证模拟 AG 与软件路径，不替代 Android / iPhone 真机验收。
