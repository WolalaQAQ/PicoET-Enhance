<div align="center">

# PicoET-Enhance

**为 PICO 4 Pro 解锁真实瞳孔直径、左右眼独立表情、90/90 Hz 高频眼嘴追踪、头显端后处理调节与局域网相机广播。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-v2.7.0-green.svg)](https://github.com/WolalaQAQ/PicoET-Enhance/releases)
[![Platform](https://img.shields.io/badge/PICO%204%20Pro-PICO%20OS%205.13.7-orange.svg)](#环境要求)
[![Releases](https://img.shields.io/github/v/release/WolalaQAQ/PicoET-Enhance)](https://github.com/WolalaQAQ/PicoET-Enhance/releases)

[English](README.md) | **简体中文**

[主要改进](#它带来什么改进) · [快速安装](#快速安装) · [配套应用](#使用配套应用) · [PC Studio](#pc-studio) · [常见问题](#常见问题) · [进阶与开发](#命令行与进阶配置) · [社区](#反馈与社区)

</div>

---

**PicoET-Enhance** 是面向 **PICO 4 Pro**（`PICO OS 5.13.7`，Magisk，SELinux Enforcing）的运行期眼动增强模块。它在内存中 hook `pxreyetrackingservice`，不修改任何系统文件，也不包含任何 PICO 二进制；同时附带免 Root 配套应用，用于头显端后处理调节与局域网相机广播。

## 它带来什么改进

PICO 4 Pro 的眼部相机以 90 Hz 采集双目画面，但原厂固件隐藏了真实瞳孔直径、在输出表情前把左右眼混合，并把表情更新频率限制在约 24 Hz。PicoET-Enhance 在运行期直接解决这三点：

- **真实瞳孔直径**：打开 `pxreyetrackingservice` 内部的逐眼企业版门控，把真实瞳孔直径写入共享内存（不触碰整机属性 `ro.pxr.externalfunc`）。
- **左右眼独立表情**：解除表情模型的跨眼耦合。`EyeLook`、`Blink`、`Wide` 与眉部通道逐眼独立计算和滤波，每眼维护各自的质量窗口、参考图与历史状态，并通过 `EPI1` v2 扩展导出右内眉及逐眼质量状态。
- **默认 90/90 Hz 眼嘴追踪**：眼部与嘴部表情推理在同一帧上并行执行，对每个符合接纳条件的新相机输入实时计算。平滑滤波、嘴部放松基线学习与质量窗口均按实际源时间戳（`Δt`）自适应推进。
- **头显端后处理调节与局域网相机广播**：附带免 Root 的 **PicoET-Enhance** 配套应用，可调节或旁路 PICO 原厂后处理的 44 个参数，也可将嘴部、左眼、右眼相机画面广播到局域网，接入 **PC Studio**、Cymple 或任意 MJPEG 客户端。

原厂推理模型、融合视线、暗图接纳、参考图保护和模型量化保持不变。增强逻辑全部驻留在服务内存中，眼动服务重启即干净卸载。

## 在追踪链路中的位置

```mermaid
flowchart LR
    A["PicoET-Enhance<br/>（头显 Magisk 模块）"] -->|"EPI1 v2 + 模式属性"| B["PicoFacialBridge 0.5.0+<br/>（头显端应用）"]
    B -->|"UDP 9030（X v2 数据包）"| C["UnifiedPicoModule 0.4.0+<br/>（PC 端 VRCFT 模块）"]
    C --> D["VRCFaceTracking<br/>（或 VRCFT-Enhance）"]
    D -->|"OSC"| E["VRChat"]
```

[PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) 会自动读取 PicoET-Enhance 的运行期模式属性，通知 PC 端的 [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule) 切换至增强模式。PC 端眼球方向与眼睑开合来自 `EyeLook` 和 `1 − Blink`，原生眼数据仅用于提供实测瞳孔直径。

> [!TIP]
> **想在 VRChat 里跑满接近 90 Hz 的 OSC？** 官方 VRCFaceTracking 以固定 10 ms 间隔轮询并发送 OSC，在 Windows 11 上实际输出常落到约 61–78 Hz。搭配 [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance)（默认 4 ms 间隔、定时器精度与并发修复）可将眼嘴 OSC 输出提升至约 87–88 Hz。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| **头显** | PICO 4 Pro |
| **固件** | PICO OS 5.13.7（Android 10 / API 29，`arm64-v8a`） |
| **Root** | 已安装 Magisk（支持 SELinux Enforcing） |
| **配套软件** | [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) `0.5.0+` 与 [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule) `0.4.0+`（支持 90 Hz `EPI1`/`X` v2） |

安装脚本会在刷入前校验头显原厂眼动库的 SHA-256，固件不匹配时自动中止，避免误装。

## 快速安装

1. 从 [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases) 下载 `magisk-picoet-enhance-v2.7.0.zip`。
2. 在头显的 **Magisk** 应用中选择 **模块 → 从本地安装** 刷入，或通过 ADB 安装：

   ```sh
   adb push magisk-picoet-enhance-v2.7.0.zip /data/local/tmp/
   adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.7.0.zip'
   ```

3. **重启头显。**

首次安装后自动以 **90/90 Hz** 增强模式（`dual/on`）启动，并在头显应用库中出现 **PicoET-Enhance** 配套应用。若升级前已手动切到普通模式，升级后会保持关闭状态。

重启后可在 Root Shell 中核对状态（可选）：

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

看到 `phase=ready applied_mode=dual applied_gate=on effective_eye_hz=90 effective_mouth_hz=90` 即已正常生效。

## 使用配套应用

从头显应用库打开 **PicoET-Enhance**。标题栏并排提供 **局域网广播** 与 **后处理** 两个标签页，以及一键中英文切换。配套应用与 PicoFacialBridge 均不需要 Root 或网络权限，应用仅在自身 Android 沙盒内向模块后台工作进程提交固定请求。

### 后处理标签页

PICO 原厂固件在表情神经网络输出之后内置了多层后处理（眉部曲线与联动、眨眼端点补齐、自适应 / 复合 / Kalman 滤波、嘴部放松残留扣除、弱嘴形 log 压缩、伸舌触发门槛与平滑）。**PicoET-Enhance 不会在原厂之外额外叠加一层后处理**，而是把 **PICO 自身内置后处理算法的 44 个参数** 开放出来供你调节或关闭；默认值与原厂完全一致。

- **「开始」页提供 4 套预设**：
  - **均衡（推荐）**：介于原厂与灵敏之间，适度减轻眼、眉、嘴和舌头的平滑，让轻微瞪眼、眨眼、眼神、眉毛与嘴形更跟手，同时保留较稳的防抖。减弱眉部联动与弱嘴形压缩，取消嘴部二次平滑，加快放松残留学习，将伸舌门槛降至 `0.35`；保留眨眼补齐、残留扣除与双唇合拢补偿。
  - **灵敏**：仅保留约一半平滑并关闭眉部联动，动作约 0.1 秒内跟上模型（静止时能看到细小抖动）。
  - **不处理**：跳过上述原厂数值后处理，直接输出限幅在 `[0, 1]` 的模型结果（逐眼质量、参考图与有效位仍生效；并非原始相机图像，也不跳过神经网络本身）。
  - **原厂**：PICO 原值，适合对照或在此基础上逐项微调。
- **15 个照镜子动作页**：左侧列出眨眼、转眼、抬眉、说话、伸舌等 15 个常见动作。每页绘制固定 90 Hz 算例曲线，直观对比 **模型给出**（灰线）与 **Avatar 收到**（绿线），拖动滑块时曲线立即重算。
- **简单 / 专业说明切换**：顶部可在面向调参现象的「简单」说明与包含变量、公式和条件的「专业」说明之间切换，不影响任何参数。
- **精细调节与全部设置**：拖动滑块粗调，点击两侧 `◀` / `▶` 单步微调，点击数值可精确输入或恢复单项默认。「全部设置」按执行顺序列出全部 44 个参数。
- **保存与应用流程**：
  1. 修改先存入草稿；点底栏 **保存** 写入 `/data/local/tmp/picoet-postprocessing.conf`（保存不会重启追踪）。
  2. 先停止 **PicoFacialBridge** 及其他眼追 / 面捕客户端。
  3. 点底栏 **应用** 重启 `pxreyetrackingservice` 使新参数生效（仅在无客户端连接且增强模式开启时重启）。

已保存配置在模块升级后会保留。若想启用新版本中更新过的预设参数，重新选择该预设后再 **保存** 并 **应用** 即可。

### 局域网广播标签页

打开 **广播总开关** 后，头显通过局域网提供三路红外相机画面（`400×400` 灰度 JPEG，质量 95）：

| 画面 | 默认端口 | MJPEG 地址 |
| --- | ---: | --- |
| **嘴相机** | `45681` | `http://<头显地址>:45681/stream.mjpg` |
| **左眼相机** | `45682` | `http://<头显地址>:45682/stream.mjpg` |
| **右眼相机** | `45683` | `http://<头显地址>:45683/stream.mjpg` |

- **按需启动、后台常驻**：广播默认关闭并记住选择；关闭配套应用界面不影响后台服务。仅在有客户端订阅时才会拉起相机，关闭总开关会安全排空并断开所有连接。
- **首次开启提示**：首次附加相机适配器需要眼追服务处于空闲状态，请先关闭其他眼追 / 面捕客户端一次，待界面显示 **已开启 · 等待客户端** 后即可恢复正常使用。
- **结果共享（可选）**：**嘴部**（`PSL1`）与 **双眼**（`PSE1`）两个独立开关允许开发者工具订阅数值追踪结果。两项默认关闭，不影响相机画面或 PicoFacialBridge。

> [!WARNING]
> 局域网发现、相机图像与结果流均为明文且无身份认证，请仅在可信的私人局域网内开启 **局域网广播**。

## PC Studio

[`pc-studio`](pc-studio) 是原生 Windows 桌面工具（Rust + Tauri v2 单文件可执行程序），用于局域网实时预览头显相机并将双眼画面转发给 **Cymple**。运行环境需 **Windows 10/11（x64）** 与 **Microsoft Edge WebView2 Runtime**（Windows 11 与大多数已更新的 Windows 10 默认内置；LTSC 或精简系统如缺失可从微软官网下载）。

- **安装程序（推荐）**：从 [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases) 下载 `PicoET-PC-Studio-Setup-v2.7.0.exe`。默认装至 `%LOCALAPPDATA%\Programs\PicoET PC Studio`（无需管理员权限），并创建开始菜单快捷方式。
- **绿色便携包**：解压 `picoet-pc-studio-v2.7.0.zip` 后直接运行 `PicoET-PC-Studio.exe`。便携版配置保存在程序同级的 `user-data/` 目录（安装版保存在 `%LOCALAPPDATA%\PicoET-PC-Studio\user-data\`）。
- **源码运行**（需 Rust `>= 1.90`、MSVC Build Tools 与 Edge WebView2 Runtime）：

  ```powershell
  cargo run --manifest-path pc-studio/src-tauri/Cargo.toml
  ```

主要功能：
- 自动发现局域网内的头显（即使 PC 默认路由指向 VPN 也能发现），或手动输入地址。
- 切换实时预览嘴部、左眼、右眼画面，并直接复制各路 MJPEG 地址供 VLC、FFmpeg、OBS 或自研脚本使用。
- **Cymple 眼部转发**：将左右眼 `PTJ1` 画面转发至 Cymple 原生 UDP 入口（`127.0.0.1:23303`），支持自定义端口及每只眼睛独立的旋转与镜像。
- **系统托盘与完整退出**：关闭主窗口时最小化到 Windows 系统托盘，保持眼部转发在后台运行；通过托盘右键菜单或应用内 **退出** 按钮即可停止转发并完整退出。
- 内置 GitHub Releases 更新检查（直达新版 PC Studio 与头显模块下载链接）。

## 关闭与卸载

- **临时切回原厂模式**：在 Magisk 中点击模块的 **操作（Action）** 按钮，或在 Root Shell 执行 `sh /data/adb/modules/picoet-enhance/picoet.sh off`。眼动服务会重启为未注入的原厂状态。
- **完全卸载**：在 Magisk 中卸载 **PicoET-Enhance** 并重启头显。配套应用、运行期状态文件及 `/data/local/tmp/picoet-postprocessing.conf` 会随模块一并清理。

## 常见问题

<details>
<summary><strong>点击「应用」失败，提示有追踪客户端正在连接</strong></summary>

应用后处理参数或切换模式都需要重启 `pxreyetrackingservice`。请先关闭 **PicoFacialBridge** 与其他眼追 / 面捕客户端，再点击 **应用**。命令行下 `dual` / `off` / `toggle` 支持加 `--force` 强制重启，而 `reload-processing` 会主动拒绝在有客户端连接时执行。

</details>

<details>
<summary><strong>开启「局域网广播」提示先关闭其他眼追、面捕客户端</strong></summary>

相机适配器首次附加要求眼动服务处于空闲状态。短暂关闭 Bridge 等追踪应用，待界面显示 **已开启 · 等待客户端** 后，再重新连接 Bridge 即可同时使用。

</details>

<details>
<summary><strong>重启后增强模式没有自动生效</strong></summary>

运行 `sh /data/adb/modules/picoet-enhance/picoet.sh status`，并查看 `/data/local/tmp/picoet-hook.log` 与 `/data/adb/picoet-enhance/*.log`。如果自动注入因耗尽恢复预算而暂停，排查原因后执行 `picoet.sh dual`（或点击 Magisk 的 **操作** 按钮）即可重置预算并重新开启。

</details>

<details>
<summary><strong>PicoET 已跑在 90 Hz，为什么 VRChat 里的眼追实际只有 60–75 Hz？</strong></summary>

官方 VRCFaceTracking 使用固定 10 ms 休眠，在 Windows 11 下常被拉长到约 15 ms。改用 [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance) 可将 OSC 输出提升至约 87–88 Hz。

</details>

---

## 命令行与进阶配置

在头显 Root Shell（`adb shell su -c ...`）中可使用以下控制命令：

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual      # 同义词：on
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh toggle
sh /data/adb/modules/picoet-enhance/picoet.sh reload-processing
```

| 命令 | 作用 |
| --- | --- |
| `status` | 查看保存与实际生效的模式 / 门控、注入阶段、服务 PID、`effective_eye_hz` / `effective_mouth_hz`、`ro.pxr.externalfunc`、最近日志与连接客户端数 |
| `dual`（`on`） | 启用增强模式（`dual/on`：瞳孔门控 + 逐眼独立表情）并重启 `pxreyetrackingservice` |
| `off` | 切回原厂模式（`off/off`），重启服务且不注入 |
| `toggle` | 增强已 `ready` 时切到 `off`，否则尝试启用 `dual` |
| `reload-processing` | 校验 `/data/local/tmp/picoet-postprocessing.conf` 并在增强开启且无客户端连接时重启服务加载（不接受 `--force`） |

有客户端连接时，`dual` / `off` / `toggle` 需显式加 `--force`。Magisk 的 **操作** 按钮等价于 `picoet.sh toggle --force`。

<details>
<summary><strong>自定义眼部与嘴部目标频率（可选）</strong></summary>

眼部与嘴部默认均为 **90 Hz**。如需降低其中任一项，将 1–90 的正整数写入对应文件，停止追踪客户端后重新运行 `picoet.sh dual` 生效：

| 文件 | 默认值 | 说明 |
| --- | ---: | --- |
| `/data/local/tmp/picoet-eye-hz` | `90` | 眼部表情目标频率；`90` 表示处理每个符合接纳条件的新帧 |
| `/data/local/tmp/picoet-mouth-hz` | `90` | 嘴部表情目标频率；上限受眼部频率约束（每个嘴帧同时计算眼部） |

示例 — 保持眼部 90 Hz、嘴部降至原厂 24 Hz：

```sh
echo 90 > /data/local/tmp/picoet-eye-hz
echo 24 > /data/local/tmp/picoet-mouth-hz
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

删除两个文件并再次执行 `picoet.sh dual` 即可恢复 90/90 Hz。自定义频率在升级后保留，普通模式（`off`）不受其影响。

</details>

<details>
<summary><strong>通过命令行写入后处理配置</strong></summary>

配置文件为 `/data/local/tmp/picoet-postprocessing.conf`（`key=value`，支持 `#` 注释，必填 `version=1`，最大 16 KiB）。所有 `.strength` 取值 `0`–`1`（`0` 关闭该环节，`1` 保留原厂强度，中间值线性混合）；`eye.enabled` 与 `mouth.enabled` 取值 `0` / `1`。

内置预设位于 [`magisk-picoet-enhance/`](magisk-picoet-enhance/)：
- [`postprocessing-balanced.conf`](magisk-picoet-enhance/postprocessing-balanced.conf)
- [`postprocessing-responsive.conf`](magisk-picoet-enhance/postprocessing-responsive.conf)
- [`postprocessing-raw.conf`](magisk-picoet-enhance/postprocessing-raw.conf)
- [`postprocessing-default.conf`](magisk-picoet-enhance/postprocessing-default.conf)

在停止追踪客户端后，可通过 Root Shell 写入并应用：

```sh
MOD=/data/adb/modules/picoet-enhance
"$MOD/bin/picoet-config" write < "$MOD/postprocessing-balanced.conf"
sh "$MOD/picoet.sh" reload-processing
```

`picoet-config` 支持 `read`、`write`、`check`、`defaults`、`balanced`、`responsive`、`raw`。非法值、重复键或未知字段会被拒绝保存；启动时若发现文件损坏或属主权限异常，会明确报错而非静默回退。

</details>

<details>
<summary><strong>自动注入与故障恢复机制</strong></summary>

开机后 `service.sh` 等待 `sys.boot_completed=1`，并通过 Binder `dumpsys` 确认 `pxreyetrackingservice` 的三项原厂算法（`ET`、`FT`、`IPD`）状态行均已响应后才执行注入。建议确认 `picoet.sh status` 返回 `phase=ready applied_mode=dual applied_gate=on` 后再启动追踪客户端。

- **瞬时重试（退出码 `3`）**：暂时无安全执行线程或准备阶段遭遇临时信号中断时，看护循环会释放控制锁并冷却 30 秒后重试，不消耗恢复预算。
- **恢复预算**：不安全失败（退出码 `4` / `5` 或异步回滚失败）在每次开机或显式模式命令周期内共享 **1** 次自动重启恢复机会。
- **稳定性预算**：最多对 **3** 个连续不稳定的服务实例尝试自动注入；仅当同一实例连续保持 `ready` 满 60 秒后才会重置计数。

完整的模块目录、状态文件路径与生命周期说明见 [`magisk-picoet-enhance/README_zh.md`](magisk-picoet-enhance/README_zh.md)。

</details>

## 技术细节

- **企业版门控**：固件通过 `PXR::EyeUtil::isBusinessDev()` 控制逐眼输出（含真实瞳孔直径）。本模块仅在 `pxreyetrackingservice` 进程内使其返回 `true`，不修改 `ro.pxr.externalfunc`。
- **局域网相机与结果协议**：自动发现（`PICO_CAMERA_DISCOVER_V1`）、HTTP MJPEG（`/stream.mjpg`）、二进制 `PTJ1`、嘴部结果（`PSL1`）、眼部结果（`PSE1`）及 `PTJIPC5` 共享内存生命周期规范见 [CAMERA-PROTOCOL_zh.md](CAMERA-PROTOCOL_zh.md)。
- **Magisk 模块参考**：脚本清单、状态 / 日志路径与注入保护机制见 [magisk-picoet-enhance/README_zh.md](magisk-picoet-enhance/README_zh.md)。

### 90 Hz 链路实测吞吐

在唤醒状态的 PICO 4 Pro（`PICO OS 5.13.7`）上回放 2,700 帧录制的 90 Hz 相机输入（约 30 秒），经 PicoET-Enhance 表情管线、PicoFacialBridge `0.5.0`、UnifiedPicoModule `0.4.0`、VRCFT-Enhance（4 ms 间隔）与桌面 VRChat 的各环节实测结果：

| 链路环节 | 实测频率 |
| --- | ---: |
| 头显端眼部与嘴部计算 | **89.80 Hz**（2,694 / 2,700 帧；6 帧触发原厂满队列丢弃，零推理失败） |
| Bridge → PC（`X` v2 数据包） | **89.84 Hz** |
| VRCFT-Enhance → VRChat OSC（同源眼 + 嘴） | **87.80 Hz**（2,633 个来源，占输入 97.5%） |

从 `X` 包抵达 PC 到 OSC 发出的中位延迟为 **9.1 ms**（P95 为 **12.5 ms**）。融合视线维持原厂行为；尽管 `EPI1` 携带左右双侧眉部数值，当前 VRCFT 宿主侧 Avatar 眉部参数仍对左右取平均。

## 从源码构建

构建依赖：
- **头显模块**：**Python 3**、**JDK 17+**（`JAVA_HOME`）、**Android SDK Platform 35** 与 **Build-Tools 35.0.0**（`ANDROID_HOME`）、**NDK 26.1.10909125** 以及 **CMake 3.22.1 / Ninja**。
- **PC Studio**：**Rust / Cargo `>= 1.90`**、**Visual Studio / MSVC Build Tools**（Windows x64）、**Microsoft Edge WebView2 Runtime**、**Node.js**（前端 JS 检查）以及 **Inno Setup 6**（构建 Windows 安装程序）。

```powershell
# 1. 构建注入器、hook 载荷、配置工具与面板后台
pwsh -File hook\build-hook.ps1

# 2. 构建 Android 局域网相机适配器、垫片、控制器与 IPC 守护进程
python camera-source/build.py android `
  --ndk "$env:ANDROID_HOME/ndk/26.1.10909125" `
  --cmake "$env:ANDROID_HOME/cmake/3.22.1/bin/cmake.exe" `
  --ninja "$env:ANDROID_HOME/cmake/3.22.1/bin/ninja.exe"

# 3. 构建配套 APK 并打包 artifacts/magisk-picoet-enhance-v<版本>.zip
pwsh -File build-picoet-enhance.ps1 -Python python
```

配套 APK 直接调用 Android SDK 工具编译，不依赖 Gradle 或外部 Android 库。打包阶段会执行 ELF 白名单校验并确认包内不含任何 PICO 二进制。

- **签名**：本地构建默认自动生成并复用开发签名；正式分发请通过 `-PanelKeystore`（或 `PICOET_PANEL_KEYSTORE`）、可选 `-PanelKeyAlias`（默认 `panel`）与 `PICOET_PANEL_PASSWORD` 提供持久签名，升级必须沿用同一签名。推送 `v<版本>` 标签触发 GitHub Actions 发布时，需配置仓库 Secrets `PICOET_PANEL_KEYSTORE_BASE64` 与 `PICOET_PANEL_PASSWORD`。
- **PC Studio 测试与打包**：执行 `python pc-studio/check.py` 运行锁定依赖的 Cargo 单元测试与 Node.js 前端校验；执行 `python pc-studio/package_release.py` 构建独立 Rust / Tauri 可执行程序、绿色 ZIP 与 Inno Setup 6 安装包（加 `--skip-installer` 可仅打包绿色 ZIP）。

## 后续计划

- [ ] **舌头方向增强** — 原厂仅输出标量 `TongueOut`，而 VRCFaceTracking 支持 `TongueLeft/Right/Up/Down` 等方向通道。计划基于局域网相机采集下脸原始帧，结合视觉分割基模自动标注并训练轻量模型，同时支持用户本地采集微调。

## Pico-Enhance 生态

| 项目 | 作用 |
| --- | --- |
| [**PicoET-Enhance**](https://github.com/WolalaQAQ/PicoET-Enhance) | 本仓库 — 真实瞳孔、左右眼独立、90/90 Hz、头显端后处理调节与局域网相机广播 |
| [**PicoFacialBridge**](https://github.com/WolalaQAQ/PicoFacialBridge) | 头显端桥接应用，通过局域网 UDP 转发 PICO 眼追与面捕数据 |
| [**UnifiedPicoModule**](https://github.com/WolalaQAQ/UnifiedPicoModule) | PC 端 VRCFaceTracking 接收模块（支持 `X` v2 协议） |
| [**VRCFT-Enhance**](https://github.com/WolalaQAQ/VRCFT-Enhance) | 优化 Windows 下 VRCFaceTracking 调度间隔，实现约 88 Hz OSC 输出 |
| [**VD-PicoEnhance**](https://github.com/WolalaQAQ/VD-PicoEnhance) | Virtual Desktop 增强模块（手追透视、码率 / 分辨率解锁、体验优化与指虎热切换） |
| [**PicoRegionSwitch**](https://github.com/WolalaQAQ/PicoRegionSwitch) | PICO 4 Pro 国区与全球应用集热切换工具 |
| [**PicoUSB-Quiet**](https://github.com/WolalaQAQ/PicoUSB-Quiet) | 抑制 PICO 反复弹出的 USB 设备授权弹窗 |

## 反馈与社区

欢迎在 [Issues](https://github.com/WolalaQAQ/PicoET-Enhance/issues) 提交问题或建议，请附上头显型号、PICO OS 版本、模块版本、`picoet.sh status` 输出以及相关日志片段。

使用求助、交流和版本更新通知，欢迎加入 Pico-Enhance 社区。

<a href="https://discord.gg/ueSEydeVD8"><img src="https://img.shields.io/badge/Discord-Join%20Pico--Enhance-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="加入 Pico-Enhance Discord"></a>

**QQ 群：** 1129699813，用 QQ 扫描下方二维码即可加入。

<img src="assets/community/qq-group-qr.png" alt="Pico-Enhance QQ 群二维码" width="240">

## 许可证

本项目原创代码与脚本采用 [MIT 许可证](LICENSE)。内置的 **ShadowHook 2.0.1** 包含 MIT / BSD 许可依赖，相机传输模块内置 **libjpeg-turbo**，PC Studio 静态链接 Rust / Tauri v2 依赖（包含 MIT、Apache-2.0、BSD、Zlib、Unicode-3.0 与未修改的 MPL-2.0 crates）；所有第三方声明均随 Magisk ZIP 与 PC Studio 安装包一并分发并汇总于 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目不包含也不分发任何 PICO 二进制。
