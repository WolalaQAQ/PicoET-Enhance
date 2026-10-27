# PC舌头追踪：开发分支

[English](TONGUE.md) | 简体中文

本仓库与[UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc)配套使用`feat/tongue-pc`。直接从源码构建相机传输、运行PC App、构建配套接收器即可内测；原Magisk ZIP及其构建流程不变，不会安装这组附加组件。

```text
PICO camera5嘴图lane（400×400灰度、目标90Hz真实新源）
  → 同步保护复制到有界共享槽
  → 独立root daemon，JPEG95，TCP45681／PTJ1
  → PC解码、FP32 ONNX分割、校准／滞回
  → 本机UDP45682／TJR1 → UnifiedPicoModule → VRCFT
```

相机服务不监听TCP、不运行舌头模型。编码器借用共享槽直到编码／发送结束，省掉第二次整图暂存复制；第一次相机保护复制保留。不用回调计数替换真实帧身份或sensor时间。camera5只有一个合成父图sensor时间，不是三路独立测得的曝光时间。

## 1. 准备PC App

已使用Windows、Python 3.12与.NET 10验证。在仓库根目录执行：

```powershell
python -m venv pc-tongue/.venv
pc-tongue/.venv/Scripts/python.exe -m pip install -r pc-tongue/requirements.txt
pc-tongue/.venv/Scripts/python.exe pc-tongue/prepare_model.py
pc-tongue/start.cmd
```

打开`http://127.0.0.1:8787`。Windows默认使用ONNX Runtime DirectML，可用DirectX 12显卡，不要求NVIDIA。网页启动时不采集，UDP默认关闭；自行输入头显地址，没有固定本机IP。

`prepare_model.py`下载作者固定的`v2.4.2-tongue.1`存档，只提取固定`shadow-final-v2.pt`并导出、校验FP32 ONNX，不解压训练照片、不执行上游脚本。已有存档可用`--archive <路径>`避免下载。模型留在忽略的`pc-tongue/assets/`，不会覆盖不同checkpoint或已有ONNX；需要另一份时用`--output`选择新目录，再在网页选对应`.pt`。

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

需要已Root的PICO 4 Pro／PICO OS 5.13.7，并安装正常PicoET-Enhance 2.4.2。适配器在hook前检查支持的系统库文件及指令窗口。工具链为NDK 26.1.10909125、CMake 3.22.1、Ninja；libjpeg-turbo 3.1.2独立构建静态库，ShadowHook直接编译仓库内已有vendored源码。

```powershell
python tongue-source/build.py android --ndk <NDK目录> --cmake <cmake可执行文件> --ninja <ninja可执行文件>
```

四个输出在`tongue-source/build-android/`：`picoet-inject`、`libpicoet_camera_jpeg.so`、`libptj_source.so`、`ptj_ipc_daemon`，不以设备库或旧编译对象为输入。已有libjpeg-turbo 3.1.2源码可用`--jpeg-source`选择，离线构建。

首次注入前停止所有头显眼／面部客户端，使用自己的ADB序列号：

```powershell
python tongue-source/device.py start --serial <serial> --state pc-tongue/user-data/source-session.json
```

ADB不在PATH时用`--adb <可执行文件>`。命令上传到本轮唯一root私有目录，仅将两份DSO标为`system_file`，通过现有PicoET控制锁注入，启动daemon，发送绑定owner的start命令并核对IPC身份／nonce ACK。不安装模块、不改SELinux策略、不重启服务、不启动追踪客户端；没有JPEG订阅者时源不复制图像。LAN TCP没有加密或认证，只在可信私网使用，不向外网暴露或转发端口。

保留state文件，其中绑定设备、boot、服务实例和远端目录。查询及停止使用同一个文件，不沿用记忆中的PID：

```powershell
python tongue-source/device.py status --serial <serial> --state pc-tongue/user-data/source-session.json
python tongue-source/device.py stop --serial <serial> --state pc-tongue/user-data/source-session.json
```

Stop先disarm并排空回调，再确认shim的join后ACK才标记停止。禁用的adapter／shim仍驻留，不要`dlclose`。成功停止后再次Start使用**同一个state文件**，复用adapter、新建daemon／session。缺ACK时保留映射、禁止重新attach；hook安装失败在该服务实例中为终态，应停止本轮未attach的daemon、保留诊断，不反复注入。正常重启服务或头显后改用新的state文件。远端会话文件留作诊断，不提供广泛清理命令。

原native生命周期在源码整合前已通过真实服务域验证；这份新便携控制器覆盖fake-device测试，未追加真机运行。

## 3. 连通全链路

1. 按[接收端说明](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE_zh.md)构建、安装配套UnifiedPicoModule分支。
2. 启动正常PICO面捕客户端，例如PicoFacialBridge；测试本舌头源时让VRCFT将面部输出分配给PICO。
3. PC App填头显地址和`.pt`路径后Start，ORT读取旁边的`.onnx`；准备发送结果时启用本机UDP45682。
4. 检查新鲜输入及Out／Left／Right。后端、时序、UDP、回放留在高级设置，源／延迟／队列留在折叠诊断。中英文切换保留未保存输入及展开状态。

App不选择PICO／Cymple嘴部来源。日常Cymple嘴＋PICO眼时保持本PICO视频App停止，接收器也会按宿主所有权阻止外部嘴部写入。明确absent三项归零，invalid／stop／过期恢复当前原厂Out并清Left／Right，不提供Up／Down。64字节TJR1契约见接收端说明。

## 个人校准与重训

需要新鲜**实时**图像，回放不能校准。展开个人设置，选择校准，按佩戴调整→闭嘴→张嘴→舌头居中→左→右进行，可选浏览器语音。校准只保存角度及面积阈值，不保存录样。

重训需单独勾选本地录样／训练同意。冻结encoder，仅CPU训练3,777参数mask head，900步Adam，正例加权BCE＋Dice；每姿态第4个样本留出。候选要求方向分离、`TP−2×FP`改善至少.05、FP增加不超过.02，完成导出／数值检查及实际推理worker预热后才提交。拒绝时保留活动模型。短期姿态留出不等于跨会话泛化证据。

个人profile／任务位于配置旁的`pc-tongue/user-data/personalization/`，不覆盖标准权重。恢复标准重置个人head／校准，保留任务文件；取消／恢复会隔离旧训练完成、预测和预览。面积滞回使用个人出现阈值，保持阈值为60%。配置、数值日志和录样留在PC，无上传接口；只有显式传入后端`--record-input`才录制完整实时JPEG。

回放可用自有NPZ：`images[N,400,400]`为uint8、`seq[N]`、`times[N,3]`为源纳秒；可选`vendor_ns`、`raw_identity`保留对应身份，在高级设置中选择。分支不含个人照片或回放数据。

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

基于Rulez777的[PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4)，审阅版本`f5f7894c6b30a86fd4699d721f966b6a01861443`。PC模型结构／预处理／几何沿用`04fe68ac88e8c1fcaf504eba8223ebf33c629d93`的`research/tongue-v2/training/shadow_model.py`；校准、mask head训练／接纳及面积滞回将tongue.2行为移到PC，增加事务启用与源新鲜度处理。

这是选择性源码整合，不合并整个研究历史。未引入训练照片、演示媒体、端侧推理／校准App或UDP19031；标准权重从作者Release取得，不在这里重新分发。见[第三方声明](THIRD_PARTY_NOTICES.md)。
