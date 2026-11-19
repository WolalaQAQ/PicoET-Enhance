# PicoET-Enhance

[English](README.md) | 简体中文

面向 PICO 4 Pro 的眼动增强 Magisk 模块：打开眼动服务的逐眼门控以输出真实瞳孔数据，解除面部表情模型的跨眼耦合让左右眼各自计算，并把眼部与嘴部表情目标频率从原厂约 24 Hz 提高到默认 90/90 Hz。配合 [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) 和 [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule) 使用。

> v2.6.0-dev.2（开发分支）· PICO 4 Pro / PICO OS 5.13.7 · [更新日志](CHANGELOG_zh.md)

**端到端嘴追开发：**`feat/tongue-pc`采用[MouthLink](https://github.com/UndefinedSignal/MouthLink)式整嘴回归：头显取图，PC配置、录制、训练和推理，配套UnifiedPicoModule同名分支合并输出。个人图像、教师预测与学生权重留在用户PC。按[源码构建与测试说明](TONGUE_zh.md)使用；本分支的模块包含可选局域网相机，不附带已验收的通用整嘴权重。

整嘴输出可在模块端选择`Hybrid`（原厂有效新鲜值优先）或`ModelOnly`（完整模型）模式。PC设置页提供按源时间计算的One-Euro平滑，`smoothing_ms=0`逐值直通。见[运行设置](TONGUE_zh.md#3-连通全链路)。

## 它做什么

PICO 4 Pro 的固件会对左右眼分别追踪，但面部表情模型在输出前把两眼混合，一侧的 Blink、视线方向、Wide 和眉部会受另一侧输入影响。眼部相机以 90 Hz 运行，这些表情却每秒只更新约 24 次。本模块在运行期做三处改动：

- **真实瞳孔数据**：打开眼动服务内部的逐眼门控，使真实瞳孔直径数据写入共享内存。
- **左右眼独立**：解除表情模型的跨眼耦合。EyeLook、Blink、Wide 和眉部通道逐眼独立计算与滤波，每眼维护自己的质量窗口、参考图和历史。滤波后的右内眉与逐眼质量状态通过 `EPI1` 扩展导出，由 PicoFacialBridge 以 `X` 包转发。
- **默认眼嘴 90/90 Hz**：眼部与嘴部表情各自在每个符合接纳条件的新相机输入上执行，同一帧的两路推理并行。降低频率是可选项，见[眼部与嘴部频率](#眼部与嘴部频率)。要让高频输出一直传到 VRChat，见[关于 90 Hz OSC](#关于-90-hz-osc)。

平滑、嘴部基线适应和质量窗口都按各自来源的时间戳推进：长时间缺帧会让质量窗口暂停，重复或倒退的来源不推进任何状态。

推理模型和融合视线保持原厂行为。Blink 端点、眼内眉部联动和眼嘴后处理默认保留，可通过[头显端后处理配置](#头显端后处理配置)分别关闭或调整。暗图接纳、参考保护和模型量化仍启用。PC 端眼球方向与眼睑开合使用 EyeLook 和 `1−Blink`，原生眼数据仅提供实测瞳孔直径。

增强通过运行期 hook 作用于眼动服务，随服务重启而卸载。

## 与其他组件的关系

```text
PicoET-Enhance（头显，本模块）
        ↓ 运行期生效，通告当前模式
PicoFacialBridge（头显）
        ↓ BridgeSplit，局域网 UDP 9030
UnifiedPicoModule（PC）
        ↓
VRCFaceTracking → VRChat OSC
```

PicoFacialBridge 读取本模块公布的临时模式属性，自动告知 PC 当前处于普通还是增强模式。

高频路径使用 `EPI1`／`X` v2，需要 PicoFacialBridge 0.5.0 及以上与 UnifiedPicoModule 0.4.0 及以上。此前的 PicoET-Enhance 2.2.0 搭配 PicoFacialBridge 0.4.0、UnifiedPicoModule 0.3.0，通过 v1 提供原厂表情节奏下的逐眼独立。较旧的 Bridge（0.3 及以上）在普通模式下仍可使用，无需本模块。

### 关于 90 Hz OSC

眼部路径送到 PC 的频率约为 90 Hz，但官方 VRCFaceTracking 宿主再把它交给 OSC 时会降频：宿主以固定的 10 ms 等待读取模块输出并写出 OSC。我们在 PICO 4 Pro 经 Virtual Desktop 串流的环境下，按原始 90 Hz 节奏回放 2,700 帧录制的眼部数据，统计 OSC 输出端每秒的新眼部方向数：

| VRCFT 宿主（5.4.5，Steam 版 `7add148`） | 每秒新眼部方向 |
|---|---:|
| 官方原版 | 约 61–78 Hz |
| 打补丁，4 ms 等待 | 约 87–88 Hz |

Windows 11 可能会忽略官方宿主申请的 1 ms 定时器精度，这时每次 10 ms 等待实际要 15 ms 左右，所以官方原版每次测量的结果都有起伏。

以上数据来自 Ryzen 7 9700X、RTX 4070 Ti SUPER、64 GB 内存、Windows 11 专业工作站版，电源计划为卓越性能。其他电脑的结果可能不同。

官方 VRCFaceTracking 搭配本模块可以正常使用。如果希望 OSC 接近 90 Hz，请使用 [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance)。它让宿主和模块进程不被 Windows 忽略定时器精度，把两处固定等待改为可设置项（默认 4 ms），并修正了宿主里几处没有同步的数据交换（等待缩短后它们更容易互相干扰）。它运行在一份独立的 VRCFaceTracking 副本里；Release 提供打好补丁的两个 DLL，仓库里同时保留源码补丁，想自己构建也可以。

## 环境要求

| 项目 | 要求 |
|---|---|
| 头显 | PICO 4 Pro |
| 固件 | PICO OS 5.13.7（Android 10 / API 29，arm64-v8a） |
| Root | 已安装 Magisk |
| 配套组件 | PicoFacialBridge 与 UnifiedPicoModule，用于把数据接入 VRCFaceTracking |

安装脚本会校验设备眼动库的 SHA-256，固件版本不匹配时停止安装。

## 安装

1. 准备 `magisk-picoet-enhance-v<版本>.zip`。本开发分支按下方源码步骤构建；已发布的正式版可从 [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases) 下载。
2. 在 Magisk App 中通过“从本地安装”刷入，或使用命令行：

   ```sh
    adb push magisk-picoet-enhance-v2.6.0-dev.2.zip /data/local/tmp/
    adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.6.0-dev.2.zip'
   ```

3. 重启头显。

首次安装默认启用增强模式 `dual/on`，重启后以 90/90 Hz 运行。如果升级前已切到普通模式，升级后仍保持关闭。

## 使用

从应用库打开 **PicoET-Enhance**。**局域网广播** 标签页显示总开关、服务状态和三路相机的地址及客户端数量。**后处理** 标签页直接显示完整参数面板，头显宽屏下仍从左侧选择要调的内容。

**结果共享** 分别控制 **嘴部** 追踪数值和 **双眼** [表情数值](CAMERA-PROTOCOL.md#eye-expression-results-pse1)的订阅权限，两项默认关闭，不影响图像或 Facial Bridge。

相机共享默认关闭并记住选择。开启后，局域网客户端可自动发现并独立订阅三路图像；有人订阅才启动相机，关闭会断开全部传输，退出GUI不影响后台。首次开启需先关闭其他眼追、面捕客户端一次，以便空闲附加适配器。图像为明文、无认证，仅在可信私网使用。开关不改变后处理参数，普通表情转发仍走Bridge。

在头显上以 Root 运行：

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh toggle
```

| 命令 | 作用 |
|---|---|
| `status` | 显示保存与实际生效的模式、注入状态、服务 PID、眼／嘴有效目标频率及 `ro.pxr.externalfunc` 的当前值 |
| `dual` | 增强模式：同时设置 `dual/on`，打开瞳孔门控并启用逐眼独立表情 |
| `off` | 普通模式：同时设置 `off/off`，重启眼动服务且不注入，回到原版 |
| `toggle` | 增强已就绪时切到普通模式，否则尝试开启增强 |

切换模式会重启眼动服务并断开眼动、面捕客户端；有客户端连接时，命令行需要加 `--force`。Magisk App 的“操作”按钮直接切换增强与普通模式；增强加载失败后，再按会重试开启。

`on` 可作为 `dual` 的同义词。状态文件为 `/data/local/tmp/picoet-mode` 与 `/data/local/tmp/picoet-gate`，日志为 `/data/local/tmp/picoet-hook.log`。

### 自动加载与恢复

自动注入会等待 `sys.boot_completed=1`，并确认同一个眼动服务实例的 Binder 已响应。使用增强模式时，建议先确认 `picoet.sh status` 显示 `phase=ready applied_mode=dual applied_gate=on`，再启动 Bridge 等追踪客户端。

`ready` 表示 hook 已安装且目标频率已配置。如果引擎在 hook 前就已初始化，会保留该引擎，不追补 profile 0 或启动期 ET 配置；增强侧状态会重新热身，不强制重建引擎。

暂时找不到安全执行线程，或准备期间遇到临时信号停止时，注入器返回 `3`。看护脚本会释放控制锁，在锁外等待 30 秒后重试，不消耗两类预算。同步与异步不安全失败共用一次自动恢复机会，每次开机或显式模式命令重新计数。另有独立上限：最多自动注入三个连续不稳定的服务实例，只有同一实例连续保持 `ready` 满 60 秒才补充次数。预算耗尽后，自动注入暂停，直到下次开机或显式执行模式命令。

### 眼部与嘴部频率

默认眼部 90 Hz、嘴部 90 Hz，无需任何配置。要降低其中一项，把正整数写入对应文件：

| 文件 | 默认值 | 作用 |
|---|---:|---|
| `/data/local/tmp/picoet-eye-hz` | 90 | 眼部表情频率；90 表示处理每个符合接纳条件的新相机输入 |
| `/data/local/tmp/picoet-mouth-hz` | 90 | 嘴部表情频率，不能超过眼部频率，因为每个嘴帧同时也会计算眼部 |

大于 90 的值截为 90，文件缺失或内容无效时使用默认值。频率之外的帧会直接跳过而不计算。文件只在服务启动时读取，所以修改后需要先退出 Bridge 等追踪客户端，再运行 `dual` 重启服务来生效。例如眼部保持 90 Hz、嘴部降到原厂的 24 Hz：

```sh
echo 90 > /data/local/tmp/picoet-eye-hz
echo 24 > /data/local/tmp/picoet-mouth-hz
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

`status` 中的 `effective_eye_hz` 和 `effective_mouth_hz` 是当前生效的配置目标，不是实测输出频率。要恢复 90/90，删除两个文件后再次运行 `dual`。

在 2.4.0 及之后版本设置的频率会在重装或升级时保留。只有首次从 2.4.0 之前的版本迁入时，才清除旧覆盖并以 90/90 起步。普通模式 `off` 完全是原厂行为，不受这些文件影响。

### 头显端后处理配置

从应用首页进入 **参数调节**；以下的「开始」、动作清单及保存／应用均位于该独立面板。

PICO原厂会在表情模型输出之后做一系列后处理：眉部曲线与联动、Blink端点补齐、自适应／复合／Kalman滤波、嘴部基线扣除、log压缩、伸舌门槛与平滑等。这些处理原本参数固定。从2.5.0起，本模块把其中44个参数开放为配置项，默认值就是原厂值；处理算法本身仍是原厂的。

模块附带独立的 **PicoET-Enhance** 设置应用，安装模块并重启后从头显应用库打开，卸载模块并重启后随模块一起移除。「开始」页提供四个预设；左侧列出15个可以照镜子检查的动作，例如眨眼、抬眉、说话。每个动作页画出模型给出的值和Avatar收到的值，写明这个动作常见的问题和该拖哪个滑杆，旁边是影响它的设置，拖动时图会重新计算。图中动作按每秒90次更新的固定条件计算，用于比较设置。「全部设置」按处理顺序列出全部44项。顶部可在「简单」和「专业」两种说明之间切换，专业说明给出变量、公式和计算条件，切换不改变任何设置。拖动滑块粗调，点击两侧的◀／▶单步微调；点击数值可精确输入或恢复单项默认，不需要编辑配置文本。面板和PicoFacialBridge均免Root：面板从自己的Android私有目录提交固定请求，由模块后台执行。VRCFT模块只处理PC端数据。

配置文件为 `/data/local/tmp/picoet-postprocessing.conf`，采用 `key=value`，支持 `#` 注释。`version=1` 必填，省略其他字段使用原有默认值。所有 `.strength` 均为 0–1：0 关闭该步骤，1 保留原有强度，中间值在当前输入与处理结果之间混合；`eye.enabled`、`mouth.enabled` 用 0／1 旁路／启用整个区域。

- **均衡（推荐）**：[`postprocessing-balanced.conf`](magisk-picoet-enhance/postprocessing-balanced.conf) 在原厂和灵敏之间折中，适度减轻眼、眉、嘴和舌头的平滑，让轻微瞪眼、眨眼、转眼、眉毛和嘴形更跟手，同时比灵敏保留更多防抖。减弱眉部联动与弱嘴形压缩，取消嘴部二次平滑，加快放松残留的学习，伸舌门槛为0.35。保留眨眼补齐、残留扣除和双唇合拢补偿；细小动作仍比原厂更明显。未列出的项使用默认值。
- **灵敏**：[`postprocessing-responsive.conf`](magisk-picoet-enhance/postprocessing-responsive.conf) 保留约一半平滑并关闭眉部联动，所有动作约0.1秒内跟上模型；停住时能看到细小抖动。眨眼补齐、残留扣除和MouthClose补偿保留。
- **不处理**：使用 [`postprocessing-raw.conf`](magisk-picoet-enhance/postprocessing-raw.conf)，输出模型当前值的限幅结果，跳过上述原厂后处理。逐眼质量、参考图、源身份、有效位及 0–1 输出范围仍保留，不代表原始相机图像或停用模型内部处理。
- **原厂／逐项调整**：[`postprocessing-default.conf`](magisk-picoet-enhance/postprocessing-default.conf) 列出全部 44 个参数的PICO原值。`adaptive.k` 越大越跟手；`current_weight` 越大越偏向当前样本；Kalman 的 process noise 增大或 measurement noise 减小，通常会减弱平滑。关闭后噪声和动作跳变会更明显；PC／VRCFT 自己的滤波仍独立生效。

底栏区分未保存、已保存待应用和本次已应用；修改未保存时不能直接应用。重新读取保存值本身不代表运行配置已经核实。保存不重启追踪；先停止 Bridge 及其他眼追／面捕客户端，再点击「应用」，无客户端占用且增强已开启时才重启眼追服务。也可在头显 Root shell 中运行：

```sh
MOD=/data/adb/modules/picoet-enhance
"$MOD/bin/picoet-config" write < "$MOD/postprocessing-balanced.conf"
sh "$MOD/picoet.sh" reload-processing
```

修改在下一次载荷启动时读取，重装／升级保留配置。要采用更新后的预设，需重新选择该预设，再保存、应用。非法参数、重复或未知字段拒绝保存；启动时发现文件损坏或权限不符，会报告失败而非默默恢复滤波。无配置文件保持原有行为。头显原生数值、面板开机集成及免Root通信的保存／应用／恢复已在PICO 4 Pro／PICO OS 5.13.7／SELinux Enforcing下验证；佩戴响应与噪声仍待比较。

## 关于企业版门控

固件通过 `PXR::EyeUtil::isBusinessDev()` 控制逐眼输出，包括真实瞳孔直径。本模块在眼动服务内将该函数的返回值设为真；整机企业模式属性 `ro.pxr.externalfunc` 保持原值。

## 兼容性与验证情况

启动安全改动使用 NDK 26.1.10909125 的本地构建，在 PICO 4 Pro / PICO OS 5.13.7 / SELinux permissive 上完成验收，正常开机、升级、模式切换、重复控制命令及定点失败分支均通过。一次 45 秒真人佩戴验证确认了持续眼嘴更新、眨眼与张闭嘴响应、profile 0 初始化无重试，以及正常停止追踪，期间服务未重启。原厂库未修改。2.5.0的设置应用、配置工具和预设文件也已在 SELinux Enforcing 下通过开机与面板通信检查。发布工作流显式选用 NDK 26.1.10909125 与 CMake 3.22.1。

以下 90/90 数据来自此前的 2.4.0 验证，并非之后版本重新测得的吞吐。设备为 PICO 4 Pro（PICO OS 5.13.7），保持唤醒、追踪就绪，未佩戴。测试用一套测试驱动程序复用模块的表情处理算法，回放 2,700 组录制的相机输入（约 30 秒），搭配 PicoFacialBridge 0.5.0、UnifiedPicoModule 0.4.0、VRCFT-Enhance 和桌面 VRChat：

| 环节 | 频率 |
|---|---:|
| 头显上的眼部与嘴部计算 | 89.80 Hz（2,700 组输入中完成 2,694 组；另 6 组遇到原厂的队列已满丢弃规则，推理无失败） |
| Bridge → PC（`X` 包） | 89.84 Hz |
| VRCFT → OSC，同一来源的眼部与嘴部 | 87.80 Hz（2,633 个来源，占输入的 97.5%） |

到达 OSC 的每个来源，VRChat 都回送了至少一项眼部值和一项嘴部值，与写入值相同。各环节的数值都与来源一致，覆盖范围是测试 Avatar 实际发送的受支持字段。loopback 抓包存在丢弃，因此 PC 侧的计数是可验证下界。从 `X` 到达 PC 到 OSC 输出，中位数为 9.1 ms，第 95 百分位为 12.5 ms。回放不是经已安装的 Magisk 包运行的，也不包含实时相机复制和 Virtual Desktop 的解码与渲染。

在 2.3.0 版本（嘴部约 24 Hz）上，另用官方 VRCFT 5.4.5 完成过一次 84 秒的真人佩戴 Virtual Desktop 运行，使用 Cymple 嘴部加 PICO 眼部：每秒约 89.7 个 `X` 包到达 PC，每包带新的眼部来源。

尚未覆盖：单眼质量失效、分时参考重建、嘴部输出的真机对照，以及长时间实机相机使用。融合视线保持原样；当前 Avatar 使用的宿主侧眉部参数仍对两侧取平均，尽管扩展会同时携带两侧数值。

## 关闭与卸载

运行 `picoet.sh off` 可关闭增强并恢复原厂眼动服务。要移除模块，在 Magisk 中卸载后重启头显；设置应用随模块一起移除，后处理配置文件也会删除。清理只删除已知状态文件，不递归进入意外目录或子挂载，因此这些目录可能保留。

## 从源码构建

构建需要Python 3、JDK 17及以上（`JAVA_HOME`）、Android platform 35与build-tools 35.0.0（`ANDROID_HOME`），以及NDK 26.1.10909125、CMake 3.22.1/Ninja。原生脚本支持`-Ndk` / `-CmakeBin`参数、`PICOET_NDK` / `PICOET_CMAKE_BIN`环境变量，或`ANDROID_HOME`下的标准目录。

```powershell
pwsh -File hook\build-hook.ps1                 # 构建 injector 与 payload
python camera-source/build.py android --ndk "$env:ANDROID_HOME/ndk/26.1.10909125" --cmake "$env:ANDROID_HOME/cmake/3.22.1/bin/cmake.exe" --ninja "$env:ANDROID_HOME/cmake/3.22.1/bin/ninja.exe"
pwsh -File build-picoet-enhance.ps1 -Python python # 构建面板并打包 Magisk zip
```

产物为`artifacts/magisk-picoet-enhance-v<版本>.zip`，包含模块脚本、原生工具、hook载荷及`system/app/PicoETEnhance/PicoETEnhance.apk`。打包检查ELF文件清单和原厂库哈希；面板直接用SDK构建，不需要Gradle或额外Android依赖库。

未提供签名时，本地构建会生成并复用开发专用签名。正式分发应通过`-PanelKeystore`（或`PICOET_PANEL_KEYSTORE`）提供持久签名，可选`-PanelKeyAlias`，密码放在`PICOET_PANEL_PASSWORD`环境变量中；升级必须沿用相同面板签名。标签构建需要仓库Secrets `PICOET_PANEL_KEYSTORE_BASE64`和`PICOET_PANEL_PASSWORD`，可选的`PICOET_PANEL_KEY_ALIAS`缺省为`panel`。

推送 `v<版本>` 标签后，GitHub Actions 会在 Windows 运行器上构建并发布带校验和的 zip。

## 待做

- [ ] **端到端整嘴模型**：以同源嘴图和原厂raw／instant／final表情为基础，补齐独立上下唇、脸颊和舌形的动作采集；微调回归教师，再蒸馏轻量学生。用户可在自己的PC上用教师辅助个人适配，不上传图像或个人权重；逐通道可观测性、真人效果及重新佩戴泛化仍需验证。
- [ ] **后处理佩戴比较**：比较均衡、灵敏与不处理三个预设的实际响应、静止噪声及Avatar表现。

## 反馈

欢迎提交 Issue。请提供头显型号、PICO OS 版本、模块版本、当前模式，以及 `picoet.sh status` 的输出与相关日志片段。

## 许可证

本项目原创代码与脚本采用 [MIT](LICENSE)。内置的 ShadowHook 2.0.1 源码包含 MIT／BSD 许可的依赖，Magisk ZIP 内附相应声明与项目许可。本项目不包含也不分发任何 PICO 二进制，仅在运行期按数字偏移引用设备自身的库。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
