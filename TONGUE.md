# PC tongue tracking — development branch

English | [简体中文](TONGUE_zh.md)

Use `feat/tongue-pc` in this repository and [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc). This is a source-based developer test: build the camera transport, run the PC app, and build the matching receiver. The regular Magisk ZIP/build workflow remains unchanged and does not install these additional components.

```text
PICO camera5 mouth lane (400×400 grayscale, fresh-source target 90 Hz)
  → synchronous protection copy into a bounded shared slot
  → separate root daemon, JPEG quality 95, TCP45681 / PTJ1
  → PC: decode → FP32 ONNX segmentation → calibration / hysteresis
  → local UDP45682 / TJR1 → UnifiedPicoModule → VRCFT
```

The camera service does not listen on TCP or run the tongue model. The encoder borrows the protected shared slot until encoding/sending finishes, avoiding a second full-image staging copy. The first camera protection copy is retained. Real frame identity and sensor timestamps are not replaced by callback counters. Camera5 supplies one composite parent sensor time, not three independently measured exposure times.

## 1. Prepare the PC app

Tested with Windows, Python 3.12 and .NET 10. From this repository's root:

```powershell
python -m venv pc-tongue/.venv
pc-tongue/.venv/Scripts/python.exe -m pip install -r pc-tongue/requirements.txt
pc-tongue/.venv/Scripts/python.exe pc-tongue/prepare_model.py
pc-tongue/start.cmd
```

Open `http://127.0.0.1:8787`. The default Windows environment uses ONNX Runtime DirectML; it works with DirectX 12 GPUs without requiring NVIDIA. The page starts idle and UDP output is off. Enter the headset address yourself; there is no fixed local IP.

`prepare_model.py` downloads the author's fixed `v2.4.2-tongue.1` archive, extracts only the pinned `shadow-final-v2.pt`, and exports a checked FP32 ONNX model. It does not extract training photos or run upstream scripts. If you already have the archive, pass `--archive <path>` to avoid downloading it. Assets remain under ignored `pc-tongue/assets/`. Existing different checkpoints or ONNX exports are not overwritten; use another `--output` directory and select its `.pt` in the app.

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

Use rooted PICO 4 Pro / PICO OS 5.13.7 with normal PicoET-Enhance 2.4.2 installed. The adapter checks its supported system-library file and instruction window before hooking. Build inputs are Android NDK 26.1.10909125, CMake 3.22.1 and Ninja. libjpeg-turbo 3.1.2 is fetched and built as a separate static-library project; ShadowHook is compiled from this repository's existing vendored source.

```powershell
python tongue-source/build.py android --ndk <NDK-directory> --cmake <cmake-executable> --ninja <ninja-executable>
```

The four outputs are in `tongue-source/build-android/`: `picoet-inject`, `libpicoet_camera_jpeg.so`, `libptj_source.so`, `ptj_ipc_daemon`. No device libraries or old compiled objects are build inputs. An existing libjpeg-turbo 3.1.2 source checkout can be selected with `--jpeg-source` for an offline build.

Stop all headset eye/face clients before the first injection. Use your own ADB serial:

```powershell
python tongue-source/device.py start --serial <serial> --state pc-tongue/user-data/source-session.json
```

`--adb <executable>` selects an ADB outside PATH. The command uploads into a unique root-private session directory, labels only its two DSOs `system_file`, serializes injection with the existing PicoET control lock, starts the daemon, sends an owner-bound start command and verifies IPC identity/nonce ACKs. It does not install a module, change SELinux policy, restart the service or start a tracking client. The source remains inactive until a JPEG subscriber connects. The LAN TCP stream is unencrypted and unauthenticated: use a trusted private network, not an exposed interface/port-forward.

Keep the state file. It identifies the device, boot, service instance and remote directory; subsequent commands use it rather than a remembered PID. For status and stop:

```powershell
python tongue-source/device.py status --serial <serial> --state pc-tongue/user-data/source-session.json
python tongue-source/device.py stop --serial <serial> --state pc-tongue/user-data/source-session.json
```

Stop disarms and drains callbacks, then checks the shim's join ACK before marking the session stopped. The disabled adapter/shim remain resident; do not `dlclose` them. Start with the **same state file** to reuse a successfully stopped adapter with a new daemon/session. Missing ACKs preserve the mapping and block reattach. A hook-install failure is terminal in that service instance: stop the owned unattached daemon and retain diagnostics rather than injecting repeatedly. After a normal service restart or reboot, use a new state file. Remote session files are retained for debugging; there is no broad cleanup command.

The native lifecycle was tested in the service domain before this source integration. This new portable controller is covered by fake-device tests, not a new device run.

## 3. Run end to end

1. Build/install the matching UnifiedPicoModule branch following its [receiver guide](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE.md).
2. Start your normal PICO face-tracking client, such as PicoFacialBridge. Let VRCFT assign face output to PICO when testing this tongue source.
3. In the PC app, set the headset address and `.pt` model path, then Start. The adjacent `.onnx` is used for ORT inference. Enable UDP output to local port45682 when ready to feed the receiver.
4. Check fresh input and the Out / Left / Right bars. Advanced settings retain backend, timing, UDP and replay controls; source/latency/queue details remain in folded diagnostics. English/Chinese switching preserves unsaved input and expanded sections.

The app never selects PICO versus Cymple mouth ownership. In Cymple-mouth / PICO-eyes use, leave this PICO video app stopped; the receiver's host ownership check also prevents foreign mouth writes. Fresh absent zeros all three tongue values; invalid/stop/expiry restore the current native Out and clear Left/Right. Up/Down are not supplied. See the receiver guide for the 64-byte TJR1 contract.

## Personal calibration and training

Use fresh **live** images, not replay. Expand Personalization, choose Calibrate, and follow fit → closed mouth → open mouth → center tongue → left → right. Optional browser speech announces the stages. Calibration stores angles and area threshold; it does not save a recording.

Retrain requires a separate explicit consent checkbox for local sample recording/training. The encoder is frozen; only the 3,777-parameter mask head is trained on CPU for 900 Adam steps, with positive-weighted BCE + Dice. Every fourth sample of each pose is held out. The candidate must separate directions, improve `TP − 2×FP` by at least .05 and increase FP by no more than .02. Export/numeric checks and live inference-worker warmup precede committing the profile. Rejected candidates leave the active model unchanged. These short held-out samples are not independent-session generalization evidence.

Personal profiles/jobs live beside the config in `pc-tongue/user-data/personalization/`, separate from standard weights. Restore standard resets the active personal head/calibration but keeps job files. Cancel/restore invalidate old training completions, predictions and previews. Area hysteresis uses the personal start threshold and 60% to remain visible. Config, numeric run logs and personal recordings stay on the PC; no upload endpoint is provided. Complete live JPEG recording occurs only with the explicit backend `--record-input` flag.

Replay accepts your own NPZ with `images[N,400,400]` uint8, `seq[N]`, and `times[N,3]` source nanoseconds; optional `vendor_ns` and `raw_identity` preserve those identities. Select it in advanced settings. No personal photos or replay dataset is included in the branch.

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

Based on Rulez777's [PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4), reviewed at `f5f7894c6b30a86fd4699d721f966b6a01861443`. PC model architecture/preprocessing/geometry follow `research/tongue-v2/training/shadow_model.py` at `04fe68ac88e8c1fcaf504eba8223ebf33c629d93`. Calibration, mask-head training/acceptance and size hysteresis adapt tongue.2's behavior to PC, with transactional activation and source freshness.

This is a selective source integration, not a merge of the upstream research history. Training photographs, demo media, on-headset inference/calibration app and UDP19031 are not included. Standard weights are acquired from the author's release, not redistributed here. See [third-party notices](THIRD_PARTY_NOTICES.md).
