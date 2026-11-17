# PC端到端整嘴追踪：开发分支

[English](TONGUE.md) | 简体中文

本仓库与[UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc)配套使用`feat/tongue-pc`。本分支模块打包可选的三路局域网相机，由配套应用首页启停；PC App和配套接收器仍从源码单独准备。

```text
PICO camera5嘴图lane（400×400灰度、目标90Hz真实新源）
  → 同步保护复制到有界共享槽
  → 独立root daemon，JPEG95，TCP45681／PTJ1
  → PC解码、整嘴ONNX连续回归
  → 本机UDP45682／MFR1＋逐字段有效位 → UnifiedPicoModule → VRCFT
```

原厂相机服务不监听TCP、不运行舌头模型。编码器借用共享槽直到最后一次像素读取结束，省掉第二次整图暂存复制；第一次相机保护复制保留。不用回调计数替换真实帧身份或sensor时间。camera5只有一个合成父图sensor时间，不是三路独立测得的曝光时间。

来源同时提供独立的左眼和右眼图像流。其他客户端可使用同一套[局域网相机协议](CAMERA-PROTOCOL.md)发现和订阅，支持PTJ1及标准HTTP MJPEG。默认端口为嘴45681、左眼45682、右眼45683，每路支持八个图像消费者并发。

## 1. 准备PC App

已使用Windows、Python 3.12与.NET 10验证。在仓库根目录执行：

```powershell
python -m venv pc-tongue/.venv
pc-tongue/.venv/Scripts/python.exe -m pip install -r pc-tongue/requirements.txt
pc-tongue/start.cmd
```

打开`http://127.0.0.1:8787`。Windows默认使用ONNX Runtime DirectML，可用DirectX 12显卡，不要求NVIDIA。网页启动时不采集，UDP默认关闭。

侧栏切换四个独立页面：**追踪**选择头显和模型后开始追踪；**采集**选择头显和预设，勾选同意后跟随示范录制，也可先打开画面预览；**训练**说明素材准备流程并提供高级训练工具；**设置**包含结果输出、连接参数、回放与诊断。切换页面会保留未保存的输入和正在进行的运行。

主界面自动查找同一局域网内的配套来源，并选择其嘴部图像流。只找到一台时自动选中，找到多台时从列表中选择，也可以点击“重新查找”再搜一次。查找使用UDP45681广播和多播，通过HTTP复核相机目录、PTC1核对来源身份，不订阅图像或标签；旧版单嘴发现仍可使用。网络隔离发现流量时，在设置页的“连接与性能”中填写地址和端口。

点击“选择文件”打开本机文件窗口，选取整嘴`.onnx`，同名`.json`描述文件放在同一目录。界面显示文件名和完整路径，取消选择会保留原来的模型。选择设备或文件只更新草稿，点击开始或保存后才写入设置。

推理需要本地整嘴`.onnx`及同名`.json`模型manifest。本分支不附带已验收的通用整嘴权重或教师权重，也不自动下载。没有模型时，先在采集页打开画面，录制第一批嘴图和标签；录制不需要先训练模型。旧`prepare_model.py`及分割／几何代码保留用于历史对照和回归检查，不属于当前的准备步骤。

### 其他后端

每种ORT发行包单独建venv，不将DirectML、GPU、OpenVINO、CPU的ORT包混装。只选一份依赖：

| 环境 | 依赖文件 |
|---|---|
| Windows默认／DirectML | `pc-tongue/requirements.txt` |
| CPU | `pc-tongue/requirements-cpu.txt` |
| NVIDIA CUDA 12 | `pc-tongue/requirements-cuda.txt` |
| Intel OpenVINO GPU | `pc-tongue/requirements-openvino.txt` |

已测CUDA 12.6环境先在新venv安装匹配的PyTorch：

```powershell
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r pc-tongue/requirements-cuda.txt
```

用该环境的Python运行`pc-tongue/backend.py`。`auto`依次尝试已安装的CUDA Graph→CUDA→OpenVINO GPU→DirectML→CPU，包含首次执行检查；显式选择后端时不自动换用另一后端。实际提供器与初始化失败原因在折叠诊断中显示。DirectML已在NVIDIA上核对，AMD／Intel真机仍待测试者验证。

## 2. 构建并启动头显源

安装本分支的完整模块与配套GUI后，在 **PicoET-Enhance → 局域网相机** 开启共享即可。默认关闭，模块记住选择；首页显示真实状态、三路地址和订阅数。首次开启前退出其他眼追／面捕客户端一次，之后客户端按需订阅。原参数面板从 **参数调节** 进入。

如果录制需要头显嘴部结果，请在配套应用打开 **共享嘴部结果**，并在 PC 工作台的 **采集 → 更多录制选项** 保持勾选 **接收头显嘴部结果**。只录图像时取消勾选。图像预览不会接收结果。

以下独立`device.py`入口用于开发诊断，不要与模块管理的相机服务同时启动。

需要已Root的PICO 4 Pro／PICO OS 5.13.7。本分支已合入PicoET-Enhance 2.5.2基线，保留其已保存后处理配置。适配器在hook前检查支持的系统库文件及指令窗口。工具链为NDK 26.1.10909125、CMake 3.22.1、Ninja；libjpeg-turbo 3.1.2独立构建静态库，ShadowHook直接编译仓库内已有vendored源码。

正式2.5.2主payload没有原厂标签回调。它可以提供JPEG，但要采集原厂表情标签，还需另行部署本分支构建的配套主payload；便携`device.py start`不会替换日常模块。状态明确区分JPEG可用、标签回调可用，以及实际观察到标签，不能把连接成功当作已经录到表情。

精确关联还要求主payload导出`picoet_stock_input_identity_version()`并返回1。只有旧标签sink的开发件仍只发送未关联诊断；当前配套件显示`association_ready_not_device_verified`，表示机制已接线，实际设备验收仍待完成。

```powershell
python tongue-source/build.py android --ndk <NDK目录> --cmake <cmake可执行文件> --ninja <ninja可执行文件>
```

输出在`tongue-source/build-android/`：`picoet-inject`、`libpicoet_camera_jpeg.so`、`libptj_source.so`、`ptj_ipc_daemon`及模块控制器`picoet-camera`，不以设备库或旧编译对象为输入。已有libjpeg-turbo 3.1.2源码可用`--jpeg-source`选择，离线构建。

需要配套主payload时，在配置好的build目录另构建`picoet_hook`目标，产出`tongue-source/build-android/hook/libpicoet_hook.so`。三路相机adapter、shim与daemon使用PTJIPC4，须一起重建；图像PTJ1保持不变。

首次注入前停止所有头显眼／面部客户端，使用自己的ADB序列号：

```powershell
python tongue-source/device.py start --serial <serial> --state pc-tongue/user-data/source-session.json
```

ADB不在PATH时用`--adb <可执行文件>`。命令上传到本轮唯一root私有目录，仅将两份DSO标为`system_file`，通过现有PicoET控制锁注入，启动daemon，发送绑定owner的start命令并核对IPC身份／nonce ACK。不安装模块、不改SELinux策略、不重启服务。Android端在第一个图像或标签订阅到来时取得本服务的原生追踪租约，最后一个退订后释放；未被订阅的图像通道不复制、不编码。LAN TCP没有加密或认证，只在可信私网使用，不向外网暴露或转发端口。

保留state文件，其中绑定设备、boot、服务实例和远端目录。查询及停止使用同一个文件，不沿用记忆中的PID：

```powershell
python tongue-source/device.py status --serial <serial> --state pc-tongue/user-data/source-session.json
python tongue-source/device.py stop --serial <serial> --state pc-tongue/user-data/source-session.json
```

Stop先disarm并排空回调，再确认shim的join后ACK才标记停止。禁用的adapter／shim仍驻留，不要`dlclose`。成功停止后再次Start使用**同一个state文件**，复用adapter、新建daemon／session。缺ACK时保留映射、禁止重新attach；hook安装失败在该服务实例中为终态，应停止本轮未attach的daemon、保留诊断，不反复注入。正常重启服务或头显后改用新的state文件。远端会话文件留作诊断，不提供广泛清理命令。

整合控制器已在PICO OS 5.13.7／2.5.2基线／SELinux Enforcing下通过启动、PTC1／时钟握手、排空和join后ACK检查。真实图像／标签配对及佩戴输入仍待验收。

## 3. 连通全链路

1. 按[接收端说明](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE_zh.md)构建、安装配套UnifiedPicoModule分支。
2. 启动正常PICO面捕客户端，例如PicoFacialBridge；测试整嘴回归时让VRCFT将面部输出分配给PICO。
3. PC App选中发现的头显，用“选择文件”选取整嘴`.onnx`后启动推理；准备发送结果时启用本机UDP45682。只有采集需求时，不加载模型、不发送有效模型结果。
4. 在追踪页查看画面；模型通道和源／延迟／队列读数位于设置页的诊断详情，后端、时序和端口位于连接与性能。中英文切换保留当前页、未保存输入和展开状态。

App不选择PICO／Cymple嘴部来源。日常Cymple嘴＋PICO眼时保持本PICO视频App停止，接收器也会按宿主所有权阻止外部嘴部写入。MFR1只覆盖有效模型字段，未知值不当中性测量；invalid／停止／过期退出模型覆盖，原厂通道恢复本帧PICO值。眼部保持原路径。

### 整嘴模型契约

schema为`pico-mouth50-v1`，字段顺序由`pc-tongue/face_schema.py`定义：MouthLink原44字段，再追加上下唇各自左右横移四项和上下Funnel两项。保留旧聚合head，不从单个原厂量伪造独立上下／左右。

同名JSON声明schema、`channels`、输入`width/height`、`trained_channels`、`validated_channels`和`valid_mask`。训练记录优化覆盖，只有通过独立验证的通道才可置输出位；运行端也遵守显式的零掩码或更窄掩码。不传`--approval`的导出保留训练来源，输出有效位为零。预处理为完整灰度图的Pillow `BILINEAR`缩放后除以255，归一化由模型完成，不使用旧sqrt／mask几何，也不做侧视图水平翻转。

MFR1直接发送命名连续控制值，不套用旧TJR方向.15–.55映射、伸舌硬阈值或Out方向相乘。旧TJR只由配套接收器保留数值兼容。输入源仍是400×400／90Hz JPEG95；模型输入尺寸在PC决定，模型必须在实际压缩域及不同佩戴上验证。

## 4. 本地录制与教师蒸馏

录制需要单独同意，与启动预览或UDP输出分开。原厂raw33、已门控instant33、final33及质量2项分层保存，并按真实源身份精确配对；不取“最近一次表情”。缺图／缺标签保留账本，来源关系未确认的记录不冒充有效训练对。

取图前先从同一endpoint完成PTC1上下文握手，绑定本轮capture session与服务代际，每帧再检查session。原厂输入时间在已审计的camera5单合成图路径中原样来自父sensor；标签按唯一精确来源关联，不能按vendor固定偏移或最近时间猜测。实际设备的当前库、模式与配对仍需验收。

动作引导覆盖原有口型、独立脸颊／上下唇、舌头短中长／方向／筒形Roll、组合动作和困难负例。过渡段排除，做不到或不可见可跳过；提示标签需要审核，未标注字段为未知，不自动补零。特别是门控TongueOut=0，不证明图中没有轻微伸舌。

进入**采集**页，连接头显前就能对照“自然放松”和目标参考图练习。用“上一个／下一个”切换，完整动作列表在“选择其他表情”中；侧面图显示嘴唇与下颌的前后深度，动画在点击“播放连续动作”后播放。正面图和照镜子一样，画面左侧是你自己的左侧。录制过程中，参考卡自动跟随当前录制步骤，不受练习时的选择影响。浏览图片不会开始采集，也不会批准标签。更多录制选项和逐图审核分别收在本页的折叠入口里。

录制前先选**采集预设**，展开**动作顺序**可以看到每一步。进度条显示当前步骤和所选计划的总步数，例如`7 / 12`。录制开始后预设锁定；第一帧到达前，实际计划已写入本轮manifest。

| 预设 | 步数 | 内容 |
|---|---:|---|
| 基础嘴部 | 12 | 放松、开闭嘴、左右微笑、噘嘴、漏斗、鼓腮、下颌左右移、伸舌、放松 |
| 舌头专项 | 11 | 放松、短中长伸舌、左右上下、舌侧卷筒、舌尖弯曲对照、放松 |
| 独立唇形 | 13 | 放松、上下唇横移、内卷、上下漏斗、噘嘴、左右压唇、放松 |
| 完整引导（默认） | 73 | 全部命名控制、组合动作、三档伸舌、对照动作和重新佩戴提示 |

选好头显、预设并勾选同意后，点击**开始录制**。每步先看示范模仿3秒，再保持到倒计时结束，界面自动切换下一步；准备时播放动画，保持时显示静态图。请让示范留在窗口内，切页、图片未加载或输入中断时，保持计时不会完成。可以**暂停／继续**以重新准备当前动作，点**跳过**进入下一步，或点**完成录制**提前保存。最后一步结束后自动停录。暂停引导期间，原始录制仍在继续。

正常`start.cmd`入口会在点击**开始录制**时保存当前选择并打开局域网输入，结束后关闭本客户端的订阅；启动和归档期间分别显示打开中、保存中状态。也可先点**打开画面**检查位置，或在已有实时整嘴追踪中开始录制，沿用同一输入连接和模型设置。回放需先停止，才能录制实时画面。选择或预览预设不会录图，也不会批准标签。取消勾选“按步骤引导我”即为自由录制，需手动结束。

图片采用原版[Unity-Movement HighFidelity人物](https://github.com/oculus-samples/Unity-Movement/tree/v71.0.0/Samples~/BodyTrackingSamples/HighFidelity)的脸部近景。73个阶段中71个有图片，覆盖全部50项命名控制，并提供70个可选动画和25个侧视图。遮挡及重新佩戴没有图片，按文字说明操作。

来源栏区分两类示范：48个阶段使用原始形态键及官方修正规则，另外23个阶段在原网格上加入了示范控制。舌头增加了蒙皮和连续弯曲，舌侧卷筒与仅弯舌尖分别示范；上下唇横移在原整嘴横移形态的水平位移上分区，另一片嘴唇和下颌保持原位。原网格和已有形态键的坐标保持不变。这些图用来帮助你理解动作，训练标签来自你的审核。舌头和独立唇横移由新增的示范控制呈现，因为Meta原模型没有对应的形态键。图片包附带出处和素材许可，不含原模型或SDK代码。

教师／学生训练入口：

```powershell
pc-tongue/.venv/Scripts/python.exe pc-tongue/face_train.py --help
```

流程为自有数据微调教师→教师产软标签→直接监督学生／蒸馏对照→ONNX导出。DINO骨干代码与checkpoint使用显式本地路径，不自动下载；正式训练需要你自己的有效数据和权重。未知标签用mask排除，人工审核标签优先于教师预测，训练／验证按人物和佩戴会话分开。

CLI子命令包括`split`、`student-baseline`、`teacher-train`、`validate-candidate`、`predict-pseudo`、`student-distill`、`local-adapt`、`evaluate`和`export`，网页训练表单读取同一套参数。先对已停录并审核的数据冻结划分：单人使用`--group-by session`，跨人研究使用`person`，至少三个独立组分别用于训练、验证和封存测试。需要独立训练环境时安装`requirements-training.txt`；不要把其中的CPU ORT与现有DirectML／CUDA发行包混装。

教师先用`teacher-train --dino-repo ... --backbone-checkpoint ... --tune frozen`训练头，再按需从`--initial`继续`--tune last-two`。`validate-candidate`与单独训练的学生基线比较；`predict-pseudo`要求该教师的接纳报告，只向训练组写软标签。`student-distill`／`local-adapt`使用这些软标签，不回流验证／测试图。导出选择新的ONNX／JSON路径，不覆盖已有模型；候选不会自动启用，验证后在模型路径中显式选择。

教师头默认学习率为`1e-4`，学生为`1e-3`。学生默认输入160×160；使用`--student-input 224`可在同一MobileNetV3-small架构上比较更高分辨率。续训默认继承checkpoint的输入尺寸，也可显式覆盖。

训练默认使用掩码MSE、不增强。`--loss huber`可对照beta为0.1的Smooth L1；`--augmentation mild`增加小幅平移／旋转、曝光／gamma及噪声，不做镜像。在相同冻结划分上每次只改一项。派生语料可用逐通道`target_weights`区分来源权重；审核纠错替换冲突的弱标签，不叠加第二份相反监督。允许输出的导出需通过`export --approval <report.json>`提供该模型对应的已接纳验证报告。

默认教师为392×392的DINOv2 ViT-B/14-reg。DINOv3支持ViT-B/16、ViT-L/16和ViT-H+/16，分别使用`--teacher-backbone dinov3`、`dinov3-large`和`dinov3-huge`，并提供对应的本地源码和checkpoint；三者均使用完整400×400图像。`--initial`续训、验证和伪标签推理继承已保存的教师类型。DINOv3源码与权重遵循[DINOv3许可](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md)。

个人适配时，将已经微调好的教师分发到用户PC，对其本地嘴图产软标签，再训练个人学生；日常只运行学生。图像、软标签和个人权重不上传。只在开发者数据上蒸馏一次不能保证保留教师对所有新人的泛化；教师在该用户身上的错误也不会因蒸馏自动纠正。

回放可用自有NPZ：`images[N,400,400]`为uint8、`seq[N]`、`times[N,3]`为源纳秒；可选`vendor_ns`、`raw_identity`保留对应身份，在设置页的“回放已保存的录制”中选择。分支不含个人照片或回放数据。

## 离线验证

```powershell
pc-tongue/.venv/Scripts/python.exe pc-tongue/check.py
```

Python测试包含真实个人ONNX导出；双语网页测试需要Node.js。Native host测试从x64 Visual Studio开发终端构建，或传`--vcvars <vcvars64.bat>`：

```powershell
python tongue-source/build.py host --cmake <cmake可执行文件> --ninja <ninja可执行文件>
python tongue-source/checks/test_ipc.py
python tongue-source/checks/test_borrowed.py
python tongue-source/checks/test_camera.py
python tongue-source/checks/test_device.py
```

这些仅使用合成图、临时文件／进程和环回socket，不调用ADB。当前分支完成源码构建与离线覆盖；真人持续新源、个人准确性、原厂眼嘴并发及VD共存仍待真机验收。

## 上游来源与范围

相机／PC链路起步于Rulez777的[PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4)，审阅版本`f5f7894c6b30a86fd4699d721f966b6a01861443`。其分割／几何、mask head训练和旧校准仅保留作历史回归，不作为当前整嘴推理或教师。

这是选择性源码整合，不合并整个研究历史。未引入训练照片、演示媒体、端侧推理／校准App或UDP19031，不在这里重新分发作者个人权重。见[第三方声明](THIRD_PARTY_NOTICES.md)。

整嘴回归契约、Pillow预处理和同源标签思路参考UndefinedSignal的[issue #5](https://github.com/WolalaQAQ/PicoET-Enhance/issues/5)与MIT许可MouthLink，审阅版本`39e091f4191d2ea1ad8536120924cdf61949dae2`。50字段扩展、标签掩码和教师／学生工具不表示上游个人模型已具备新增能力。
