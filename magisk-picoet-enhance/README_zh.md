# PicoET-Enhance 模块技术参考（`v2.6.2`）

[English](README.md) | **简体中文**

本文件面向开发者与进阶用户，说明安装在 `/data/adb/modules/picoet-enhance/` 的 Magisk 模块内部结构、状态文件、控制命令与恢复机制。安装步骤、配套应用用法及 PC Studio 说明请查看 [主 README](../README_zh.md)。

## 适用环境与设计边界

- **目标平台**：PICO 4 Pro、PICO OS 5.13.7（Android 10 / API 29，`arm64-v8a`），Magisk，SELinux Enforcing。
- **零 PICO 字节**：模块包仅包含本项目自研脚本、原生工具、预设文件与配套 APK。`customize.sh` 在安装时校验 `/system/lib64/libpxreyetrackingservice.so` 与 `/system/lib64/libpxreyetracking.phoenix.so` 的 SHA-256，不匹配即中止。
- **纯内存 Hook**：所有 hook 通过 `ptrace` 注入驻留在 `pxreyetrackingservice` 进程内存中（`picoet-inject` + `libpicoet_hook.so`），不修改任何系统库，也不触碰 `ro.pxr.externalfunc`。

## 模块目录结构

| 路径（位于 `/data/adb/modules/picoet-enhance/` 下） | 职责 |
| --- | --- |
| `module.prop`、`sepolicy.rule`、`hashes.sh` | 模块元数据（`id=picoet-enhance`、`version=2.6.2`、`versionCode=30`）、SELinux 规则，以及 `picoet-inject` 与 `libpicoet_hook.so` 的 SHA-256 校验钉 |
| `customize.sh` | 安装时执行原厂库哈希校验、权限设置、清理 `/data/system/package_cache/*/PicoETEnhance-*` 解析缓存以及模式规范化 |
| `post-fs-data.sh` | 刻意保持空操作以防阻塞启动；临时属性在重启时自动清空 |
| `service.sh` | `late_start` 看护进程：等待 `sys.boot_completed=1`，拉起 `picoet-panel`，并循环调用 `picoet.sh ensure` 与 `picoet-camera reconcile` |
| `picoet.sh` | Root 命令行控制器与看护事务脚本（`status`、`dual`/`on`、`off`、`toggle`、`reload-processing`，以及内部子命令 `boot`/`ensure`） |
| `action.sh` | Magisk **操作（Action）** 按钮入口（执行 `picoet.sh toggle --force` 后打印 `status`） |
| `uninstall.sh` | 卸载后在下次开机时清理状态文件、运行目录、频率与后处理配置及临时属性 |
| `bin/picoet-inject` | ARM64 `ptrace` 注入器及文件 / 锁辅助工具（`--run-locked`、`--prepare-runtime`、`--save-mode`、`--normalize-mode`、`--read-file`、`--read-log`） |
| `bin/picoet-config` | Root 命令行工具，用于读取、校验并原子写入 `/data/local/tmp/picoet-postprocessing.conf` |
| `bin/picoet-panel` | 模块自有的 root 信箱工作进程，处理来自零权限 `dev.pico.etenhance` APK 的固定请求 |
| `bin/picoet-camera` | 模块自有的局域网相机控制器（`on`、`off`、`status`、`reconcile`、`halt`、`results-on/off`、`eyes-on/off`） |
| `bin/ptj_ipc_daemon` | 独立 root 传输守护进程（负责 libjpeg-turbo 编码、UDP 发现、HTTP MJPEG、`PTJ1`、`PSL1`、`PSE1`） |
| `lib/libpicoet_hook.so` | 注入载荷（逐眼瞳孔门控、左右眼独立表情、90/90 Hz 调度、`EPI1` v2 导出、44 项原厂后处理参数化） |
| `lib/libpicoet_camera_jpeg.so`、`lib/libptj_source.so` | 进程内局域网相机适配器与共享内存垫片（`PTJIPC5`），按服务实例按需部署 |
| `system/app/PicoETEnhance/PicoETEnhance.apk` | Systemless 挂载的零权限配套应用（**局域网广播** 与 **后处理** 双标签页） |
| `postprocessing-{default,balanced,responsive,raw}.conf` | PICO 原厂 44 项后处理参数的预设配置 |

## 运行期属性、状态文件与日志

### 临时 Android 属性（由 `picoet.sh` 发布）

- `picoet.enhance.mode`（`off` | `dual`）与 `picoet.enhance.gate`（`off` | `on`）：供 `PicoFacialBridge`（`0.5.0+`）读取以检测增强模式，并向 `UnifiedPicoModule`（`0.4.0+`）通告 `EPI1`/`X` v2。眼动服务重启或退出时属性会立即清空。

### 持久化与运行期路径

| 路径 | 用途 |
| --- | --- |
| `/data/local/tmp/picoet-mode`、`/data/local/tmp/picoet-gate` | 保存的模式（`dual`/`off`）与门控（`on`/`off`），以 `u:object_r:shell_data_file:s0` 标签原子写入 |
| `/data/local/tmp/picoet-eye-hz`、`/data/local/tmp/picoet-mouth-hz` | 可选的启动期目标频率覆盖（正整数 `1..90`，默认 `90/90`，嘴部不超过眼部，升级保留） |
| `/data/local/tmp/picoet-postprocessing.conf` | 已保存的 44 参数后处理配置（`version=1`、`key=value`，上限 16 KiB；载荷启动时读取一次，升级保留） |
| `/data/local/tmp/picoet-hook.log` | 注入载荷运行日志 |
| `/data/local/tmp/picoet-runtime/` | 私有运行期目录，存放按 PID 命名的载荷状态报告（`<pid>.state`）与异步 `unsafe` 标记 |
| `/data/local/tmp/picoet-camera/` | 按服务实例隔离的 `0700` 暂存目录，存放 `video-channel.bin`、暂存相机 `.so` 副本、`camera-status.json` 与 `daemon.log` |
| `/data/adb/picoet-enhance/` | 模块控制状态（`control.lock`、`boot.id`、`recovery.used`、`injection.blocked`、`unsafe.instance`、`unsafe.handled`、`automatic.attempts`、`camera.enabled`、`camera.results`、`camera.eyes`、`camera.instance`、`camera.failure`）与控制日志（`picoet.log`、`inject.log`、`service.log`、`panel.log`） |

## 命令行参考

在头显 Root Shell 中执行：

```sh
MOD=/data/adb/modules/picoet-enhance

# 模式切换与状态查看
sh "$MOD/picoet.sh" status
sh "$MOD/picoet.sh" dual              # 同义词：on（加 --force 可在有客户端时强制重启）
sh "$MOD/picoet.sh" off               # 恢复未注入的原厂服务
sh "$MOD/picoet.sh" toggle            # ready 时切 off，否则尝试开启 dual
sh "$MOD/picoet.sh" reload-processing # 校验配置并在增强已开启且空闲时重启加载（不接受 --force）

# 后处理配置工具
"$MOD/bin/picoet-config" read|check|write|defaults|balanced|responsive|raw
```

- 首次安装默认初始化为 `dual/on`（90/90 Hz）；升级保留已有的 `off` 状态。
- `status` 输出 `effective_eye_hz` 与 `effective_mouth_hz`（生效的配置目标，非实测帧率）、`phase`、`applied_mode`、`applied_gate`、`dumpsys` 客户端连接数，以及 `/data/local/tmp/picoet-hook.log` 末尾 5 行。
- 修改目标频率时，先停止追踪客户端，写入 `/data/local/tmp/picoet-eye-hz` 和 / 或 `/data/local/tmp/picoet-mouth-hz`，再运行 `picoet.sh dual`；删除对应文件并再次执行 `dual` 即可恢复 90/90 Hz。
- `picoet-config write` 从 `stdin` 读取并拒绝非法、重复或未知字段；若启动时发现配置文件损坏或属主异常，载荷会明确报错而非静默回退。

## 注入生命周期与恢复预算

所有控制操作（`picoet.sh` 与 `picoet-camera`）均通过 `/data/adb/picoet-enhance/control.lock` 串行化。

1. **就绪门禁**：`service.sh` 等待 `sys.boot_completed=1`。执行任何 `ptrace` 附加之前，`picoet.sh` 会确认 `init.svc.pxreyetrackingservice=running`，并通过有超时的 `dumpsys -t 1 pxreyetrackingservice` 核对同一 `pid:starttime` 实例已输出全部三项原厂算法（`ET`、`FT`、`IPD`）状态行。
2. **运行期核实**：仅检测到 `libpicoet_hook.so` 可执行映射绝不视为就绪，载荷必须原子写入匹配当前 `pid:starttime` 且 `phase=ready` 的 schema-2 状态报告。启动追踪客户端前建议先确认 `phase=ready applied_mode=dual applied_gate=on`。若引擎在注入前已完成初始化，模块会保持其运行并自行热身增强状态，不强制重建引擎。
3. **注入器退出码与恢复策略**：
   - `0`：附加与 `dlopen` 成功（最终就绪仍以载荷状态文件为准）。
   - `2`：安全 / 已知失败；暂停后续自动注入，直到下次重启或显式模式命令。
   - `3`：暂时找不到安全的用户态执行线程或遭遇临时信号停止，**未写入任何字节**。`service.sh` 会释放锁并冷却 **30 秒** 后重试，不消耗两类预算。
   - `4` / `5`：残留部分映射（`4`）或远端调用 / 恢复进入不安全状态并已终止目标（`5`）；消耗共享恢复预算（每次开机或显式模式命令周期内最多 **1** 次自动重启重试）。载荷异步回滚失败共享同一份单次预算。
   - `6`：`--run-locked` 忙（另一条控制命令正持有 `control.lock`）。
4. **连续不稳定实例上限**：自动注入最多连续尝试 **3** 个不稳定的服务实例（`automatic.attempts`）；仅当同一实例连续保持 `ready` 满 **60 秒** 后才会重置计数。
5. **手动重置**：执行显式模式命令（`picoet.sh dual`、`off`、`toggle` 或 Magisk **操作** 按钮）会清空 `injection.blocked`、`recovery.used` 与 `automatic.attempts`。

## 配套应用与相机控制边界

- 配套 APK（`dev.pico.etenhance`）不声明任何 Android 权限，也不执行 Shell 命令或打开网络 Socket。
- `picoet-panel` 核对调用方 UID 属于 `dev.pico.etenhance`，并通过基于目录描述符的 `openat` + `O_NOFOLLOW` 处理固定信箱请求。
- `picoet-camera` 在 `control.lock` 保护下管理 **局域网广播**（`on` / `off`）、**嘴部**（`results-on` / `results-off`）与 **双眼**（`eyes-on` / `eyes-off`）。首次附加相机通过 `dumpsys` 核对追踪空闲，不会重启 `pxreyetrackingservice`。若进程内已驻留单独启动的开发适配器（`camera-source/device.py`），`picoet-camera` 会回报 `foreign_adapter` 并拒绝操作，直至结束该开发会话并重启服务。
- 完整的线缆协议与 `PTJIPC5` 细节见 [CAMERA-PROTOCOL_zh.md](../CAMERA-PROTOCOL_zh.md)。

## 卸载

在 Magisk 中卸载 **PicoET-Enhance** 并重启头显。`uninstall.sh` 会删除已知状态文件、运行目录、频率与后处理配置及临时属性，不递归进入意外目录或嵌套挂载。
