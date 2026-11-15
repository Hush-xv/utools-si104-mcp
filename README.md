# utools-si104-mcp

**English**: An MCP (Model Context Protocol) server that wraps the UTools-SI104 USB-to-SPI/I2C adapter SDK (Lanmotion) into 14 AI-callable tools — device enumeration, SPI full-duplex and long transfers, I2C master transactions and register access, bus scanning, slave buffers & mailbox FIFOs, channel recovery, and VCC power control. Stateless stdio design (device opened per call, co-exists with the vendor GUI), MCP-spec structured output and tool annotations, hardware-validated semantics. Setup and hardware notes below are in Chinese.

把 UTools-SI104（USB 转 SPI/I2C 适配器，Lanmotion）SDK 封装成 MCP 服务器，让 ZCode / Claude Code / Codex / DeepSeek 等 AI 客户端直接驱动硬件：设备枚举、SPI 全双工收发与长发送、I2C 主机事务与寄存器读写、总线扫描、从机缓冲与邮箱 FIFO、通道恢复、VCC 供电诊断与控制——**14 个工具覆盖 SDK 全部 API**。

协议日志走 stderr，stdout 专用于 MCP 协议。

## 特性

- **双传输无状态**：stdio（AI 客户端）+ 可选 HTTP（网页控制台）；每次工具调用原子化 `open → 操作 → close`，只在调用期间占用 dongle（毫秒级），与 uTools 主程序共存，无需常驻进程。同一时间只服务一个客户端，并发会撞 USB 互斥（瞬时 `-2/-3` 服务端已内置 ≤2 次 100ms 自动重试，仅重试 open、绝不重试总线操作；极端并发仍可能报错，重试即可）
- **全 API 覆盖**：14 个工具，含 `si104_raw_request` 万能透传兜底（固件私有命令）、`si104_vcc_control` 供电控制与 `si104_i2c_control` 通道恢复
- **结构化错误**：`{error, code, code_meaning, backend_code, firmware_status, completion_unknown}`；`code_meaning` 按 SDK Guide §08 官方错误码表给出静态含义，**`completion_unknown=true` 时总线操作可能已发生，禁止盲目重发**（附 `retry_hint`）
- **文档对齐**：容量上限（i2c TX≤504、寄存器读≤512 但自增按 256 页翻页、寄存器写≤504 含指针字节、raw request 请求/回复各≤512、SPI TX≤504、长发送 ≤1024 word）、速率预设（mode 0/1/2 = 100k/400k/1M，`bus_hz` 仅 mode=3 生效）、状态解码（SPI_TX_STATUS BB5HI、I2C GET_STATUS 32 B）均按官方《SDK Guide》/《Technical Manual》实现（容量与错误码已对照 SDK 1.0.5 官方文档复核）
- **可诊断**：每调用关联 ID（`#N`）+ `[param]/[device]/[internal]` 分级日志；负载只记长度不记内容

## MCP 规范化

- **结构化输出**：14 个工具声明 `outputSchema`（pydantic 模型，字段全可选、可空），成功调用同时返回 `structuredContent` 与等价的 text JSON——旧客户端不受影响，强类型客户端可直接按 schema 解析；诊断性附加字段经 `extra="allow"` 透传，不破坏校验。未赋值的可选字段在 `structuredContent` 中为 `null`（schema 已声明可空，pydantic dump 行为）
- **工具注解与显示名**：`si104_enumerate / device_info / read_vcc` 标注 `readOnlyHint=true`；扫描会重配通道并读取目标，不标记为只读；`vcc_control / spi_control` 标注 `idempotentHint=true`；`i2c_control` 标注 `destructiveHint=true`（reset 清配置）；`raw_request` 有意不标注（万能透传不作承诺）；全部工具带 `title`
- **错误语义**：参数校验失败与设备级失败均以 `isError=true` 返回（MCP 规范建议）；结果文本仍是完整结构化 JSON（SDK 会在其前加 `Error executing tool <tool>: ` 前缀，剥离后 `json.loads` 即得全部字段）；`completion_unknown` 语义不变
- **输入侧 schema**：全部参数带 `Field` 描述（模型选工具时看到的是 inputSchema 而非 docstring），枚举类取值用 `Literal` 收紧——`action`×3、`data_bits`(长发送 8/16/32)、I2C `mode`(0-3)，错值在 schema 层就被拦住；运行时校验保留作纵深防御

## 安全注意事项

- 所有工具入参在进入 SDK 前均做范围/格式校验；schema 层另有枚举约束
- USB 单句柄互斥即天然限流：并发调用者收到瞬时 `-2/-3`，服务端已内置退避重试（见特性节）
- 敏感操作（使能供电 `si104_vcc_control`、通道复位 `si104_i2c_control`、万能透传 `si104_raw_request`）建议客户端在调用前弹出人工确认——这是客户端职责（MCP 规范 Security Considerations），服务端不代劳
- 协议日志只记负载长度不记内容，总线数据不会泄入日志

## 前置条件

- UTools-SI104 dongle
- 官方 SDK 包内的 Python wheel（内置原生 DLL；**SDK 本体不在本仓库分发**，许可按官方条款自备）
- Python ≥ 3.10

## 安装

wheel 与 MCP 依赖必须装进**同一环境**（server 用哪套 python 启动，就用哪套的 si104）：

```bat
:: 1) 下载仓库并创建专用 Python 环境
git clone https://github.com/Hush-xv/utools-si104-mcp.git
cd utools-si104-mcp
python -m venv .venv
:: 2) 官方 SDK wheel 与本项目安装到同一个环境（SDK 包需自备）
.venv\Scripts\python.exe -m pip install <sdk包>\python\wheels\utools_si104-1.0.5-py3-none-win_amd64.whl
.venv\Scripts\python -m pip install .
```

Linux 同理（`.venv/bin/python`；Linux 还需系统 libusb）。

### macOS（Apple Silicon）免编译运行

仓库内置 arm64 预编译库（libusb 1.0.29 + SI104 SDK 1.0.5，见 `runtime/macos-arm64/README.md`），无需 Homebrew：

```bash
R=$PWD/runtime/macos-arm64
export SI104_SDK_LIBRARY=$R/libsi104_sdk.dylib
export SI104_LIBUSB=$R/libusb-1.0.dylib
# SI104 Python 绑定来自 Lanmotion SDK 包(未随本仓库分发):
# PYTHONPATH 指向其 python/ 目录,或安装其 wheels/utools_si104-*.whl
```

首次运行若被 Gatekeeper 拦截:`xattr -dr com.apple.quarantine runtime/macos-arm64/*.dylib`。

## 14 个工具

| 工具 | 用途 |
|---|---|
| `si104_enumerate` | 列出 USB 上的 SI104 dongle（serial/vid/pid） |
| `si104_device_info` | 只读诊断：身份/固件/解码 channel status/运行时计数/capabilities/limits（六项逐项独立降级） |
| `si104_spi_transfer` | 原子 SPI 全双工事务（TX≤504 B，`data_bits>8` 须整字对齐） |
| `si104_spi_tx_words` | SPI 长发送（BEGIN/DATA/COMMIT 流式，8/16/32 bit，≤1024 word，无回读） |
| `si104_spi_control` | abort / tx_status（BB5HI 状态解码） |
| `si104_i2c_transfer` | 原子 I2C 主机事务（写后读重复 START）；`monitor_vcc=true` 附 vcc 字段 |
| `si104_i2c_read_regs` | 主机寄存器读：指针相位 + 重复 START（实测验证）；`reg_addr_16bit=true` 时 reg 0..65535，指针 2 字节 MSB-first |
| `si104_i2c_write_regs` | 主机寄存器写：指针 + 数据一条事务；16-bit 寄存器地址实测可用 |
| `si104_i2c_scan` | 7-bit 总线扫描；NACK 表示该地址未应答，超时/设备级错误中止并返回 `aborted_at`、`found_before_abort` 与完整错误字段 |
| `si104_i2c_control` | I2C 通道恢复：abort 取消在途事务（0x14）/ reset 复位通道并清配置（0x15），复位后需重配 |
| `si104_i2c_slave` | `configure` 明确重配从机地址、模式与数据格式并返回状态；read/write 操作原始缓冲，push/pop 操作邮箱 FIFO（push≤256 B/pop≤512 B）；缓冲操作复用匹配配置，不覆盖现有数据 |
| `si104_raw_request` | vendor request 透传（EVENT_POLL=0x1B 等无专属工具的命令） |
| `si104_read_vcc` | VCC 供电状态（`0x1F` 空查询，免总线流量） |
| `si104_vcc_control` | 使能/关断共享 VCC 输出（`0x1E`，NO_POWER 恢复专用），回读 `0x1F` 确认设备侧状态 |

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

### 调试（MCP Inspector）

不接客户端也能可视化查看工具、schema 并手动调用：

```bat
npx @modelcontextprotocol/inspector .venv\Scripts\python.exe server.py
```

### 网页控制台（HTTP）

不经任何客户端，浏览器直接操作设备——与 AI 客户端共用同一套工具、校验和错误通道：

```bash
# Linux/macOS
SI104_MCP_TRANSPORT=http .venv/bin/python server.py     # 默认 http://127.0.0.1:8800
```
```bat
:: Windows (cmd)
set SI104_MCP_TRANSPORT=http && .venv\Scripts\python.exe server.py
```

打开 http://127.0.0.1:8800：选择设备与 I2C 通道，在主机、从机和 SPI 工作区操作；从机寄存器模式可只配置地址与指针位宽，操作结果在右侧显示完整 HEX。每路通道的表单草稿独立保存，实际状态可通过“读取四路状态”刷新。VCC 为所有 I2C 通道共享，事务记录仅保留在当前页面会话中。

可双击 Windows 的 `start-windows.cmd`，或运行 `.venv/bin/python si104_launcher.py`（macOS/Linux）。启动器检查本机 `/health`：已有 SI104 服务则复用，未启动则启动服务并等待就绪，然后打开浏览器。端口由其他程序占用或 SDK 加载失败时会报告原因或日志路径。安装后的 `si104-console` 命令使用同一入口。`/health` 只返回服务身份和就绪状态，不访问 USB。macOS 从仓库源码启动时可自动定位仓库附带的 arm64 库；wheel 不包含 `runtime/`，从其他目录安装后请显式配置原生库路径。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SI104_MCP_TRANSPORT` | `stdio` | 设为 `http` 启用网页控制台 |
| `SI104_MCP_HOST` | `127.0.0.1` | 监听地址；绑非回环地址会在日志大声警告 |
| `SI104_MCP_PORT` | `8800` | 监听端口 |
| `SI104_MCP_TOKEN` | 未设 | 设置后 `/mcp` 要求 `Authorization: Bearer <token>`，控制台页面顶部粘贴即可 |

**安全边界**：默认仅监听本机回环；控制台可执行包括 `si104_raw_request` 在内的全部工具——请勿在不受信任的网络暴露，跨机访问务必设置 `SI104_MCP_TOKEN`。HTTP 与网页回归由 CI 的替身模式验证。

## 使用方法

配好后在任一客户端里说人话，AI 自选工具：

- "列一下 SI104 设备并读 VCC"
- "扫一下 I2C 通道 0" → `si104_i2c_scan`
- "SPI mode0 1MHz 发 `deadbeef` 回读 4 字节" → `si104_spi_transfer`
- "轮询 0x50 的 0x00-0x0F 寄存器" → `si104_i2c_read_regs`
- "dongle 卡死了" → `si104_spi_control(action="abort")` 恢复

不经过 AI 也可直接验证链路：

```bat
.venv\Scripts\python unit_test.py
.venv\Scripts\python smoke_test.py
```

`unit_test.py` 覆盖参数校验、二进制解码器和工具流程；`smoke_test.py`、`http_test.py` 始终使用测试替身，插着真机也不会发送总线事务。真机测试请显式运行 `hw_test.py` 等硬件脚本并确认接线；空总线和卡住的总线需分别复测扫描超时语义。

## 无硬件自测与 CI

仓库自带 `si104_stub.py` 测试替身（内存版 SDK 子集模型，**非固件仿真器**）：设置 `SI104_MCP_STUB=1` 时，即使装有官方 SDK 也只使用替身。`unit_test.py`、`smoke_test.py` 和 `http_test.py` 不访问真机；普通生产启动缺 wheel 时会报错退出并给出安装指引。

CI（`.github/workflows/ci.yml`）：ruff、单元、stdio/HTTP 契约和 Node 网页回归，矩阵 ubuntu/windows × Python 3.10/3.13，不依赖 vendor wheel 与硬件。实机行为仍以显式运行的 `hw_test.py` / `stress_boundary_test.py` 为准。

## 发布清单（维护者）

- **GitHub topics** 建议：`mcp` `model-context-protocol` `i2c` `spi` `usb` `hardware` `ai-tools` `claude-code` `zcode`
- **MCP Registry**：仓库公开后按 [registry 流程](https://modelcontextprotocol.io/registry/about) 提交（需所有权验证）
- **Release**：`v1.1.0` annotated tag 的 message 即发布说明；push 时带 `--tags`
- **传输**：stdio 与可选的 HTTP 模式；HTTP 访问边界见“网页控制台”。

## 边界

- 容量上限各工具不同，详见工具 docstring（AI 可见）；错误信息含参数名与合法范围
- **USB 接口号 = 功能面**（TM §1，实测确认）：0-3 = I2C 通道 0-3，4 = SPI，5 = 维护。I2C 类命令（GET_STATUS 32B、VCC_SET/0x1F 阈值查询）必须发给对应 I2C 接口，发 SPI 面（4）会报 `status=4` 拒绝；`si104_device_info` 已按面分组返回 SPI/I2C 诊断
- **NO_POWER（firmware_status=11）**：共享 VCC 输出默认关闭，I2C 总线无上拉电源时传输报 `status=11`。使能一次即可（掉电前有效）：`si104_vcc_control(channel, enable=true)`——错误 hint 已自动携带此提示；实测使能后 VCC ≈ 3.1 V
- `si104_i2c_slave`：**实测固件在供电期间跨 USB 会话保持通道配置**——一次配置后，主机侧工具可在后续调用里直接访问从机；`configure` 明确重配，缓冲操作复用匹配的角色、地址、逻辑模式和寄存器指针位宽
- `si104_i2c_scan` 实测：能在回环总线上正确扫出从机地址
- **寄存器式访问已实测重建**：绑定 `read_buffer/write_buffer` 对固件 1.3 **已失效**（连本通道镜像都报 `invalid I2C write/read command/length`，文档漂移实锤），工具重建在 `i2c_transfer` 指针相位上；`reg_addr_16bit=true` 时指针 2 字节 MSB-first，reg 0..65535 **实测可用**
- **合并事务语义（实测）**：写后读的写入段对从机逻辑是"命令相位"，不落入从机接收缓冲；mailbox 模式纯写的首字节是指针（其余才入邮箱）
- `si104_i2c_scan` 仅 7-bit（0x08..0x77）；固件状态 7 作为该地址未应答，状态 8 超时或其他设备级错误中断扫描并返回位置及已发现地址
- `si104_device_info` 的 status 解码按 TM §8 32 B 格式；interface=4 之外的接口号映射文档未明确，格式不符降级返回 raw_hex
- 参数入口校验前移：channel 0-3、mode 0-3、7/10-bit 地址域、timeout_ms u16（0=固件默认 1000）、custom 模式 bus_hz 10k-1M、SPI sclk_hz 50k-40M、cpol/cpha ∈ {0,1}（SDK Guide §06）
- **长发送完成态跨 USB 会话保持（真机实测）**：`tx_status` 报告最后一次完成记录（如 done_ok + 计数）而非 idle，且 abort 只清在途上传、不清完成记录——断电或新 BEGIN 才会重置；`si104_spi_control` 的 docstring 已按真机行为修正
- `si104_raw_request` 请求与回复各 ≤512 B（SDK Guide §07）；508 是 SI104 自身寄存器镜像写（`i2c_reg_write`）的限制，两者勿混淆。固件 1.3 上已实测失效的 `i2c_reg_read/write/reply` 由主机寄存器工具使用原子事务替代；`ping` 等未封装命令可经 raw_request 访问

## 官方错误码（SDK Guide §08）

结构化错误里的 `code` 遵循 SDK 统一错误码，`code_meaning` 字段即下表含义：

| code | 含义 |
|---|---|
| -1 | 参数错误 |
| -2 | 未找到设备 |
| -3 | 接口或设备忙 |
| -4 | 超时 |
| -5 | USB 断开 |
| -6 | 回复格式错误 |
| -7 | 不支持 |
| -8 | USB 或其他 IO 错误 |
| -9 | 缓冲不足 |
| -10 | 设备拒绝（`firmware_status` 给出固件侧原因，如 7=NACK、8=超时、11=NO_POWER） |
| -11 | 多设备需指定序列号 |

`completion_unknown=true` 表示总线操作可能已经发生：先核对从机状态，禁止盲目重发（SDK Guide §08）。

## 许可

- 本仓库代码：[MIT](LICENSE)
- UTools-SI104 SDK / 固件：Lanmotion 版权所有，需自备授权 SDK 包；libusb 为 LGPL-2.1-or-later（见 SDK 包内 `THIRD_PARTY_NOTICES.txt`）
