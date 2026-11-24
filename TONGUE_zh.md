# PC 端到端整嘴追踪使用指南

[English](TONGUE.md) | 简体中文

本分支（`feat/tongue-pc`）配合同名分支的 [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc) 使用，将 PICO 4 Pro 的嘴部相机画面通过局域网传到 PC，在本地完成 50 通道整嘴推理与原厂结果融合，再送入 VRCFaceTracking（VRCFT）。

```text
PICO 嘴部相机（400×400 灰度，90 Hz）
  → 头显独立后台服务编码（JPEG 95，TCP 45681 / PTJ1）
  → PC 工作台解码与整嘴 ONNX 推理，按通道与原厂结果融合
  → 本机 UDP 45682 / MFR1 → UnifiedPicoModule → VRCFT
```

头显模块同时提供独立的左眼与右眼画面流（默认端口：嘴部 `45681`、左眼 `45682`、右眼 `45683`，每路支持 8 个并发消费者），第三方客户端可按 [局域网相机协议](CAMERA-PROTOCOL.md) 通过 PTJ1 或 HTTP MJPEG 订阅。所有图像、标注与个人模型权重均保留在本机 PC 上。

---

## 1. 准备 PC 工作台

已在 Windows、Python 3.12 与 .NET 10 上验证。在仓库根目录创建虚拟环境并启动：

```powershell
python -m venv pc-studio/.venv
pc-studio/.venv/Scripts/python.exe -m pip install -r pc-studio/requirements.txt
pc-studio/start.cmd
```

启动后在浏览器打开 `http://127.0.0.1:8787`。默认依赖使用 ONNX Runtime DirectML，兼容各类 DirectX 12 显卡；工作台启动后处于空闲状态，发往 VRCFT 的 UDP 输出默认关闭。

### 界面概览

| 页面 | 用途 |
|---|---|
| **追踪** | 自动发现并选择头显、选取整嘴模型（`.onnx`），启停实时追踪与画面预览 |
| **采集** | 对照 71 组动作示范练习、录制本地素材，并逐帧审核可见标签 |
| **训练** | 引导数据准备流程，并调用本机训练与导出工具 |
| **诊断** | 分为 **表情诊断**（实时 50 通道监控、置顶卡片与双源对照）与 **连接诊断**（帧率、延迟、提供器、队列与原厂流状态） |
| **设置** | 配置发往 VRCFT 的开关、通道分流预设、One-Euro 滤波、模型与推理后端、头显地址端口及本地回放 |

工作台会自动在局域网（UDP `45681`）搜索头显：仅发现一台时自动选中，发现多台时从下拉列表选择；若局域网隔离广播，可在 **设置 → 头显与端口连接** 手动填写地址与端口。

点击 **选择文件** 选取整嘴 `.onnx` 模型，同名 `.json` 描述文件需放在同一目录。本分支不附带现成的通用整嘴或教师权重；还没有模型时，可以直接前往 **采集** 页录制自己的素材。

### 切换推理后端

不同 ONNX Runtime 发行包会互相冲突，请为每种后端创建独立的 venv，只安装对应的一份依赖：

| 推理后端 | 依赖文件 |
|---|---|
| Windows 默认（DirectML） | `pc-studio/requirements.txt` |
| 仅 CPU | `pc-studio/requirements-cpu.txt` |
| NVIDIA CUDA 12 | `pc-studio/requirements-cuda.txt` |
| Intel OpenVINO GPU | `pc-studio/requirements-openvino.txt` |

例如在 CUDA 12.6 环境下，先在新 venv 中安装匹配的 PyTorch，再安装 CUDA 版依赖并用该环境的 Python 启动 `pc-studio/backend.py`：

```powershell
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r pc-studio/requirements-cuda.txt
```

推理设备设为 `auto` 时，按 **CUDA Graph → CUDA → OpenVINO GPU → DirectML → CPU** 的顺序探测并做首次推理校验；手动指定具体设备时不会回退到其他后端。实际生效的执行提供器与报错信息可在 **诊断 → 连接诊断** 查看。

---

## 2. 启动头显端相机源

### 常规使用（推荐）

安装本分支构建的 Magisk 模块与配套应用后：

1. 首次开启前先关闭头显端其他眼追／面捕客户端一次。
2. 在头显打开 **PicoET-Enhance → 局域网广播**，开启总开关（默认关闭并会记住选择；页面会显示运行状态、三路地址和当前订阅数）。**后处理** 标签页保留原有的眼嘴后处理面板。
3. 如需在 PC 端合流原厂嘴部通道，或在录制时同步保存原厂标签，请在 **结果共享** 中打开 **嘴部**（原厂嘴部结果流 `PSL1` 由增强 hook 提供，需确认头显 `PicoET-Enhance` 处于增强模式而非原厂关闭态）。

> 局域网相机流未加密且无鉴权，请仅在可信的私有局域网内使用，不要暴露到公网或做外网端口转发。

### 从源码构建与手动注入（开发调试）

以下方式面向开发者排查，请勿与模块自带的托管相机服务同时运行。适用于已 Root 的 PICO 4 Pro（PICO OS 5.13.7），构建依赖 Android NDK `26.1.10909125`、CMake `3.22.1` 与 Ninja（`libjpeg-turbo 3.1.2` 会自动拉取并静态编译，离线环境可用 `--jpeg-source` 指定本地源码）。

```powershell
python camera-source/build.py android --ndk <NDK目录> --cmake <cmake路径> --ninja <ninja路径>
```

产物位于 `camera-source/build-android/`，包含 `picoet-inject`、`libpicoet_camera_jpeg.so`、`libptj_source.so`、`ptj_ipc_daemon` 及模块控制器 `picoet-camera`。若需与原厂标签精确关联，请在构建目录另行编译 `picoet_hook` 目标（产出 `camera-source/build-android/hook/libpicoet_hook.so`）并与相机组件成套更新；眼部结果订阅见 [PSE1 协议说明](CAMERA-PROTOCOL.md#eye-expression-results-pse1)。

首次手动注入前先退出头显上的眼／面捕客户端，再使用设备 ADB 序列号启动（ADB 不在 PATH 时传 `--adb <路径>`）：

```powershell
python camera-source/device.py start  --serial <serial> --state pc-studio/user-data/source-session.json
python camera-source/device.py status --serial <serial> --state pc-studio/user-data/source-session.json
python camera-source/device.py stop   --serial <serial> --state pc-studio/user-data/source-session.json
```

`--state` 文件记录了本次会话的设备、启动代际与远端目录，查询状态、停止以及同一次服务周期内重新启动都应复用同一个 state 文件；系统重启或眼动服务重启后改用新的 state 文件。Android 端按需持有追踪租约，没有客户端订阅的图像通道不会被复制或编码。

---

## 3. 连通全链路

1. 按 [UnifiedPicoModule 接收端指南](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE_zh.md) 构建并安装同分支的 VRCFT 模块。
2. 启动常规 PICO 面捕客户端（如 PicoFacialBridge），在 VRCFT 中将面部追踪分配给 PICO。
3. 打开 PC 工作台，选中头显和整嘴 `.onnx` 模型，点击 **开始追踪**；确认画面与读数正常后，在 **设置** 中开启 **把表情发送给 VRCFaceTracking**（默认本机 UDP `45682`）。
4. 在 **追踪** 页查看实时画面，在 **诊断** 页检查通道响应与链路延迟。

当 `UnifiedPicoModule` 在 UDP `45682` 收到活跃的 `MFR1` 流时，会按 `valid_mask` 写入收到的通道：完整覆盖全部 33 个原厂嘴部通道时自动将 Bridge 订阅切换为仅眼动（`PXR_SUB` 掩码 `3 → 1`，眼动不断流）；若模型仅覆盖部分通道（如仅 6 路舌头模型）或头显 `PSL1` 暂时断流，未覆盖通道的 `valid_mask` 为 0，`UnifiedPicoModule` 会保留 Bridge 面部订阅（`mask=3`）以维持原厂下巴与嘴唇驱动；用户在设置页显式设为 **关闭**（`off`）的通道则以 `valid_mask=1, value=0.0` 明确归零。停止发送或超时后自动恢复 Bridge 直连面捕。若日常采用「Cymple 嘴＋PICO 眼」混搭，保持本工作台停止即可，工作台不会争抢嘴部所有权。

### 通道分流预设

在 **设置 → 通道分流预设**（`mouth_mode`）中选择默认来源，修改后点 **保存设置** 即时热生效，无需停止追踪或重启 VRCFT：

| 预设 | 原厂通道数 | 模型通道数 | 说明 |
|---|---:|---:|---|
| **精选混合**（`curated`，推荐默认） | 28 | 18 | 稳定基础口型交由头显原生 `final33` 直接驱动，缺失、单侧拆分与原厂串扰严重的通道交由新模型 |
| **原厂全量混合**（`hybrid`） | 37 | 9 | 头显支持的 37 项均优先用原生值，仅 9 项原厂缺失通道用新模型 |
| **仅新模型**（`model_only`） | 0 | 46 | 全部 46 项均由 PC 模型驱动，不订阅头显原厂结果流（`LABELS`） |

**精选混合（`curated`）**交由新模型的 18 项通道包括：
- **9 项原厂缺失通道**：`TongueLeft`、`TongueRight`、`TongueUp`、`TongueDown`、`TongueRoll`、`CheekSuckLeft`、`CheekSuckRight`、`MouthTightenerLeft`、`MouthTightenerRight`
- **3 项耦合或单侧拆分通道**：`TongueOut`、`CheekPuffLeft`、`CheekPuffRight`
- **6 项原厂串扰明显通道**：`MouthSmileL`、`MouthSmileR`、`MouthFrownL`、`MouthFrownR`、`MouthUpperUpL`、`MouthUpperUpR`

三套预设共用下方的 **逐项调整形态键来源** 表格（46 项按解剖部位分 6 组，覆盖 29 项独立原厂键、8 项由头显聚合源拆分出的独立键，以及 9 项可切「新模型／关闭」的扩展键）。修改任意单项后会自动切换为 `custom`；只要有至少 1 路选择原厂，就会复用同一条 `PSL1` 连接读取头显结果。

### 滤波与常用设置

**设置 → 滤波与平滑** 提供标准 One-Euro 滤波参数，保存后即时热生效：

| 参数 | 字段名 | 默认值 | 作用 |
|---|---|---:|---|
| 启用 One-Euro 滤波 | `filter_enabled` | 开启 | 总开关 |
| 最小截止频率（Hz） | `filter_min_cutoff` | `8.0` | 越低静止越稳，越高对微动作越敏感 |
| 速度系数 | `filter_beta` | `4.0` | 越大快速说话和大幅动作越跟手、延迟越低 |
| 导数截止频率（Hz） | `filter_d_cutoff` | `1.0` | 平滑速度估计，抑制尖峰抖动 |

滤波按真实相机时间戳（`mouth_sensor_ns`）逐通道计算，并在**模型推理之后、原厂合流之前**执行——因此交给原厂的通道直通头显 `final33`，不会叠加二次滤波。新会话、更换模型、无效输出、掩码变更或帧间隔超过 `stale_ms` 时会自动重置滤波状态。旧配置文件中的 `smoothing_ms` 仍自动换算兼容（`min_cutoff = 1000 / (2π × smoothing_ms)` Hz，`0` 为关闭）。运行日志会同时保留滤波前的 `raw_values` / `raw_channels`、滤波值与发布时间。

其余设置分为两组：
- **设置 → 模型与推理**：模型路径、推理设备、结果有效期（`stale_ms`）与预览帧率；更改模型路径或设备保存时会停止当前追踪，预览帧率实时生效。
- **设置 → 头显与端口连接**：手动头显地址、视频端口与本机 UDP 端口；更改后保存时会停止当前追踪。

### 表情诊断与连接诊断

- **诊断 → 表情诊断**：以 30 Hz 刷新 50 个通道的实时读数，支持将常用通道**置顶**到顶部监控区，按解剖部位、来源或活跃度筛选，支持**冻结读数**与**原厂／模型双源并排对照**（对照视图会自动标注头显合并槽位，并隐藏原厂不支持的列）。
- **诊断 → 连接诊断**：查看接收与推理帧率、端到端与模型耗时（P50 / P95）、当前执行提供器、队列与丢帧统计、`PSL1` 原厂流连接状态以及错误详情。

---

## 4. 录制素材与训练个人模型

### 跟随示范录制与审核

打开 **采集** 页，连接头显前即可对照示范图和动画练习动作（正面图类似镜子，画面左侧即你的左侧；侧面图用于观察唇颌前后深度）。录制前在 **采集预设** 中选择动作方案，展开 **动作顺序** 可查看全部步骤：

| 采集预设 | 步数 | 涵盖动作 |
|---|---:|---|
| 基础嘴部 | 12 | 放松、开闭嘴、左右微笑、噘嘴、漏斗、鼓腮、下颌左右移、伸舌、放松 |
| 舌头专项 | 11 | 放松、短／中／长伸舌、左右上下、舌侧卷筒、舌尖弯曲对照、放松 |
| 独立唇形 | 13 | 放松、上下唇横移、内卷、上下漏斗、噘嘴、左右压唇、放松 |
| 完整引导（默认） | 71 | 全部 50 项命名控制、组合动作、三档伸舌及口内舌动作、说话与舌尖上弯对照 |

选好头显、预设和 **换步方式**，勾选同意后点击 **开始录制**：
- **自动换步**：每步默认留 7 秒准备（可在 3–30 秒间调整），随后倒计时保持并自动进入下一步；计时期间请保持示范图可见。
- **手动换步**：摆好动作后依次点击 **开始保持** → **结束保持** → **确认动作完成**。
- **灵活控制**：可随时 **上一步 / 下一步 / 跳到步骤 / 跳过 / 暂停**；取消勾选「按步骤引导我」可进入自由录制。若中途结束或断连，保存后可点击 **从上次断点续录** 补录剩余动作（新段另存）。
- **同步保存原厂标签**：在 **采集 → 更多录制选项** 勾选 **接收头显嘴部结果**（需先在头显配套应用的 **结果共享** 中开启 **嘴部**），仅录图像时取消勾选即可。

建议在不同佩戴位置、甚至隔天各录一轮，以覆盖佩戴偏移。录制完成后，在 **采集 → 审核已录图像** 中逐页检查图像，填写实际可见的控制量 JSON（未填项保持未知，不自动补零），并将遮挡或看不清的帧标记为「看不清」「被遮挡」或「无嘴部画面」。

示范图包基于 [Unity-Movement HighFidelity 角色](https://github.com/oculus-samples/Unity-Movement/tree/v71.0.0/Samples~/BodyTrackingSamples/HighFidelity) 制作，涵盖 71 组静图、70 段动画与 25 个侧视图，其中 48 组使用原版形态键与修正规则，23 组在原网格上添加了舌头与独立上下唇示范控制。

### 教师 / 学生训练流程

本机训练支持命令行与工作台 **训练** 页表单（两者共用同一套参数）。若需要独立训练环境，可另建 venv 安装 `pc-studio/requirements-training.txt`（避免将其中的 CPU 版 ORT 与推理环境混装）。查看全部参数：

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/face_train.py --help
```

标准流程按顺序使用以下子命令：

1. `split`：基于已审核的数据集冻结划分（单人用 `--group-by session`，多人用 `--group-by person`，至少需要 3 个独立分组分别作为训练、验证与封存测试集）。
2. `teacher-train` 与 `student-baseline`：用本地 DINO 源码与权重微调教师（先 `--tune frozen` 训头，再按需通过 `--initial` 续训 `--tune last-two`），同时训练一个直接监督的学生基线作对照。
3. `validate-candidate`：在验证集上对比教师与基线学生，生成验证批准报告。
4. `predict-pseudo`：凭通过验收的教师报告，仅为训练分组生成软标签。
5. `student-distill` / `local-adapt`：用审核标签与软标签蒸馏训练个人学生模型（人工审核标签优先于弱标签与教师软标签）。
6. `evaluate` 与 `export`：评估最终学生并导出新的 `.onnx` 与 `.json`（带 `--approval <report.json>` 才会置起对应通道的输出有效位）；导出后在工作台手动选择新模型文件即可启用。

常用训练参数：
- **教师骨干**（`--teacher-backbone`）：默认 DINOv2 ViT-B/14-reg（392×392）；另支持 `dinov3`（ViT-B/16）、`dinov3-large`（ViT-L/16）与 `dinov3-huge`（ViT-H+/16），均使用 400×400 全图，需提供对应的本地源码目录与权重路径（遵循 [DINOv3 许可](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md)）。
- **学生输入与学习率**：学生默认 160×160（可用 `--student-input 224` 提升分辨率，基于 MobileNetV3-small），默认头学习率教师 `1e-4`、学生 `1e-3`。
- **损失与增强**：默认掩码 MSE、无增强；支持 `--loss huber`（Smooth L1，β=0.1）与 `--augmentation mild`（小幅平移、旋转、曝光／gamma 与噪声，不做水平镜像）。

### 进阶全分辨率连续训练流水线（`mouth50_trainer`）

如果需要训练全 400×400 输入的 50 通道连续回归模型（支持空间金字塔学生 `VFTStudent50`、解二值化连续损失 `debinarized_continuous_loss`、5-TTA 置信度软蒸馏、近红外物理域增强与可选因果 GRU-256 时序微调），可直接调用独立的离线训练包 `pc-studio/mouth50_trainer/`：

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/mouth50_trainer/pipeline.py --help
```

流水线分为四个独立子命令（本地骨干源码与权重可通过 `--dino-repo`、`--dino-weights`、`--mnv3-weights` 传入，或设置环境变量 `MOUTH50_DINO_REPO`、`MOUTH50_DINO_WEIGHTS`、`MOUTH50_MNV3_WEIGHTS`）：

| 子命令 | 作用 |
|---|---|
| `teacher` | 使用 `debinarized_continuous_loss` 与 12-query 空间注意力头微调 DINOv3-L 教师模型 |
| `kd` | 生成 5-TTA 软标签与连续置信度权重（支持 `--resume` 断点续跑） |
| `student` | 蒸馏训练单帧 `VFTStudent50`（默认 `small@384 pyramid roi=400`，含余弦重启、EMA 与 ONNX 导出） |
| `temporal` | 可选：在连续片段上微调有状态 `StatefulGRUStudent50` 并导出带 `h_in`／`h_out` 的 ONNX |

各阶段通过 `--data-dir` 指定包含 `vft50_train.jsonl`、`vft50_val71.jsonl` 的数据目录，通过 `--out-dir` 指定输出路径；导出的 `.onnx` 与同名 `.json` 清单可直接被工作台加载（默认保持 `valid_mask: 0`，需经独立验收后再解锁对应通道）。

### 本地数据回放

在 **设置 → 回放已保存的录制** 中可加载本地 `.npz` 数据集离线调试（无需连接头显），文件需包含 `images[N, 400, 400]`（uint8）、`seq[N]` 与 `times[N, 3]`（源纳秒），可选附带 `vendor_ns` 与 `raw_identity`。开始实时录制前需先停止回放。

---

## 5. 模型契约与离线测试

### 整嘴模型契约

- **通道顺序（`pico-mouth50-v1`）**：定义于 `pc-studio/face_schema.py`，包含 MouthLink 原 44 项，外加 4 项独立上下唇横移与 2 项上下漏斗共 50 项。
- **同名 `.json` 清单**：需声明 `schema`、`channels`、输入 `width` / `height`、`trained_channels`、`validated_channels` 与 `valid_mask`。运行端严格按 `valid_mask` 输出有效位；未传 `--approval` 导出的候选模型掩码为 `0`。
- **输入预处理与时序支持**：将 400×400 完整灰度图经 Pillow `BILINEAR` 缩放到模型输入尺寸后除以 `255`（不做水平镜像，归一化放在模型内）。除标准单帧 ONNX（`frame → blendshapes`）外，也支持带 `[1, H]` 隐藏状态（`h_in` / `h_out`）的有状态 ONNX，并在新会话、换模型、无效输出或断流超过 `stale_ms` 时自动清零隐藏状态。
- **输出协议**：`MFR1` 直接发送 0–1 连续控制值与逐字段有效位掩码。

### 离线测试

PC 端 Python 单元测试与双语网页测试（需安装 Node.js）：

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/check.py
```

头显相机服务的本机合成测试（从 x64 Visual Studio 开发终端运行，或传 `--vcvars <vcvars64.bat>`，无需连接 ADB）：

```powershell
python camera-source/build.py host --cmake <cmake路径> --ninja <ninja路径>
python camera-source/checks/test_ipc.py
python camera-source/checks/test_borrowed.py
python camera-source/checks/test_camera.py
python camera-source/checks/test_device.py
```

---

## 鸣谢与参考

- 相机与 PC 链路起步于 Rulez777 的 [PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4)（审阅提交 `f5f7894c6b30a86fd4699d721f966b6a01861443`）。
- 整嘴回归契约、Pillow 预处理与同源标签思路参考 UndefinedSignal 的 [issue #5](https://github.com/WolalaQAQ/PicoET-Enhance/issues/5) 与 MIT 许可的 MouthLink（审阅提交 `39e091f4191d2ea1ad8536120924cdf61949dae2`）。
- 相关素材与开源组件许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。
