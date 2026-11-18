# PC end-to-end whole-mouth tracking — development branch

English | [简体中文](TONGUE_zh.md)

Use `feat/tongue-pc` in this repository and [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc). This branch's module bundles optional three-camera LAN transport controlled from the companion home screen. Prepare the PC app and matching receiver separately from source.

```text
PICO camera5 mouth lane (400×400 grayscale, fresh-source target 90 Hz)
  → synchronous protection copy into a bounded shared slot
  → separate root daemon, JPEG quality 95, TCP45681 / PTJ1
  → PC: decode → continuous whole-mouth ONNX regression
  → local UDP45682 / MFR1 with per-field validity → UnifiedPicoModule → VRCFT
```

The vendor camera service does not listen on TCP or run the tongue model. The encoder borrows the protected shared slot until its last pixel read, avoiding a second full-image staging copy. The first camera protection copy is retained. Real frame identity and sensor timestamps are not replaced by callback counters. Camera5 supplies one composite parent sensor time, not three independently measured exposure times.

The source also exposes independent left-eye and right-eye streams. Other clients can discover and subscribe through the same [LAN camera protocol](CAMERA-PROTOCOL.md), using PTJ1 or standard HTTP MJPEG. The default ports are mouth45681, left45682 and right45683; each stream supports eight concurrent image consumers.

## 1. Prepare the PC app

Tested with Windows, Python 3.12 and .NET 10. From this repository's root:

```powershell
python -m venv pc-tongue/.venv
pc-tongue/.venv/Scripts/python.exe -m pip install -r pc-tongue/requirements.txt
pc-tongue/start.cmd
```

Open `http://127.0.0.1:8787`. The default Windows environment uses ONNX Runtime DirectML; it works with DirectX 12 GPUs without requiring NVIDIA. The page starts idle and UDP output is off.

The sidebar switches between four task pages: **Tracking** selects a headset and model and starts tracking; **Capture** selects a headset and preset, then records with the expression guide after you tick the consent box, with an optional camera preview first; **Train** explains how to prepare data and provides advanced training tools; **Settings** holds output, connection, replay and diagnostics. Switching pages keeps unsaved input and any running session.

The main page looks for matching sources on the local network and selects their mouth stream. A single result is selected automatically; choose from the list when several are found, or click **Find again** to search again. Discovery uses broadcast and multicast on UDP45681, verifies the camera catalog over HTTP and checks the owner through PTC1, without subscribing to images or labels. Legacy mouth-only discovery remains supported. If your network isolates discovery traffic, enter the address and port under Settings → Connection and performance.

Click **Choose file** to open the PC's file dialog and select a whole-mouth `.onnx`; its same-name `.json` manifest goes in the same folder. The page shows the file name and full path, and cancelling keeps the previous model. Choosing a device or file updates the draft; settings are saved when you start or click Save.

Inference needs a local whole-mouth `.onnx` and a `.json` manifest with the same name. This branch bundles no validated general-purpose mouth or teacher weights and downloads none. Without a model, open the camera on the Capture page and record your first images and labels; recording does not need a trained model. The old `prepare_model.py` and the segmentation/geometry code remain for history and regression checks and are not part of the current setup.

### Other backends

Create a **separate venv** for each ORT distribution; do not install DirectML, GPU, OpenVINO and CPU ORT packages together. Choose exactly one dependency file:

| Environment | Requirements |
|---|---|
| Windows default / DirectML | `pc-tongue/requirements.txt` |
| CPU | `pc-tongue/requirements-cpu.txt` |
| NVIDIA CUDA 12 | `pc-tongue/requirements-cuda.txt` |
| Intel OpenVINO GPU | `pc-tongue/requirements-openvino.txt` |

For the tested CUDA 12.6 setup, install PyTorch's matching wheels first in the new venv:

```powershell
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r pc-tongue/requirements-cuda.txt
```

Run that environment's Python against `pc-tongue/backend.py`. `auto` tries installed CUDA Graph → CUDA → OpenVINO GPU → DirectML → CPU, including first-execution checks. Explicit backend choices are strict. Actual providers and initialization failures appear in folded diagnostics. DirectML has been checked on NVIDIA hardware; AMD/Intel live hardware results remain for testers.

## 2. Build and start the headset source

With this branch's complete module and companion installed, turn on the master switch in **PicoET-Enhance → LAN broadcast**. It defaults off and remembers your choice; the tab reports actual status, stream addresses and client counts. Close other eye/face clients once before first attachment, then let clients subscribe as needed. The **Post-processing** tab shows the original parameter panel.

To include headset mouth results, also turn on **Mouth** under **Result sharing** in the companion. In the PC workbench, **Recording → More recording options → Receive headset mouth results** controls the subscription for that recording. Turn the PC option off for image-only recording; preview never subscribes to results.

The standalone `device.py` path below is for development diagnostics; do not run it alongside the module-managed camera service.

Use rooted PICO 4 Pro / PICO OS 5.13.7. This branch includes the PicoET-Enhance 2.5.2 base, including its saved postprocessing settings. The adapter checks its supported system-library file and instruction window before hooking. Build inputs are Android NDK 26.1.10909125, CMake 3.22.1 and Ninja. libjpeg-turbo 3.1.2 is fetched and built as a separate static-library project; ShadowHook is compiled from this repository's existing vendored source.

The official 2.5.2 main payload has no stock-label callback. JPEG remains available, but stock labels require a separately deployed matching main payload from this branch; portable `device.py start` does not replace the installed module. Status distinguishes JPEG capability, label callback capability and actual observed labels. A successful connection is not evidence that expressions were recorded.

Exact association also requires the main payload's `picoet_stock_input_identity_version()` export to return 1. Older development payloads with only the label sink still produce unassociated diagnostics. The matching build reports `association_ready_not_device_verified`: the mechanism is connected, while live-device acceptance remains pending.

```powershell
python tongue-source/build.py android --ndk <NDK-directory> --cmake <cmake-executable> --ninja <ninja-executable>
```

The outputs are in `tongue-source/build-android/`: `picoet-inject`, `libpicoet_camera_jpeg.so`, `libptj_source.so`, `ptj_ipc_daemon` and the module controller `picoet-camera`. No device libraries or old compiled objects are build inputs. An existing libjpeg-turbo 3.1.2 source checkout can be selected with `--jpeg-source` for an offline build.

For the matching main payload, build the `picoet_hook` target separately in the configured build directory; its output is `tongue-source/build-android/hook/libpicoet_hook.so`. Update it with the camera adapter, shim, controller and daemon for PTJIPC5. See [eye result subscriptions](CAMERA-PROTOCOL.md#eye-expression-results-pse1); the mouth workbench does not record PSE1.

Stop all headset eye/face clients before the first injection. Use your own ADB serial:

```powershell
python tongue-source/device.py start --serial <serial> --state pc-tongue/user-data/source-session.json
```

`--adb <executable>` selects an ADB outside PATH. The command uploads into a unique root-private session directory, labels only its two DSOs `system_file`, serializes injection with the existing PicoET control lock, starts the daemon, sends an owner-bound start command and verifies IPC identity/nonce ACKs. It does not install a module, change SELinux policy or restart the service. On Android, the first image or label subscription starts this source's native tracking lease; the last unsubscribe releases it. Unsubscribed lanes are not copied or encoded. The LAN TCP stream is unencrypted and unauthenticated: use a trusted private network, not an exposed interface/port-forward.

Keep the state file. It identifies the device, boot, service instance and remote directory; subsequent commands use it rather than a remembered PID. For status and stop:

```powershell
python tongue-source/device.py status --serial <serial> --state pc-tongue/user-data/source-session.json
python tongue-source/device.py stop --serial <serial> --state pc-tongue/user-data/source-session.json
```

Stop disarms and drains callbacks, then checks the shim's join ACK before marking the session stopped. The disabled adapter/shim remain resident; do not `dlclose` them. Start with the **same state file** to reuse a successfully stopped adapter with a new daemon/session. Missing ACKs preserve the mapping and block reattach. A hook-install failure is terminal in that service instance: stop the owned unattached daemon and retain diagnostics rather than injecting repeatedly. After a normal service restart or reboot, use a new state file. Remote session files are retained for debugging; there is no broad cleanup command.

The integrated controller has passed start, PTC1/clock handshake, drain and join-ACK checks on PICO OS 5.13.7 with the 2.5.2 base and SELinux enforcing. Live image/label pairing and worn input remain pending.

## 3. Run end to end

1. Build/install the matching UnifiedPicoModule branch following its [receiver guide](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE.md).
2. Start your normal PICO face-tracking client, such as PicoFacialBridge. Let VRCFT assign face output to PICO when testing whole-mouth regression.
3. Select the discovered headset and use **Choose file** to select the whole-mouth `.onnx`, then start inference. Enable local UDP45682 when ready to feed the receiver. Capture-only operation loads no model and sends no valid model controls.
4. Watch the camera on Tracking. Model channels and source/latency/queue readings are in Settings → Diagnostics; backend, timing and ports are in Connection and performance. English/Chinese switching preserves the current page, unsaved input and expanded sections.

The app never selects PICO versus Cymple mouth ownership. In Cymple-mouth / PICO-eyes use, leave this PICO video app stopped; the receiver's ownership check also prevents foreign mouth writes. MFR1 overrides supported fields only; unknown values are not neutral measurements. Invalid/stop/expiry release model overrides and restore the current PICO values for stock channels. Eyes stay on their existing path.

### Whole-mouth model contract

The schema is `pico-mouth50-v1`, ordered by `pc-tongue/face_schema.py`: MouthLink's original 44 fields followed by four independent upper/lower lip horizontal controls and two upper/lower Funnel controls. Preserve the old aggregate heads rather than inventing independent parts from a single stock value.

The adjacent JSON declares the schema, `channels`, input `width/height`, `trained_channels`, `validated_channels` and `valid_mask`. Training records optimization coverage; only independently validated channels may have an output bit. Runtime also obeys an explicit zero or narrower mask. Export without `--approval` produces a zero-mask candidate, retaining its training provenance. Preprocessing resizes the full grayscale image with Pillow `BILINEAR` and divides by 255; normalization is inside the model. There is no legacy sqrt/mask geometry or horizontal flip of this side-view image.

MFR1 sends named continuous controls directly, without legacy TJR .15–.55 remapping, tongue-presence thresholds or multiplying directions by Out. The matching receiver retains numeric TJR compatibility only. The source remains 400×400 / 90 Hz JPEG95; the PC chooses model input size. Evaluate models in the actual compression domain and on independent headset fits.

## 4. Local recording and teacher/student distillation

Recording requires separate consent, independent of preview or UDP output. Preserve stock raw33, gated instant33, final33 and quality2 separately and pair by exact source identity, not the latest available expression. Keep missing-image/label accounting; unconfirmed source associations are not valid training pairs.

Before opening video, complete the PTC1 context handshake on the same endpoint, bind the capture session/service generation and check every frame's session. In the audited single-composite camera5 path, stock input time is copied unchanged from the parent sensor time. Labels require unique exact source association, not a fixed vendor offset or nearest-time guess. The actual device's libraries, mode and pairing still need acceptance.

Guidance covers existing mouth controls, independent cheeks/upper/lower lips, short/medium/full tongue extension, directions/tube Roll, combinations and hard negatives. Exclude transitions and skip actions that cannot be performed or observed. Prompt targets need review; unspecified fields remain unknown rather than zero. In particular, a gated TongueOut zero does not establish that the image contains no slight tongue extension.

Open **Capture** to practice with the relaxed and target reference images before connecting a headset. Use **Previous** and **Next**, or pick from the full list under **Pick another expression**. Side views show lip and jaw depth, and movements play when you click **Play movement**. Front images work like a mirror: the left side of the image is your own left. While recording, the reference card follows the current recording step instead of your practice selection. Browsing images does not start capture or approve labels. More recording options and image review have their own folded sections on this page.

Choose a **Capture preset** before recording, and expand **Actions in order** to see every step in sequence. The progress bar shows the current step and the selected plan's total, such as `7 / 12`. The preset is locked once recording starts, and the actual plan is saved in the recording manifest before the first frame arrives.

| Preset | Steps | Contents |
|---|---:|---|
| Mouth basics | 12 | Relaxation, opening/closing, left/right smiles, pucker, funnel, cheeks, jaw shifts, tongue extension, relaxation |
| Tongue movements | 11 | Relaxation, short/medium/full extension, left/right/up/down, side roll, tip-curl contrast, relaxation |
| Independent lip movements | 13 | Relaxation, upper/lower horizontal shifts, inward rolls, upper/lower funnels, pucker, left/right presses, relaxation |
| Complete guide (default) | 71 | Every named control, combined movements, three tongue-extension lengths, and mouth shapes with the tongue inside, speaking and a tongue-tip curl |

Choose the headset, preset and **Step advance** mode, tick the consent box, then click **Start recording**. The live mouth image stays beside the demonstration. **Automatic** gives you seven seconds to prepare by default (adjustable from 3 to 30), then times the hold and advances. Keep the demonstration visible for timed steps. In **Manual**, click **Start holding** when ready, then **Finish holding** and **Confirm action done**. The last step saves the recording.

Use **Previous step**, **Next step**, or **Go to step** to change the active step or choose where to start. Moving without confirming leaves the previous step unfinished; **Skip** explicitly leaves it out. **Pause** suspends the guide but keeps recording. If you finish early or the camera disconnects, **Continue last recording** offers the remaining steps after the current segment is saved. It starts a separate segment after you consent and click **Start recording** again. Mark covered or unclear mouths in **Review recorded images**, not during the guide.

The normal `start.cmd` entrypoint saves your current selection and opens LAN input when you click **Start recording**, then closes this client's subscription when recording ends. The page shows opening and saving states while those operations finish. You can also **Open camera** to check the view first, or record during existing live whole-mouth tracking, keeping the same input connection and model settings. Stop replay before recording live images. Selecting or previewing a preset records nothing and approves no labels. Untick **Guide me step by step** for free recording, then finish it manually.

The image pack uses face close-ups of the original [Unity-Movement HighFidelity character](https://github.com/oculus-samples/Unity-Movement/tree/v71.0.0/Samples~/BodyTrackingSamples/HighFidelity). It illustrates all 71 stages, including all 50 named controls, with 70 optional movement animations and 25 side views. To add variety across headset fits, take the headset off, refit it and record again, ideally on another day.

The source label separates 48 stages made with the original morphs and official corrective rules from 23 stages that use guide controls added on the original mesh. Tongue examples add skin binding and continuous bending, with separate side-rolling and tip-curl examples. Independent upper and lower lip shifts apply masked horizontal displacement to the original mouth-shift morphs, while the other lip and the jaw stay in place. The original mesh and morph coordinates are unchanged. These images help you copy the movement; training labels come from your review. The tongue and independent lip movements are shown with added guide controls because the original Meta model has no matching morphs. Source and asset-license notices are included; the original model and SDK code are not bundled.

Teacher/student training entry:

```powershell
pc-tongue/.venv/Scripts/python.exe pc-tongue/face_train.py --help
```

The sequence is teacher fine-tuning on owned data → teacher soft labels → direct-student/distillation comparison → ONNX export. DINO backbone code and checkpoints use explicit local paths, with no automatic downloads. Real training needs your valid data and weights. Mask unknown targets, prioritize reviewed labels over teacher predictions, and split by person/headset-fit session.

CLI operations are `split`, `student-baseline`, `teacher-train`, `validate-candidate`, `predict-pseudo`, `student-distill`, `local-adapt`, `evaluate` and `export`; the web training form reads the same arguments. Freeze a split from stopped, reviewed recordings first: use `--group-by session` for one person or `person` for cross-person research, with at least three independent groups for training, validation and sealed testing. Install `requirements-training.txt` in a separate training environment if needed; do not mix its CPU ORT distribution with an existing DirectML/CUDA distribution.

Start the teacher with `teacher-train --dino-repo ... --backbone-checkpoint ... --tune frozen`, then optionally continue from `--initial` with `--tune last-two`. Compare against a separately trained student baseline using `validate-candidate`. `predict-pseudo` requires that teacher's approval and labels only training groups. `student-distill`/`local-adapt` use those soft labels without feeding validation/test images back into training. Export to a new ONNX/JSON pair rather than overwriting a model. Candidates are not activated automatically; select the validated ONNX explicitly in the model path.

The default head learning rate is `1e-4` for teachers and `1e-3` for students. Students use 160×160 inputs by default; `--student-input 224` enables a higher-resolution comparison with the same MobileNetV3-small architecture. Continuation inherits the checkpoint's input size unless explicitly overridden.

Training defaults to masked MSE and no augmentation. `--loss huber` compares Smooth L1 with beta 0.1; `--augmentation mild` adds small shifts/rotations, exposure/gamma changes and noise without mirroring. Change one option at a time on the same frozen split. Derived corpora may supply per-channel `target_weights`; reviewed corrections replace conflicting weak targets rather than adding a second loss. For an approved export, pass the exact model's accepted validation report through `export --approval <report.json>`.

The default teacher is DINOv2 ViT-B/14-reg at 392×392. DINOv3 supports ViT-B/16, ViT-L/16 and ViT-H+/16 through `--teacher-backbone dinov3`, `dinov3-large` and `dinov3-huge`, respectively. Supply the matching local source and checkpoint; all three use the full 400×400 image. Continuation through `--initial`, validation and pseudo-label inference inherit the saved teacher kind. DINOv3 source and weights are covered by the [DINOv3 License](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md).

For personal adaptation, distribute the fine-tuned teacher to the user's PC, generate soft labels from their local images and train their student. Run only the student during normal use. Images, soft labels and personal weights are not uploaded. Distillation on developer data alone does not guarantee all of the teacher's new-user generalization; distillation does not automatically correct teacher errors on that user.

Replay accepts your own NPZ with `images[N,400,400]` uint8, `seq[N]`, and `times[N,3]` source nanoseconds; optional `vendor_ns` and `raw_identity` preserve those identities. Select it under Settings → Replay a saved recording. No personal photos or replay dataset is included in the branch.

## Offline verification

```powershell
pc-tongue/.venv/Scripts/python.exe pc-tongue/check.py
```

Python checks include real personal-model ONNX export; Node.js is needed for the bilingual UI tests. For native host checks, build from an x64 Visual Studio developer shell (or pass `--vcvars <vcvars64.bat>`):

```powershell
python tongue-source/build.py host --cmake <cmake-executable> --ninja <ninja-executable>
python tongue-source/checks/test_ipc.py
python tongue-source/checks/test_borrowed.py
python tongue-source/checks/test_camera.py
python tongue-source/checks/test_device.py
```

These use synthetic images, temporary files/processes and loopback sockets, never ADB. The current branch has source builds and offline coverage; sustained worn camera input, personal accuracy, original eye/mouth concurrency and VD coexistence still need live acceptance.

## Upstream source and scope

The camera/PC integration started from Rulez777's [PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4), reviewed at `f5f7894c6b30a86fd4699d721f966b6a01861443`. Its segmentation/geometry, mask-head training and old calibration remain archival regression code, not the active whole-mouth inference route or teacher.

This is a selective source integration, not a merge of the upstream research history. Training photographs, demo media, on-headset inference/calibration app and UDP19031 are not included, and the author's personal weights are not redistributed here. See [third-party notices](THIRD_PARTY_NOTICES.md).

The whole-mouth regression contract, Pillow preprocessing and source-paired label approach draw on UndefinedSignal's [issue #5](https://github.com/WolalaQAQ/PicoET-Enhance/issues/5) and MIT-licensed MouthLink, reviewed at `39e091f4191d2ea1ad8536120924cdf61949dae2`. The 50-field extension, label masks and teacher/student tools do not imply that upstream personal weights already support the additional controls.
