<div align="center">

# PicoET-Enhance

**Unlock real pupil diameter, independent per-eye expressions, 90/90 Hz tracking, on-headset tuning, and LAN camera streaming on the PICO 4 Pro.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-v2.7.0-green.svg)](https://github.com/WolalaQAQ/PicoET-Enhance/releases)
[![Platform](https://img.shields.io/badge/PICO%204%20Pro-PICO%20OS%205.13.7-orange.svg)](#requirements)
[![Releases](https://img.shields.io/github/v/release/WolalaQAQ/PicoET-Enhance)](https://github.com/WolalaQAQ/PicoET-Enhance/releases)

**English** | [简体中文](README_zh.md)

[Features](#what-it-improves) · [Quick Install](#quick-install) · [Companion App](#using-the-companion-app) · [PC Studio](#pc-studio) · [FAQ](#troubleshooting--faq) · [Developer Guide](#cli--advanced-controls) · [Community](#feedback--community)

</div>

---

**PicoET-Enhance** is a runtime Magisk module for the **PICO 4 Pro** (`PICO OS 5.13.7`, Magisk, SELinux Enforcing). It hooks `pxreyetrackingservice` in memory without modifying system files or shipping any PICO binaries, and ships a zero-permission companion app for post-processing tuning and LAN camera streaming.

## What It Improves

PICO 4 Pro tracks both eyes at 90 Hz, yet stock firmware hides real pupil diameter, mixes both eyes together before outputting facial expressions, and caps expression updates at about 24 Hz. PicoET-Enhance fixes all three at runtime:

- **Real pupil diameter**: Opens the per-eye business gate inside `pxreyetrackingservice` so real pupil diameter is written to shared memory (device-wide `ro.pxr.externalfunc` stays untouched).
- **Independent left & right eye expressions**: Removes cross-eye coupling in the expression model. `EyeLook`, `Blink`, `Wide`, and brow channels run and filter independently per eye, each with its own quality window, reference image, and history. The right inner brow and per-eye quality states are exported via the `EPI1` v2 extension.
- **90/90 Hz eye & mouth tracking**: Runs eye and mouth expression inference in parallel on every eligible fresh camera frame instead of ~24 Hz. Smoothing, mouth rest-baseline learning, and quality windows adapt automatically to each source's actual timestamps (`Δt`).
- **On-headset tuning & LAN camera broadcast**: Includes the rootless **PicoET-Enhance** companion app to tune or bypass PICO's built-in expression post-processing (44 parameters) and stream the mouth, left-eye, and right-eye cameras over your local network to **PC Studio**, Cymple, or any MJPEG client.

Stock neural models, combined gaze, dark-image admission, reference protection, and model quantization stay intact. Everything lives in service memory and unloads cleanly when `pxreyetrackingservice` restarts.

## How It Fits Into the Tracking Pipeline

```mermaid
flowchart LR
    A["PicoET-Enhance<br/>(Headset Magisk Module)"] -->|"EPI1 v2 + mode props"| B["PicoFacialBridge 0.5.0+<br/>(Headset App)"]
    B -->|"UDP 9030 (X v2 packets)"| C["UnifiedPicoModule 0.4.0+<br/>(PC VRCFT Module)"]
    C --> D["VRCFaceTracking<br/>(or VRCFT-Enhance)"]
    D -->|"OSC"| E["VRChat"]
```

[PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) detects PicoET-Enhance's runtime mode automatically and switches the PC receiver ([UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule)) into enhanced mode. On the PC, eye direction and eyelid openness come from `EyeLook` and `1 − Blink`; native eye data supplies measured pupil diameter.

> [!TIP]
> **Want near-90 Hz OSC inside VRChat?** Official VRCFaceTracking polls modules and writes OSC on fixed 10 ms waits, which drops output to roughly 61–78 Hz on Windows 11. Use [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance) (4 ms default waits + timer-resolution and concurrency fixes) to carry ~87–88 Hz eye and mouth updates all the way to VRChat OSC.

## Requirements

| Component | Requirement |
| --- | --- |
| **Headset** | PICO 4 Pro |
| **Firmware** | PICO OS 5.13.7 (Android 10 / API 29, `arm64-v8a`) |
| **Root** | Magisk installed (works under SELinux Enforcing) |
| **Bridge & PC Receiver** | [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) `0.5.0+` and [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule) `0.4.0+` for 90 Hz `EPI1`/`X` v2 |

The installer verifies the SHA-256 hashes of the headset's stock eye-tracking libraries before installing and aborts safely if your firmware build does not match.

## Quick Install

1. Download `magisk-picoet-enhance-v2.7.0.zip` from [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases).
2. Flash it in the **Magisk** app (**Modules → Install from storage**), or install over ADB:

   ```sh
   adb push magisk-picoet-enhance-v2.7.0.zip /data/local/tmp/
   adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.7.0.zip'
   ```

3. **Reboot the headset.**

Fresh installs boot directly into enhanced mode (`dual/on`) at **90/90 Hz** and add the **PicoET-Enhance** companion app to your app library. If you had switched to normal mode before upgrading, your off state is preserved.

To verify after reboot (optional, root shell):

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

Look for `phase=ready applied_mode=dual applied_gate=on effective_eye_hz=90 effective_mouth_hz=90`.

## Using the Companion App

Open **PicoET-Enhance** from the headset's app library. The title bar gives you two peer tabs — **LAN broadcast** and **Post-processing** — plus a one-tap English / Chinese switch. Neither the companion app nor PicoFacialBridge requests root or network permissions; the app sends fixed requests from its Android sandbox to the module's background worker.

### Post-processing Tab

PICO's stock firmware runs several post-processing stages after the expression neural network (brow curves and linkages, blink endpoint snapping, adaptive/composite/Kalman filters, mouth rest-baseline removal, weak-shape log compression, and tongue gating/smoothing). **PicoET-Enhance does not add an extra processing layer on top** — it exposes **44 parameters of PICO's own built-in post-processing** so you can tune or bypass each stage. Defaults match PICO's original values.

- **Four presets on the Start page**:
  - **Balanced (recommended)**: Middle ground between Stock and Responsive. Speeds up eyes, brows, mouth, and tongue response while keeping comfortable steadiness. Weakens brow linkages and weak-shape compression, disables the second mouth smoothing pass, speeds up rest learning, and lowers the tongue trigger threshold to `0.35`, while keeping blink snapping, rest removal, and lip-closure boost.
  - **Responsive**: Keeps about half the smoothing and turns off brow linkages so movements catch up within ~0.1 s (fine jitter is visible while holding still).
  - **No processing**: Skips PICO's numerical post-processing and emits bounded `[0, 1]` model outputs directly (quality checks, reference images, and validity stay active; this is not raw camera video or a model bypass).
  - **Stock**: PICO's original values for side-by-side comparison or per-parameter tuning.
- **15 mirror-check movements**: The left sidebar lists 15 everyday expressions (blinking, looking sideways, raising brows, talking, sticking out your tongue, and so on). Each page draws a synthetic 90 Hz comparison curve showing **Model output** (grey) vs. **Avatar receives** (green) that recalculates live as you adjust sliders.
- **Simple vs. Technical wording**: Flip the wording switch at the top between plain-language tips and full variables/formulas without changing any settings.
- **Fine control & All settings**: Drag sliders for coarse adjustments, tap `◀` / `▶` for single-step nudges, or tap a numeric value for exact input or single-field reset. Open **All settings** to view all 44 parameters in execution order.
- **Save vs. Apply workflow**:
  1. Edits stay in a draft first; tap **Save** to write `/data/local/tmp/picoet-postprocessing.conf` (saving never restarts tracking).
  2. Stop **PicoFacialBridge** and any other eye/face tracking client.
  3. Tap **Apply** to restart `pxreyetrackingservice` with the saved settings (restarts only when no clients are connected and enhancement is active).

Saved settings survive module upgrades. To pick up updated values from a preset after an upgrade, select that preset again, then **Save** and **Apply**.

### LAN Broadcast Tab

Turn on **Enable broadcast** to expose the headset's three IR cameras (`400×400` grayscale JPEG, quality 95) over your local network:

| Feed | Default Port | MJPEG URL |
| --- | ---: | --- |
| **Mouth** | `45681` | `http://<headset-ip>:45681/stream.mjpg` |
| **Left eye** | `45682` | `http://<headset-ip>:45682/stream.mjpg` |
| **Right eye** | `45683` | `http://<headset-ip>:45683/stream.mjpg` |

- **On-demand & persistent**: Broadcast defaults to off and remembers your choice. Closing the companion app leaves the background service running. Cameras activate only while at least one client is subscribed; switching **Enable broadcast** off drains and closes every active stream.
- **First attachment**: Close other eye/face tracking clients once when enabling broadcast for the first time so the camera adapter can attach while tracking is idle.
- **Result sharing** (optional): Independent switches for **Mouth** (`PSL1`) and **Both eyes** (`PSE1`) let developer tools subscribe to numerical tracking outputs while broadcasting. Both default off and never affect camera video or PicoFacialBridge.

> [!WARNING]
> LAN discovery, camera video, and result streams are unencrypted and unauthenticated. Enable **LAN broadcast** only on a trusted private network.

## PC Studio

[`pc-studio`](pc-studio) is a native Windows desktop app (Rust + Tauri v2, single executable) for previewing the headset's LAN camera feeds and forwarding both eye feeds into **Cymple**. Requires **Windows 10/11 (x64)** and the **Microsoft Edge WebView2 Runtime** (preinstalled on Windows 11 and most updated Windows 10 systems; available from Microsoft if missing on LTSC or minimal installs).

- **Installer (recommended)**: Download `PicoET-PC-Studio-Setup-v2.7.0.exe` from [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases). Installs per-user to `%LOCALAPPDATA%\Programs\PicoET PC Studio` (no admin rights needed) and creates a Start Menu shortcut.
- **Portable**: Unpack `picoet-pc-studio-v2.7.0.zip` and run `PicoET-PC-Studio.exe`. Settings are stored in `user-data/` next to the executable (installed copies store settings under `%LOCALAPPDATA%\PicoET-PC-Studio\user-data\`).
- **From source** (Rust `>= 1.90`, MSVC Build Tools, Edge WebView2 Runtime):

  ```powershell
  cargo run --manifest-path pc-studio/src-tauri/Cargo.toml
  ```

Key features:
- Automatic LAN headset discovery (works even when your default PC route points at a VPN) or manual IP entry.
- Live camera preview switching between Mouth, Left eye, and Right eye, with copyable MJPEG URLs for VLC, FFmpeg, OBS, or custom scripts.
- **Cymple eye relay**: Forwards both eye streams (`PTJ1`) to Cymple's native UDP input (`127.0.0.1:23303`) with configurable ports and per-eye rotation/flip.
- **System tray & clean exit**: Closing the window hides PC Studio to the Windows system tray so an active Cymple eye relay keeps running; right-click the tray icon or click the in-app **Quit** button to stop the relay and exit cleanly.
- Built-in GitHub Releases update checker (links directly to new PC Studio and headset module packages).

## Disabling & Uninstalling

- **Temporarily disable enhancement**: Tap the **Action** button in Magisk, or run `sh /data/adb/modules/picoet-enhance/picoet.sh off` as root. This restarts `pxreyetrackingservice` without injection (100% stock behavior).
- **Uninstall completely**: Remove **PicoET-Enhance** in the Magisk app and reboot. The companion app, runtime state files, and `/data/local/tmp/picoet-postprocessing.conf` are removed on the next boot.

## Troubleshooting & FAQ

<details>
<summary><strong>Apply fails or says tracking clients are connected</strong></summary>

Applying post-processing or switching modes restarts `pxreyetrackingservice`. Stop **PicoFacialBridge** and any other eye/face tracking client first, then tap **Apply** again. From a root shell, mode switching (`dual` / `off` / `toggle`) accepts `--force`, whereas `reload-processing` intentionally refuses while clients are connected.

</details>

<details>
<summary><strong>LAN broadcast says "Close other eye and face tracking clients once"</strong></summary>

The camera adapter only attaches while `pxreyetrackingservice` is idle. Stop Bridge or other tracking apps briefly, wait for **LAN broadcast** to report **On · waiting for a client**, then you can resume normal tracking alongside streaming.

</details>

<details>
<summary><strong>Enhanced mode does not activate after reboot</strong></summary>

Check `sh /data/adb/modules/picoet-enhance/picoet.sh status` and inspect `/data/local/tmp/picoet-hook.log` and `/data/adb/picoet-enhance/*.log`. If automatic injection stopped after exhausting its recovery budget, fix the reported cause and run `picoet.sh dual` (or tap the Magisk **Action** button) to reset the budget and retry.

</details>

<details>
<summary><strong>Why is VRChat eye tracking still ~60–75 Hz even though PicoET runs at 90 Hz?</strong></summary>

Official VRCFaceTracking uses fixed 10 ms sleeps, which Windows 11 often stretches to ~15 ms. Installing [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance) lifts OSC throughput to ~87–88 Hz.

</details>

---

## CLI & Advanced Controls

Run these commands as root on the headset (`adb shell su -c ...`):

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual      # alias: on
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh toggle
sh /data/adb/modules/picoet-enhance/picoet.sh reload-processing
```

| Command | Effect |
| --- | --- |
| `status` | Prints saved/applied mode & gate, injection phase, service PID, `effective_eye_hz` / `effective_mouth_hz`, `ro.pxr.externalfunc`, recent hook log lines, and active client count |
| `dual` (`on`) | Enables enhanced mode (`dual/on`: pupil gate + independent per-eye expressions) and restarts `pxreyetrackingservice` |
| `off` | Restores stock service (`off/off`) without injection |
| `toggle` | Switches to `off` when enhancement is `ready`; otherwise attempts to enable `dual` |
| `reload-processing` | Validates `/data/local/tmp/picoet-postprocessing.conf` and restarts the service without `--force` (only when enhanced mode is active and no clients are connected) |

Switching (`dual` / `off` / `toggle`) refuses while clients are connected unless `--force` is passed. The Magisk **Action** button runs `picoet.sh toggle --force`.

<details>
<summary><strong>Custom Eye & Mouth Rates (optional)</strong></summary>

Both eyes and mouth default to **90 Hz**. To cap either target rate, write a positive integer (1–90) to its file and restart the service with `picoet.sh dual` after stopping clients:

| File | Default | Behavior |
| --- | ---: | --- |
| `/data/local/tmp/picoet-eye-hz` | `90` | Target eye expression rate; `90` processes every eligible fresh frame |
| `/data/local/tmp/picoet-mouth-hz` | `90` | Target mouth expression rate; clamped to the eye rate (every mouth frame also computes eyes) |

Example — keep eyes at 90 Hz and lower mouth to stock 24 Hz:

```sh
echo 90 > /data/local/tmp/picoet-eye-hz
echo 24 > /data/local/tmp/picoet-mouth-hz
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

Delete both files and run `picoet.sh dual` to return to 90/90 Hz. Rate files survive upgrades and do not affect normal mode (`off`).

</details>

<details>
<summary><strong>Applying Post-processing Presets via CLI</strong></summary>

The configuration file is `/data/local/tmp/picoet-postprocessing.conf` (`key=value`, `#` comments, `version=1` required, max 16 KiB). Every `.strength` accepts `0`–`1` (`0` bypasses the stage, `1` keeps full strength, intermediate values blend); `eye.enabled` and `mouth.enabled` accept `0`/`1`.

Bundled presets live in [`magisk-picoet-enhance/`](magisk-picoet-enhance/):
- [`postprocessing-balanced.conf`](magisk-picoet-enhance/postprocessing-balanced.conf)
- [`postprocessing-responsive.conf`](magisk-picoet-enhance/postprocessing-responsive.conf)
- [`postprocessing-raw.conf`](magisk-picoet-enhance/postprocessing-raw.conf)
- [`postprocessing-default.conf`](magisk-picoet-enhance/postprocessing-default.conf)

Apply one from a root shell (after stopping tracking clients):

```sh
MOD=/data/adb/modules/picoet-enhance
"$MOD/bin/picoet-config" write < "$MOD/postprocessing-balanced.conf"
sh "$MOD/picoet.sh" reload-processing
```

`picoet-config` supports `read`, `write`, `check`, `defaults`, `balanced`, `responsive`, and `raw`. Invalid, duplicate, or unknown keys are rejected; malformed or improperly owned files fail explicitly at startup instead of silently falling back.

</details>

<details>
<summary><strong>Automatic Injection & Recovery Behavior</strong></summary>

At boot, `service.sh` waits for `sys.boot_completed=1` and a Binder `dumpsys` response from `pxreyetrackingservice` confirming all three stock algorithms (`ET`, `FT`, `IPD`) are initialized before injecting. Wait until `picoet.sh status` shows `phase=ready applied_mode=dual applied_gate=on` before launching tracking clients.

- **Transient retry (exit `3`)**: If no safe executor thread is available or a temporary signal interrupts attachment, the watchdog releases the lock and waits 30 s before retrying without consuming any budget.
- **Recovery budget**: Unsafe failures (exits `4`/`5` or async rollback failure) share **1** automatic restart-and-retry per boot or explicit mode command.
- **Stability budget**: Automatic injection runs against at most **3** consecutive unstable service instances; the counter resets only after one instance stays continuously `ready` for 60 s.

For complete state files, script internals, and module layout, see [`magisk-picoet-enhance/README.md`](magisk-picoet-enhance/README.md).

</details>

## Technical Details

- **Business gate**: Firmware gates per-eye data (including real pupil diameter) behind `PXR::EyeUtil::isBusinessDev()`. PicoET-Enhance hooks this function to return `true` inside `pxreyetrackingservice` without touching `ro.pxr.externalfunc`.
- **LAN camera & result wire protocol**: Discovery (`PICO_CAMERA_DISCOVER_V1`), HTTP MJPEG (`/stream.mjpg`), binary `PTJ1`, mouth labels (`PSL1`), eye results (`PSE1`), and `PTJIPC5` shared-memory lifecycle are documented in [CAMERA-PROTOCOL.md](CAMERA-PROTOCOL.md).
- **Magisk module internals**: Module layout, state/log paths, and recovery rules are documented in [magisk-picoet-enhance/README.md](magisk-picoet-enhance/README.md).

### Measured 90 Hz Throughput

Replaying 2,700 recorded 90 Hz camera frames (~30 s) on an awake PICO 4 Pro (`PICO OS 5.13.7`) through PicoET-Enhance's expression pipeline, PicoFacialBridge `0.5.0`, UnifiedPicoModule `0.4.0`, VRCFT-Enhance (4 ms wait), and desktop VRChat:

| Pipeline Stage | Measured Rate |
| --- | ---: |
| Headset eye & mouth computation | **89.80 Hz** (2,694 / 2,700 frames; 6 hit stock full-queue drop, 0 failures) |
| Bridge → PC (`X` v2 packets) | **89.84 Hz** |
| VRCFT-Enhance → VRChat OSC (matched eye + mouth) | **87.80 Hz** (2,633 sources, 97.5% of inputs) |

Median `X`-arrival-to-OSC latency was **9.1 ms** (P95 **12.5 ms**). Combined gaze stays stock, and current VRCFT avatar brow parameters still average left and right brows on the host even though `EPI1` carries both sides.

## Building from Source

Prerequisites:
- **Headset module**: **Python 3**, **JDK 17+** (`JAVA_HOME`), **Android SDK Platform 35** & **Build-Tools 35.0.0** (`ANDROID_HOME`), **NDK 26.1.10909125**, and **CMake 3.22.1 / Ninja**.
- **PC Studio**: **Rust / Cargo `>= 1.90`**, **Visual Studio / MSVC Build Tools** (Windows x64), **Microsoft Edge WebView2 Runtime**, **Node.js** (for frontend JS checks), and **Inno Setup 6** (for the Windows installer).

```powershell
# 1. Build injector, hook payload, config helper, and panel worker
pwsh -File hook\build-hook.ps1

# 2. Build Android LAN camera adapter, shim, controller, and IPC daemon
python camera-source/build.py android `
  --ndk "$env:ANDROID_HOME/ndk/26.1.10909125" `
  --cmake "$env:ANDROID_HOME/cmake/3.22.1/bin/cmake.exe" `
  --ninja "$env:ANDROID_HOME/cmake/3.22.1/bin/ninja.exe"

# 3. Build the companion APK and package artifacts/magisk-picoet-enhance-v<version>.zip
pwsh -File build-picoet-enhance.ps1 -Python python
```

The companion APK builds directly with the Android SDK (no Gradle or external Android libraries). Packaging enforces an ELF allowlist and verifies zero PICO binaries are bundled.

- **Signing**: Local builds create and reuse a dev keystore by default. For distributable builds, pass `-PanelKeystore` (or `PICOET_PANEL_KEYSTORE`), optional `-PanelKeyAlias` (default `panel`), and set `PICOET_PANEL_PASSWORD`. Upgrades must preserve the same signing key. GitHub Actions tag builds (`v<version>`) require `PICOET_PANEL_KEYSTORE_BASE64` and `PICOET_PANEL_PASSWORD` repository secrets.
- **PC Studio tests & packaging**: Run `python pc-studio/check.py` to execute locked Cargo unit tests and Node.js frontend checks, and `python pc-studio/package_release.py` to build the standalone Rust/Tauri executable, portable ZIP, and Inno Setup 6 installer (pass `--skip-installer` for the portable ZIP only).

## Roadmap

- [ ] **Tongue direction tracking** — PICO firmware only exposes scalar `TongueOut`, while VRCFaceTracking supports directional channels (`TongueLeft/Right/Up/Down`). Planned pipeline: capture raw lower-face frames via the LAN camera service, auto-label with segmentation foundation models, train a lightweight student model, and support local user fine-tuning.

## Pico-Enhance Ecosystem

| Project | Purpose |
| --- | --- |
| [**PicoET-Enhance**](https://github.com/WolalaQAQ/PicoET-Enhance) | This repository — real pupil, independent eyes, 90/90 Hz, post-processing tuning, LAN camera streaming |
| [**PicoFacialBridge**](https://github.com/WolalaQAQ/PicoFacialBridge) | Headset app forwarding PICO eye & face tracking over LAN UDP |
| [**UnifiedPicoModule**](https://github.com/WolalaQAQ/UnifiedPicoModule) | PC VRCFaceTracking receiver module for PicoFacialBridge (`X` v2 support) |
| [**VRCFT-Enhance**](https://github.com/WolalaQAQ/VRCFT-Enhance) | Patches VRCFaceTracking host timing for ~88 Hz OSC output on Windows |
| [**VD-PicoEnhance**](https://github.com/WolalaQAQ/VD-PicoEnhance) | Virtual Desktop enhancements for PICO (hand tracking, bitrate/resolution unlock, QoL, Index Knuckles auto-switch) |
| [**PicoRegionSwitch**](https://github.com/WolalaQAQ/PicoRegionSwitch) | Hot-switch between CN and Global firmware app sets on PICO 4 Pro |
| [**PicoUSB-Quiet**](https://github.com/WolalaQAQ/PicoUSB-Quiet) | Suppresses repetitive USB authorization prompts on PICO |

## Feedback & Community

Bug reports and questions are welcome in [Issues](https://github.com/WolalaQAQ/PicoET-Enhance/issues) — include your headset model, PICO OS version, module version, output of `picoet.sh status`, and relevant log excerpts.

Join the Pico-Enhance community for help, discussion and release announcements.

<a href="https://discord.gg/ueSEydeVD8"><img src="https://img.shields.io/badge/Discord-Join%20Pico--Enhance-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join Pico-Enhance on Discord"></a>

**QQ group:** 1129699813. Scan the code below with QQ to join.

<img src="assets/community/qq-group-qr.png" alt="Pico-Enhance QQ group QR code" width="240">

## License

Original code and scripts are licensed under the [MIT License](LICENSE). Vendored **ShadowHook 2.0.1** sources include MIT- and BSD-licensed components, the camera transport bundles **libjpeg-turbo**, and PC Studio statically links Rust/Tauri v2 dependencies (including MIT, Apache-2.0, BSD, Zlib, Unicode-3.0, and unmodified MPL-2.0 crates); all notices are shipped with both the Magisk ZIP and PC Studio packages and listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This project contains and distributes no PICO binaries.
