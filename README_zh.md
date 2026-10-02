# PicoET-Enhance

[English](README.md) | 简体中文

面向 PICO 4 Pro 的眼动增强 Magisk 模块：让头显的眼动服务输出真实的逐眼视线与瞳孔数据，配合 [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) 和 [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule) 使用。

> 当前版本 v2.0.3，面向 PICO 4 Pro 的 PICO OS 5.13.7。v2.0.2 已在该环境完成实机验证；本版修复已通过构建与静态检查，尚待真机复测。详见[更新日志](CHANGELOG_zh.md)。

## 它做什么

PICO 4 Pro 的固件本身就能算出左右眼各自的视线，但默认只对外提供融合后的结果。本模块在运行期对该行为做两处修正：

- 打开眼动服务内部的逐眼门控，使逐眼视线与真实瞳孔数据被写入共享内存；
- 让眼动算法同时运行左右眼的单眼模型，得到真正的分眼视线向量（`dual` 模式），也可以只取其中一只眼（`left` / `right`）。

修正只发生在眼动服务进程的内存里。模块不重打包任何应用，不写入 `/system`，不修改任何持久属性。

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

本模块只负责让头显产生更好的数据。它会把当前生效的模式写成两个临时属性，PicoFacialBridge 无需 Root 就能读到，据此自动判定普通模式或增强模式并告知 PC 端。因此 PC 侧不需要为本模块做额外配置。

## 环境要求

| 项目 | 要求 |
|---|---|
| 头显 | PICO 4 Pro |
| 固件 | PICO OS 5.13.7（Android 10 / API 29，arm64-v8a） |
| Root | 已安装 Magisk |
| 配套组件 | PicoFacialBridge 与 UnifiedPicoModule，用于把数据接入 VRCFaceTracking |

尚未确认其他型号与固件版本可用。安装脚本会校验设备上两份眼动库的 SHA-256，版本不符时拒绝安装；即使绕过安装校验，运行期载荷也会因锚点与哈希守卫而拒绝装载，服务保持原样。

## 安装

1. 从 [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases) 下载 `magisk-picoet-enhance-v<版本>.zip`。
2. 在 Magisk App 中通过“从本地安装”刷入，或使用命令行：

   ```sh
   adb push magisk-picoet-enhance-v2.0.3.zip /data/local/tmp/
   adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.0.3.zip'
   ```

3. 重启头显。

新安装的默认状态是 `off`，即不注入、保持原版。重启后需要手动选择模式。

## 使用

在头显上以 Root 运行：

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh left
sh /data/adb/modules/picoet-enhance/picoet.sh right
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh gate on
sh /data/adb/modules/picoet-enhance/picoet.sh gate off
```

| 命令 | 作用 |
|---|---|
| `status` | 显示保存的模式、注入状态、服务 PID 与 `ro.pxr.externalfunc` 的当前值 |
| `dual` | 逐眼视线。左右眼单眼模型同时运行，输出真实分眼视线（推荐） |
| `left` / `right` | 融合视线改由指定一只眼的单眼模型给出 |
| `off` | 重启眼动服务且不注入，回到完全原版 |
| `gate on` / `gate off` | 单独控制门控层。打开后固件会写入逐眼与瞳孔字段，但视线仍是按固定深度拆分的融合结果 |

切换模式会重启眼动服务，正在使用的眼动与面捕客户端会断开。有客户端连接时切换会被拒绝，需要显式加 `--force`。Magisk App 的“操作”按钮会按 `off → left → right → dual → off` 循环。

状态文件为 `/data/local/tmp/picoet-mode` 与 `/data/local/tmp/picoet-gate`，日志为 `/data/local/tmp/picoet-hook.log`。这些路径由载荷固定，不随模块改名。

## 关于企业版门控

固件里控制逐眼数据写入的开关是 `PXR::EyeUtil::isBusinessDev()`。除了逐眼功能，它还是整机的企业版（ToB）许可标志：系统属性 `ro.pxr.externalfunc` 为 1 时，framework、VR 桌面、设置和一批企业应用都会切换到企业版形态，机型名称也会改变。

本模块不碰这个属性，也不修改任何系统文件。它只通过运行期 hook 让眼动服务进程内对该函数的调用返回真值，范围仅限该进程。模块的日志与状态输出中可以看到 `ro.pxr.externalfunc` 始终为 0，模块本身也会在卸载后不留下任何持久改动。

## 兼容性与验证情况

在本项目的支持环境中（PICO 4 Pro / PICO OS 5.13.7 / SELinux Permissive）已完成以下验证：

- `dual` 模式下在真实眼帧上确认了随注视距离变化的真实辐辏，不是固定距离拆分；
- `left` / `right` 模式的闭眼回归；
- 卸载或 `off` 后回到原版；
- 开机自动注入，以及在眼动服务重启后由 watchdog 自动重新注入。

尚未验证的内容包括：SELinux 为 Enforcing 时的 SEPolicy 规则（模块附带一份尽力而为的规则，但当前设备为 Permissive，未实际生效过）、`right` 模式下闭左眼的组合、以及 PC/VRCFT 侧的完整取证。

## 风险与回滚

- 切换模式会重启眼动服务，眼动与面捕客户端全部断开。
- 载荷在未知固件上会因为守卫不匹配而不装载，但注入本身属于底层操作，理论上存在让服务异常的可能。服务的 init 配置并非 critical 级别，异常后会被系统重新拉起。
- 回滚：`picoet.sh off` 立即回到原版；卸载模块并重启则完全清除。hook 只存在于进程内存，重启服务即消失。

## 从源码构建

构建需要 Android NDK 与 CMake/Ninja。脚本会从 `-Ndk` / `-CmakeBin` 参数、`PICOET_NDK` / `PICOET_CMAKE_BIN` 环境变量，或 `ANDROID_HOME` 下的标准目录中查找工具链。

```powershell
pwsh -File hook\build-hook.ps1                 # 构建 injector 与 payload
pwsh -File build-picoet-enhance.ps1            # 打包成 Magisk zip
```

产物为 `artifacts/magisk-picoet-enhance-v<版本>.zip`。打包脚本会扫描暂存目录中的所有文件，拒绝任何已知 PICO 库的哈希，并只允许我们自己的两个 ELF 产物，确保发布包内不含任何 PICO 字节。

推送 `v<版本>` 标签后，GitHub Actions 会在 Windows 运行器上构建并发布带校验和的 zip。

## 待做

- [ ] **舌头方向增强**：PICO 目前只对外提供伸舌量 `TongueOut` 一个标量，而 VRCFaceTracking 已定义了 `TongueLeft/Right/Up/Down` 等方向通道。计划先取下脸相机的原始帧，用视觉分割基模自动打标，再训练一个小模型，并允许用户采集自己的数据在本地做在线微调。
- [ ] **面捕不滤波输出**：把端侧 FaceTrackor 的时间平滑做成可选关闭，让模块直接转发逐帧原始面部数据。时间滤波改由 PC 端按需处理，端侧只负责发原始值。

## 反馈

欢迎提交 Issue。请提供头显型号、PICO OS 版本、模块版本、当前模式，以及 `picoet.sh status` 的输出与相关日志片段。本模块只针对用户自己的设备。

## 许可证

本项目原创代码与脚本采用 [MIT](LICENSE)。`hook/third_party/shadowhook-2.0.1/` 为 ByteDance ShadowHook 2.0.1 的源码副本，采用 MIT 许可。本项目不包含也不分发任何 PICO 二进制，仅在运行期按数字偏移引用设备自身的库。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
