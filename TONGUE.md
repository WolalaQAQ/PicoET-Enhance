# PC End-to-End Whole-Mouth Tracking Guide

English | [简体中文](TONGUE_zh.md)

Use this branch (`feat/tongue-pc`) together with the matching [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule/tree/feat/tongue-pc) branch. It streams the PICO 4 Pro mouth camera over your LAN to a PC workbench, runs local 50-channel whole-mouth regression, blends selected channels with headset native output, and feeds VRCFaceTracking (VRCFT).

```text
PICO mouth camera (400×400 grayscale, 90 Hz)
  → standalone headset root daemon (JPEG 95, TCP 45681 / PTJ1)
  → PC workbench: decode, whole-mouth ONNX inference, per-channel blending
  → local UDP 45682 / MFR1 → UnifiedPicoModule → VRCFT
```

The headset module also exposes independent left-eye and right-eye streams (default ports: mouth `45681`, left eye `45682`, right eye `45683`, up to 8 concurrent consumers per stream). Other clients can subscribe over PTJ1 or standard HTTP MJPEG using the [LAN camera protocol](CAMERA-PROTOCOL.md). Recorded images, labels, and personal weights stay on your PC.

---

## 1. Prepare the PC app

Tested on Windows with Python 3.12 and .NET 10. From the repository root:

```powershell
python -m venv pc-studio/.venv
pc-studio/.venv/Scripts/python.exe -m pip install -r pc-studio/requirements.txt
pc-studio/start.cmd
```

Open `http://127.0.0.1:8787` in your browser. The default Windows setup uses ONNX Runtime DirectML, which works on DirectX 12 GPUs without requiring NVIDIA. The workbench starts idle with UDP output turned off.

### Workspace overview

| Page | Purpose |
|---|---|
| **Tracking** | Discover and pick a headset, choose a whole-mouth `.onnx` model, and start or stop live tracking and preview |
| **Capture** | Practice with 71 expression guides, record local sessions, and review visible frame labels |
| **Train** | Walk through data preparation and run local training and export tools |
| **Diagnostics** | Switch between **Expression monitor** (live 50-channel meters, pinned cards, side-by-side comparison) and **Connection diagnostics** (rates, latency, providers, queues, and native stream health) |
| **Settings** | Configure VRCFT UDP output, channel-routing presets, One-Euro filtering, inference backend, headset ports, and local replay |

The workbench automatically discovers matching headsets on your LAN over UDP `45681`: a single device is selected automatically, or you can pick one from the list when several are present. If your network isolates discovery broadcasts, enter the address and port manually under **Settings → Headset & ports**.

Click **Choose file** to select a whole-mouth `.onnx` model; place its same-name `.json` manifest in the same folder. This branch does not bundle general-purpose mouth or teacher weights. If you do not have a model yet, open **Capture** first to record your own data.

### Inference backends

Create a **separate venv** for each ONNX Runtime package—do not mix DirectML, CUDA, OpenVINO, and CPU ORT distributions in the same environment:

| Backend | Requirements file |
|---|---|
| Windows default (DirectML) | `pc-studio/requirements.txt` |
| CPU only | `pc-studio/requirements-cpu.txt` |
| NVIDIA CUDA 12 | `pc-studio/requirements-cuda.txt` |
| Intel OpenVINO GPU | `pc-studio/requirements-openvino.txt` |

For CUDA 12.6, install matching PyTorch wheels first in the new venv, install `requirements-cuda.txt`, and launch `pc-studio/backend.py` with that venv's Python:

```powershell
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r pc-studio/requirements-cuda.txt
```

When inference device is set to `auto`, the runtime probes **CUDA Graph → CUDA → OpenVINO GPU → DirectML → CPU** with a first-run check. Choosing a specific device disables automatic fallback. Check **Diagnostics → Connection diagnostics** to see the active provider or any initialization error.

---

## 2. Build and start the headset source

### Normal use (recommended)

After installing this branch's Magisk module and companion app:

1. Close other eye- and face-tracking clients on the headset once before first attachment.
2. Open **PicoET-Enhance → LAN broadcast** on the headset and turn on the master switch (defaults off and remembers your choice; the page displays live status, stream addresses, and client counts). The **Post-processing** tab keeps the original eye/mouth parameter panel.
3. If you want to blend native mouth channels on the PC or save native labels while recording, also turn on **Mouth** under **Result sharing**.

> The LAN TCP streams are unencrypted and unauthenticated. Use them only on a trusted private network and do not expose or forward these ports externally.

### Building from source and standalone diagnostics

The `device.py` workflow below is for developer testing; do not run it alongside the module-managed camera service. It targets a rooted PICO 4 Pro running PICO OS 5.13.7 and requires Android NDK `26.1.10909125`, CMake `3.22.1`, and Ninja (`libjpeg-turbo 3.1.2` is fetched and built statically; pass `--jpeg-source` for an offline checkout).

```powershell
python camera-source/build.py android --ndk <NDK-directory> --cmake <cmake-path> --ninja <ninja-path>
```

Build outputs appear in `camera-source/build-android/`: `picoet-inject`, `libpicoet_camera_jpeg.so`, `libptj_source.so`, `ptj_ipc_daemon`, and the module controller `picoet-camera`. When exact stock-label association is needed, build the `picoet_hook` target as well (`camera-source/build-android/hook/libpicoet_hook.so`) and deploy it together with the camera binaries. Eye-result subscriptions are documented in [PSE1 eye expression results](CAMERA-PROTOCOL.md#eye-expression-results-pse1).

Close all headset eye/face clients before the first injection, then run with your device's ADB serial (pass `--adb <path>` if ADB is not on PATH):

```powershell
python camera-source/device.py start  --serial <serial> --state pc-studio/user-data/source-session.json
python camera-source/device.py status --serial <serial> --state pc-studio/user-data/source-session.json
python camera-source/device.py stop   --serial <serial> --state pc-studio/user-data/source-session.json
```

Keep the `--state` file across `status`, `stop`, and subsequent `start` calls within the same service lifetime; use a new state file after a service restart or headset reboot. On Android, tracking leases are acquired on demand, and unsubscribed camera lanes are neither copied nor encoded.

---

## 3. Run end to end

1. Build and install the matching VRCFT receiver following the [UnifiedPicoModule receiver guide](https://github.com/WolalaQAQ/UnifiedPicoModule/blob/feat/tongue-pc/TONGUE.md).
2. Start your normal PICO face-tracking client (such as PicoFacialBridge) and let VRCFT assign face output to PICO.
3. In the PC workbench, choose your headset and whole-mouth `.onnx` model, then click **Start tracking**. Once video and meters look right, turn on **Send expressions to VRCFaceTracking** under **Settings** (local UDP `45682`).
4. Watch the live feed on **Tracking** and verify channel response and latency on **Diagnostics**.

While `UnifiedPicoModule` receives active `MFR1` frames on UDP `45682`, it writes the merged 50-channel output directly and lowers Bridge's face demand to eye-only (`PXR_SUB` mask `3 → 1`), keeping eye tracking uninterrupted; stopping or expiring seamlessly restores direct Bridge face tracking. When running a Cymple-mouth / PICO-eyes setup, simply leave this workbench stopped.

### Channel routing presets

Pick a preset under **Settings → Channel routing presets** (`mouth_mode`). Saving applies routing live without stopping camera tracking or restarting VRCFT:

| Preset | Native targets | Model targets | Behavior |
|---|---:|---:|---|
| **Curated hybrid** (`curated`, default) | 28 | 18 | Headset native `final33` drives 28 stable mouth shapes; the model drives missing, split, and severe-crosstalk targets |
| **Full native hybrid** (`hybrid`) | 37 | 9 | All 37 headset-supported targets prefer native output; the 9 missing targets use the model |
| **Model only** (`model_only`) | 0 | 46 | All 46 morph targets come from the PC model without opening the headset `LABELS` stream |

The 18 model-driven targets in **Curated hybrid** (`curated`) are:
- **9 missing in native**: `TongueLeft`, `TongueRight`, `TongueUp`, `TongueDown`, `TongueRoll`, `CheekSuckLeft`, `CheekSuckRight`, `MouthTightenerLeft`, `MouthTightenerRight`
- **3 coupled or split**: `TongueOut`, `CheekPuffLeft`, `CheekPuffRight`
- **6 prone to native crosstalk**: `MouthSmileL`, `MouthSmileR`, `MouthFrownL`, `MouthFrownR`, `MouthUpperUpL`, `MouthUpperUpR`

All three presets edit the same **Adjust source per morph target** table below (46 targets across 6 anatomical groups: 29 direct native controls, 8 subdivided controls split from aggregate headset slots, and 9 pure extensions switchable between `model` and `off`). Changing any individual target switches the badge to `custom`, and a single shared `PSL1` stream opens whenever at least one target uses native output.

### Filtering and settings

**Settings → Filtering & smoothing** exposes standard One-Euro filter controls that apply live on save:

| Control | Config key | Default | Effect |
|---|---|---:|---|
| Enable One-Euro filter | `filter_enabled` | On | Master toggle |
| Min cutoff frequency (Hz) | `filter_min_cutoff` | `8.0` | Lower steadies resting jitter; higher responds faster to subtle motion |
| Speed coefficient (beta) | `filter_beta` | `4.0` | Higher reduces lag during fast speech and rapid motion |
| Derivative cutoff (Hz) | `filter_d_cutoff` | `1.0` | Smooths velocity estimation to damp spikes |

Filtering runs per valid model channel using actual `mouth_sensor_ns` intervals **before** native channels are merged in—native `stock` channels pass headset `final33` through directly without double filtering. Filter state resets on new sessions, model changes, invalid outputs, mask changes, or gaps longer than `stale_ms`. Existing configs using `smoothing_ms` map cleanly (`min_cutoff = 1000 / (2π × smoothing_ms)` Hz; `0` disables filtering). Logs record pre-filter `raw_values`/`raw_channels`, filtered outputs, and publication timestamps.

Other settings are grouped into two sections:
- **Settings → Model & inference**: model path, inference device, result expiry (`stale_ms`), and preview rate. Changing the model path or device stops the current run when saved; preview rate updates live.
- **Settings → Headset & ports**: manual headset address, video port, and local UDP port. Saving changes stops the current run.

### Diagnostics and monitoring

- **Diagnostics → Expression monitor**: polls all 50 channels at 30 Hz with pinnable top cards, anatomical region/source/active filters, a **Freeze** toggle, and an optional **Compare native & model** view that marks merged headset slots and omits native columns where PICO stock has no channel.
- **Diagnostics → Connection diagnostics**: shows receive/inference rates, model and end-to-end latency (P50 / P95), active execution providers, queue/drop counters, `PSL1` native stream status, and error details.

---

## 4. Recording and training a personal model

### Guided recording and review

Open **Capture** to practice poses with the reference images and animations before connecting a headset (front views mirror your face so the left side is your own left; side views show lip and jaw depth). Pick a **Capture preset** and expand **Actions in order** to preview the sequence:

| Capture preset | Steps | Coverage |
|---|---:|---|
| Mouth basics | 12 | Relaxation, opening/closing, left/right smiles, pucker, funnel, cheeks, jaw shifts, tongue extension, relaxation |
| Tongue movements | 11 | Relaxation, short/medium/full extension, left/right/up/down, side roll, tip-curl contrast, relaxation |
| Independent lip movements | 13 | Relaxation, upper/lower horizontal shifts, inward rolls, upper/lower funnels, pucker, left/right presses, relaxation |
| Complete guide (default) | 71 | All 50 named controls, combinations, three tongue lengths, tongue-in-mouth poses, speech, and tip-curl contrast |

Select your headset, preset, and **Step advance** mode, tick the consent box, and click **Start recording**:
- **Automatic**: defaults to 7 seconds of preparation per step (adjustable from 3 to 30 seconds), times the hold, and advances automatically. Keep the demonstration visible during timed steps.
- **Manual**: click **Start holding** when ready, then **Finish holding** and **Confirm action done**.
- **Flexible navigation**: use **Previous step**, **Next step**, **Go to step**, **Skip**, or **Pause** anytime, or untick **Guide me step by step** for free recording. If a session stops early or disconnects, click **Continue last recording** after saving to capture the remaining steps into a new segment.
- **Saving native labels**: under **Capture → More recording options**, keep **Receive headset mouth results** ticked (with **Mouth** enabled under **Result sharing** on the headset), or untick it for image-only recording.

To capture variation across headset fits, take the headset off, refit it, and record another session—ideally on another day. Afterwards, open **Capture → Review recorded images** to inspect frames, enter visible controls as JSON (omitted controls stay unlabeled rather than zero), and mark unclear or blocked frames as *Uncertain*, *Occluded*, or *No mouth*.

The demonstration pack renders close-ups of the [Unity-Movement HighFidelity character](https://github.com/oculus-samples/Unity-Movement/tree/v71.0.0/Samples~/BodyTrackingSamples/HighFidelity) across all 71 stages (70 animations, 25 side views): 48 stages use the original morph targets and corrective rules, and 23 add guide controls on the original mesh for tongue and independent upper/lower lip movements.

### Teacher/student training

Local training runs through either the CLI or the **Train** page form (both share the same parameters). If you want an isolated training environment, create a separate venv with `pc-studio/requirements-training.txt` so its CPU ORT package does not collide with your DirectML/CUDA inference venv. Inspect all options with:

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/face_train.py --help
```

Typical pipeline order:

1. `split`: freeze a grouped split from reviewed recordings (`--group-by session` for one person or `person` across subjects; use at least three independent groups for train, validation, and sealed test).
2. `teacher-train` and `student-baseline`: fine-tune a local DINO teacher (`--tune frozen` first, then optionally continue via `--initial` with `--tune last-two`), alongside a directly supervised student baseline.
3. `validate-candidate`: compare the teacher against the baseline student and emit a validation approval report.
4. `predict-pseudo`: generate teacher soft labels for the training split only using the approved teacher report.
5. `student-distill` / `local-adapt`: train the personal student from reviewed labels and soft labels (reviewed labels override conflicting weak or teacher targets).
6. `evaluate` and `export`: evaluate the final student and export a new `.onnx` / `.json` pair (pass `--approval <report.json>` to enable validated output bits), then select the exported ONNX in the workbench.

Key training options:
- **Teacher backbone** (`--teacher-backbone`): defaults to DINOv2 ViT-B/14-reg (392×392). Also supports `dinov3` (ViT-B/16), `dinov3-large` (ViT-L/16), and `dinov3-huge` (ViT-H+/16) at full 400×400 resolution using local source and checkpoints (see the [DINOv3 License](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md)).
- **Student input & learning rates**: MobileNetV3-small defaults to 160×160 (`--student-input 224` enables higher resolution); default head learning rates are `1e-4` for teachers and `1e-3` for students.
- **Loss & augmentation**: defaults to masked MSE with no augmentation; `--loss huber` enables Smooth L1 (β=0.1) and `--augmentation mild` adds small shifts, rotations, exposure/gamma jitter, and noise without horizontal flipping.

### Full-resolution continuous training pipeline (`mouth50_trainer`)

For full 400×400 50-channel continuous regression—using the spatial-pyramid student (`VFTStudent50`), de-binarized continuous loss (`debinarized_continuous_loss`), 5-TTA confidence-weighted soft distillation, IR physical domain augmentation, and optional causal GRU-256 temporal fine-tuning—use the standalone offline package under `pc-studio/mouth50_trainer/`:

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/mouth50_trainer/pipeline.py --help
```

The pipeline exposes four stages (pass local backbone source and weights via `--dino-repo`, `--dino-weights`, and `--mnv3-weights`, or set `MOUTH50_DINO_REPO`, `MOUTH50_DINO_WEIGHTS`, and `MOUTH50_MNV3_WEIGHTS`):

| Stage | Purpose |
|---|---|
| `teacher` | Fine-tune the DINOv3-L teacher with a 12-query spatial attention head and `debinarized_continuous_loss` |
| `kd` | Generate 5-TTA soft targets and continuous confidence weights (supports `--resume`) |
| `student` | Distill a single-frame `VFTStudent50` (`small@384 pyramid roi=400` by default) with cosine restarts, EMA, and ONNX export |
| `temporal` | Optional: fine-tune a stateful `StatefulGRUStudent50` on contiguous clips and export ONNX with `h_in`/`h_out` |

Pass `--data-dir` (containing `vft50_train.jsonl` and `vft50_val71.jsonl`) and `--out-dir` to each stage. Exported `.onnx` / `.json` pairs match the workbench manifest contract and default to `valid_mask: 0` until validated.

### Local replay

Use **Settings → Replay a saved recording** to test offline with a local `.npz` file containing `images[N, 400, 400]` (uint8), `seq[N]`, and `times[N, 3]` (source nanoseconds), plus optional `vendor_ns` and `raw_identity`. Stop replay before starting a live recording.

---

## 5. Model contract and offline verification

### Whole-mouth model contract

- **Channel order (`pico-mouth50-v1`)**: defined in `pc-studio/face_schema.py`—MouthLink's original 44 controls followed by 4 independent upper/lower lip horizontal controls and 2 upper/lower Funnel controls.
- **Adjacent `.json` manifest**: declares `schema`, `channels`, input `width`/`height`, `trained_channels`, `validated_channels`, and `valid_mask`. The runtime enforces `valid_mask`; exports without `--approval` produce a zero-mask candidate.
- **Preprocessing & temporal models**: full 400×400 grayscale images are resized with Pillow `BILINEAR` and divided by `255` (no horizontal flip; normalization lives inside the model). Alongside standard single-frame ONNX (`frame → blendshapes`), the runtime supports stateful ONNX models exposing a `[1, H]` hidden-state pair (`h_in` / `h_out`), resetting state on new sessions, model changes, invalid outputs, or gaps exceeding `stale_ms`.
- **Wire output**: `MFR1` sends continuous `[0, 1]` values directly with a per-field validity mask.

### Offline verification

Run PC Python tests and bilingual UI tests (requires Node.js):

```powershell
pc-studio/.venv/Scripts/python.exe pc-studio/check.py
```

Run synthetic native host tests for `camera-source` from an x64 Visual Studio developer shell (or pass `--vcvars <vcvars64.bat>`; does not use ADB):

```powershell
python camera-source/build.py host --cmake <cmake-path> --ninja <ninja-path>
python camera-source/checks/test_ipc.py
python camera-source/checks/test_borrowed.py
python camera-source/checks/test_camera.py
python camera-source/checks/test_device.py
```

---

## Credits and notices

- The camera and PC pipeline started from Rulez777's [PR #4](https://github.com/WolalaQAQ/PicoET-Enhance/pull/4) (reviewed at `f5f7894c6b30a86fd4699d721f966b6a01861443`).
- The whole-mouth regression contract, Pillow preprocessing, and source-paired label design draw on UndefinedSignal's [issue #5](https://github.com/WolalaQAQ/PicoET-Enhance/issues/5) and MIT-licensed MouthLink (reviewed at `39e091f4191d2ea1ad8536120924cdf61949dae2`).
- Asset and third-party license notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
