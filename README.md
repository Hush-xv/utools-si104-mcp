# utools-si104-mcp

把 UTools-SI104（USB 转 SPI/I2C 适配器，Lanmotion）SDK 封装成 MCP 服务器，让 ZCode / Claude Code / Codex / DeepSeek 等 AI 客户端直接驱动硬件：设备枚举、SPI 全双工收发与长发送、I2C 主机事务与寄存器读写、总线扫描、从机缓冲、VCC 供电诊断——**12 个工具覆盖 SDK 全部 API**。

协议日志走 stderr，stdout 专用于 MCP 协议。

## 特性

- **stdio 无状态**：每次工具调用原子化 `open → 操作 → close`，只在调用期间占用 dongle（毫秒级），与 uTools 主程序共存，无需常驻进程。同一时间只服务一个客户端，并发会撞 USB 互斥（后到者报 `SI104 not found`，重试即可）
- **全 API 覆盖**：12 个工具 + `si104_raw_request` 万能透传兜底（固件私有命令）
- **文档对齐**：容量上限（i2c TX≤504、寄存器读≤256/写≤508、SPI TX≤504、长发送 ≤1024 word）、速率预设（mode 0/1/2 = 100k/400k/1M，`bus_hz` 仅 mode=3 生效）、状态解码（SPI_TX_STATUS BB5HI、I2C GET_STATUS 32 B）均按官方《Technical Manual》/《SDK Guide》实现
- **结构化错误**：`{error, code, backend_code, firmware_status, completion_unknown}`；**`completion_unknown=true` 时总线操作可能已发生，禁止盲目重发**（附 `retry_hint`）
- **可诊断**：每调用关联 ID（`#N`）+ `[param]/[device]/[internal]` 分级日志；负载只记长度不记内容

## 前置条件

- UTools-SI104 dongle
- 官方 SDK 包内的 Python wheel（内置原生 DLL；**SDK 本体不在本仓库分发**，许可按官方条款自备）
- Python ≥ 3.10

## 安装

wheel 与 MCP 依赖必须装进**同一环境**（server 用哪套 python 启动，就用哪套的 si104）：

```bat
:: 1) 安装官方 SDK wheel（自备 SDK 包；Linux 换 linux_x86_64.whl）
pip install <sdk包>\python\wheels\utools_si104-1.0.5-py3-none-win_amd64.whl

:: 2) 本仓库
git clone https://github.com/Hush-xv/utools-si104-mcp.git
cd utools-si104-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Linux 同理（`.venv/bin/python`；Linux 还需系统 libusb）。

## 12 个工具

| 工具 | 用途 |
|---|---|
| `si104_enumerate` | 列出 USB 上的 SI104 dongle（serial/vid/pid） |
| `si104_device_info` | 只读诊断：身份/固件/解码 channel status/运行时计数/capabilities/limits（六项逐项独立降级） |
| `si104_spi_transfer` | 原子 SPI 全双工事务（TX≤504 B，`data_bits>8` 须整字对齐） |
| `si104_spi_tx_words` | SPI 长发送（BEGIN/DATA/COMMIT 流式，8/16/32 bit，≤1024 word，无回读） |
| `si104_spi_control` | abort / tx_status（BB5HI 状态解码） |
| `si104_i2c_transfer` | 原子 I2C 主机事务（写后读重复 START）；`monitor_vcc=true` 附 vcc 字段 |
| `si104_i2c_read_regs` | 读寄存器（reg 8-bit，≤256 B） |
| `si104_i2c_write_regs` | 写寄存器（reg 8-bit，≤508 B） |
| `si104_i2c_scan` | 7-bit 总线扫描；设备级错误（code -2/-3/-5/-9/-11）中断并返回 `found_before_abort` |
| `si104_i2c_slave` | 从机缓冲读/写（raw/mailbox/register）；固件在供电期间保持通道配置，工具复用匹配配置不覆盖 |
| `si104_raw_request` | vendor request 透传（VCC_SET=0x1E、EVENT_POLL=0x1B 等无专属工具的命令） |
| `si104_read_vcc` | VCC 供电状态（`0x1F` 空查询，免总线流量） |

二进制数据一律小写 hex 字符串（`"deadbeef"`，可带 `0x` 前缀/空格）；`serial=None` = 第一台 dongle。

## 客户端接入

配置里的路径换成实际安装路径；配好重开客户端会话即可看到 `mcp__si104__*` 工具。

### ZCode

用户级 `~/.zcode/cli/config.json`（项目级 `<repo>/.zcode/config.json` 同理，键 `mcp.servers`）：

```json
{
  "mcp": {
    "servers": {
      "si104": {
        "command": "<repo>\\mcp-server 目录的绝对路径\\.venv\\Scripts\\python.exe",
        "args": ["<repo>\\...\\utools-si104-mcp\\server.py"]
        }
    }
  }
}
```

### Claude Code

```bat
claude mcp add si104 -- <venv>\Scripts\python.exe <repo>\server.py
```

### Codex

`~/.codex/config.toml`：

```toml
[mcp_servers.si104]
command = "<venv>\\Scripts\\python.exe"
args = ["<repo>\\server.py"]
```

### DeepSeek

App 的 MCP 设置选自定义（stdio）：命令 = `<venv>\Scripts\python.exe`，参数 = `<repo>\server.py`。网页版够不到本机进程，需本机 App 版。

## 使用方法

配好后在任一客户端里说人话，AI 自选工具：

- "列一下 SI104 设备并读 VCC"
- "扫一下 I2C 通道 0" → `si104_i2c_scan`
- "SPI mode0 1MHz 发 `deadbeef` 回读 4 字节" → `si104_spi_transfer`
- "轮询 0x50 的 0x00-0x0F 寄存器" → `si104_i2c_read_regs`
- "dongle 卡死了" → `si104_spi_control(action="abort")` 恢复

不经过 AI 也可直接验证链路：

```bat
.venv\Scripts\python smoke_test.py
```

期望输出 `SMOKE OK`：12 工具注册 + 无 dongle 时结构化错误路径。插上 dongle 后建议按顺序跑一遍实机验证：`device_info`（对照 limits）→ `i2c_scan` → SPI 回环（MOSI-MISO 短接，8/16/32 bit × CS 极性矩阵）→ `read_vcc` 与 `raw_request(0x1F)` 互证。

## 边界

- 容量上限各工具不同，详见工具 docstring（AI 可见）；错误信息含参数名与合法范围
- **USB 接口号 = 功能面**（TM §1，实测确认）：0-3 = I2C 通道 0-3，4 = SPI，5 = 维护。I2C 类命令（GET_STATUS 32B、VCC_SET/0x1F 阈值查询）必须发给对应 I2C 接口，发 SPI 面（4）会报 `status=4` 拒绝；`si104_device_info` 已按面分组返回 SPI/I2C 诊断
- **NO_POWER（firmware_status=11）**：共享 VCC 输出默认关闭，I2C 总线无上拉电源时传输报 `status=11`。使能一次即可（掉电前有效）：`si104_raw_request(channel, 0x1E, '0100')`——错误 hint 已自动携带此提示；实测使能后 VCC ≈ 3.1 V
- `si104_i2c_slave`：**实测固件在供电期间跨 USB 会话保持通道配置**——一次配置后，主机侧工具可在后续调用里直接访问从机；工具对已初始化且匹配（role=1 + 同地址 + 同逻辑模式）的通道复用配置不覆盖，不匹配才重配
- `si104_i2c_scan` 实测：能在回环总线上正确扫出从机地址
- 寄存器地址 8-bit（0..255）：SDK helper 命令域仅 8 bit，>0xFF 寄存器地址暂不可表达，待硬件核实 16-bit flag 语义后经 `raw_request` 补
- `si104_i2c_scan` 仅 7-bit（0x08..0x77）；协议无独立 NACK 码，超时/IO 类错误按"该地址无设备"处理，设备级错误中断扫描
- `si104_device_info` 的 status 解码按 TM §8 32 B 格式；interface=4 之外的接口号映射文档未明确，格式不符降级返回 raw_hex
- 参数入口校验前移：channel 0-3、7/10-bit 地址域、timeout_ms u16（0=固件默认 1000）、custom 模式 bus_hz 10k-1M、sclk_hz 正数

## 许可

- 本仓库代码：[MIT](LICENSE)
- UTools-SI104 SDK / 固件：Lanmotion 版权所有，需自备授权 SDK 包；libusb 为 LGPL-2.1-or-later（见 SDK 包内 `THIRD_PARTY_NOTICES.txt`）
