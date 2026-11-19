# PicoET-Enhance

English | [简体中文](README_zh.md)

An eye-tracking enhancement Magisk module for the PICO 4 Pro: it opens the eye-tracking service's per-eye gate for real pupil data, decouples the facial expression model so each eye is processed on its own, and targets 90 Hz for both eye and mouth expressions by default instead of the stock rate of about 24 Hz. Use it with [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) and [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule).

> v2.6.0 · PICO 4 Pro / PICO OS 5.13.7 · [Changelog](CHANGELOG.md)

## What it does

The PICO 4 Pro firmware tracks each eye, but its facial expression model mixes the two eyes before output, so blink, look direction, wideness and brows for one eye depend on the other eye's inputs. It also updates these expressions only about 24 times a second, although the eye cameras run at 90 Hz. This module changes three things at runtime:

- **Real pupil data**: it opens the per-eye gate inside the eye-tracking service, so real pupil diameter data is written to shared memory.
- **Independent eyes**: it removes the expression model's cross-eye coupling. EyeLook, Blink, Wide and the brow channels are computed and filtered per eye, each with its own quality window, reference image and history. The filtered right inner brow and the per-eye quality states are exported as an `EPI1` extension, which PicoFacialBridge forwards as `X` packets.
- **90/90 Hz eye and mouth by default**: the eye and mouth expression paths each run on every eligible fresh camera input, and the two inferences for a frame run in parallel. Lower rates are optional ([Eye and mouth rates](#eye-and-mouth-rates)). To carry the high rate all the way to VRChat, see [About 90 Hz OSC](#about-90-hz-osc).

Smoothing, mouth baseline adaptation and quality windows follow each source's own timestamps: a long gap pauses the quality window, and a repeated or backward source does not advance anything.

The inference models and combined gaze stay stock. Blink endpoints, within-eye brow linkage and eye/mouth postprocessing remain enabled by default and can be individually adjusted or bypassed in [headset postprocessing](#headset-postprocessing). Dark-image admission, reference protection and model quantization remain enabled. On the PC, eye direction and eyelid openness use EyeLook and `1−Blink`; native eye data supplies measured pupil diameter only.

The enhancement runs as hooks in the eye-tracking service and is unloaded when that service restarts.

## How it fits together

```text
PicoET-Enhance (headset, this module)
        ↓ active at runtime, advertises the mode
PicoFacialBridge (headset)
        ↓ BridgeSplit, LAN UDP 9030
UnifiedPicoModule (PC)
        ↓
VRCFaceTracking → VRChat OSC
```

PicoFacialBridge reads the module's transient mode properties and automatically tells the PC whether normal or enhanced mode is active.

The high-rate path uses `EPI1` / `X` v2 and needs PicoFacialBridge 0.5.0 or later and UnifiedPicoModule 0.4.0 or later. The earlier PicoET-Enhance 2.2.0 with PicoFacialBridge 0.4.0 and UnifiedPicoModule 0.3.0 provides per-eye independence at the stock expression cadence using v1. Older Bridge versions (0.3 and up) still work in normal mode without this module.

### About 90 Hz OSC

The eye path delivers about 90 Hz to the PC, but the official VRCFaceTracking host passes it on to OSC at a lower rate: it reads module output and writes OSC on fixed 10 ms waits. We measured this on a PICO 4 Pro streaming through Virtual Desktop, replaying 2,700 recorded eye frames at their original 90 Hz timing and counting new eye directions at the OSC output:

| VRCFT host (5.4.5, Steam build `7add148`) | New eye directions per second |
|---|---:|
| Official, unmodified | about 61–78 Hz |
| Patched, 4 ms waits | about 87–88 Hz |

Windows 11 may ignore the 1 ms timer resolution the official host asks for. When it does, each 10 ms wait takes about 15 ms, which is why the official result varies from run to run.

These figures came from a Ryzen 7 9700X, RTX 4070 Ti SUPER, 64 GB RAM and Windows 11 Pro for Workstations on the Ultimate Performance power plan. Other PCs may see different numbers.

The module works with the official VRCFaceTracking as is. If you want OSC close to 90 Hz, use [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance). It has the host and module processes keep Windows from ignoring their timer resolution, turns the two fixed waits into settings (4 ms by default), and fixes a few unsynchronized data exchanges in the host that shorter waits make more likely to collide. It runs from a separate copy of VRCFaceTracking; releases include the two patched DLLs, and the repository also keeps the source patch if you prefer to build them yourself.

## Requirements

| Item | Requirement |
|---|---|
| Headset | PICO 4 Pro |
| Firmware | PICO OS 5.13.7 (Android 10 / API 29, arm64-v8a) |
| Root | Magisk installed |
| Companion components | PicoFacialBridge and UnifiedPicoModule, to feed the data into VRCFaceTracking |

The installer verifies the SHA-256 of the device's eye-tracking libraries and stops if the firmware build does not match.

## Installation

1. Download `magisk-picoet-enhance-v<version>.zip` from [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases).
2. Flash it in the Magisk app under Install from storage, or from a command line:

   ```sh
    adb push magisk-picoet-enhance-v2.6.0.zip /data/local/tmp/
    adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.6.0.zip'
   ```

3. Reboot the headset.

Fresh installs enable enhanced mode (`dual/on`) at 90/90 Hz after reboot. If you switched to normal mode before upgrading, it stays off.

## Usage

Open **PicoET-Enhance** from the application library. The **LAN broadcast** tab has the master switch, service status, and addresses and client counts for the three cameras. The **Post-processing** tab shows the full tuning panel, with its original left sidebar on the headset.

Under **Result sharing**, **Mouth** and **Both eyes** independently allow clients to subscribe to mouth tracking or [eye expression results](CAMERA-PROTOCOL.md#eye-expression-results-pse1). Both default off and do not affect camera images or Facial Bridge.

Sharing defaults off and remembers your choice. Clients can discover and independently subscribe to all three streams; cameras run only while subscribed. Switching off disconnects every stream; closing the app leaves the service running. Close other eye/face clients once before the first attachment so the adapter can initialize while tracking is idle. Images are unencrypted and unauthenticated, for trusted private networks. The switch does not change processing settings; ordinary expression forwarding still uses Bridge. For a PC-side viewer see [LAN broadcast and PC Studio](#lan-broadcast-and-pc-studio).

Run as root on the headset:

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh toggle
```

| Command | Effect |
|---|---|
| `status` | Shows the saved and applied mode, injection state, service PID, configured effective eye/mouth targets and the current `ro.pxr.externalfunc` value |
| `dual` | Enhanced mode: sets `dual/on` together, opening the pupil gate and enabling the per-eye independent expressions |
| `off` | Normal mode: sets `off/off` together and restarts the service without injection; fully stock |
| `toggle` | Switches to normal mode if enhancement is ready; otherwise attempts to enable enhancement |

Switching restarts the eye-tracking service and disconnects eye and face tracking clients; command-line switching requires `--force` while clients are connected. The Magisk app's Action button switches directly between enhanced and normal mode. After an enhancement failure, pressing it again retries enabling.

`on` is accepted as a synonym for `dual`. The state files are `/data/local/tmp/picoet-mode` and `/data/local/tmp/picoet-gate`, and the log is `/data/local/tmp/picoet-hook.log`.

### Automatic loading and recovery

Automatic injection waits for `sys.boot_completed=1` and a Binder response from the same eye-tracking service instance. Before starting Bridge or another tracking client in enhanced mode, check that `picoet.sh status` shows `phase=ready applied_mode=dual applied_gate=on`.

`ready` confirms the installed hooks and configured target rates. Engines initialized before the hooks are kept running: profile 0 and startup ET settings are not applied retroactively, and the enhancement's own state warms up again rather than forcing an engine rebuild.

If no safe executor is available, or a temporary signal stop interrupts preparation, the injector returns `3`. The watchdog releases the control lock and waits 30 seconds before retrying, without consuming either budget. Synchronous and asynchronous unsafe failures share one automatic recovery per boot or explicit mode command. A separate limit permits injection into at most three consecutive unstable service instances; only the same instance remaining continuously `ready` for 60 seconds replenishes that limit. When a budget is exhausted, automatic injection stops until the next boot or an explicit mode command.

### Eye and mouth rates

The defaults are 90 Hz for eyes and 90 Hz for mouth, and no configuration is needed to use them. To lower either one, write a positive integer to its file:

| File | Default | Effect |
|---|---:|---|
| `/data/local/tmp/picoet-eye-hz` | 90 | Eye-expression rate; 90 processes every eligible fresh camera input |
| `/data/local/tmp/picoet-mouth-hz` | 90 | Mouth-expression rate; cannot exceed the eye rate, because every mouth frame also computes the eyes |

Values above 90 are capped at 90, and a missing or invalid file uses the default. Frames outside the chosen rate are skipped rather than computed. The files are read once when the service starts, so apply a change by stopping tracking clients such as Bridge and re-running `dual`, which restarts the service. For example, to keep eyes at 90 Hz and run the mouth at the stock 24 Hz:

```sh
echo 90 > /data/local/tmp/picoet-eye-hz
echo 24 > /data/local/tmp/picoet-mouth-hz
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh status
```

`status` shows the configured rates as `effective_eye_hz` and `effective_mouth_hz`; these are the targets in effect, not measured output rates. To return to 90/90, delete both files and run `dual` again.

Rates set in 2.4.0 or later survive reinstalls and upgrades. Only the first migration from a version before 2.4.0 clears old overrides to start at 90/90. Normal mode (`off`) is fully stock regardless of these files.

### Headset postprocessing

Open **Expression tuning** from the app home screen. Its Start page, movement list and save/apply controls form the existing independent tuning panel.

PICO's stock firmware postprocesses the expression model's output: brow curves and links, Blink endpoint snapping, adaptive/composite/Kalman filters, mouth baseline removal, log compression, the tongue threshold and smoothing. Its parameters were fixed. Since 2.5.0 this module exposes 44 of them as settings whose defaults are the stock values; the processing algorithms themselves remain PICO's.

The module includes a separate **PicoET-Enhance** settings app. Open it from the headset application library after installation and reboot; uninstalling the module and rebooting removes it together with the module. Start offers four presets. The list on the left names 15 movements to check in a mirror, such as a blink, raising the brows or talking. Each movement page draws what the model outputs and what the avatar receives for that movement, says what usually goes wrong and which slider to move, and shows the settings that shape it; the chart recalculates as you drag. These charts are worked examples at a fixed 90 updates per second, for comparing settings. All settings lists all 44 values in processing order. A switch at the top shows the same settings in Simple wording or Technical wording (variables, formulas and calculation conditions) without changing any setting. Drag a slider for larger changes or tap ◀ / ▶ at either end for one fine step. Tap a value for precise input or an individual reset; no configuration-text editing is needed. Both the panel and PicoFacialBridge remain rootless: the panel submits fixed requests from its private Android sandbox to the module-owned worker. The VRCFT module manages only PC-side processing.

The file is `/data/local/tmp/picoet-postprocessing.conf`: `key=value`, with `#` comments. `version=1` is required; omitted fields keep existing defaults. Each `.strength` accepts 0–1: 0 bypasses the stage, 1 retains its existing amount, and intermediate values mix its input with its result. `eye.enabled` and `mouth.enabled` accept 0/1 to bypass/enable the entire region.

- **Balanced (recommended)**: [`postprocessing-balanced.conf`](magisk-picoet-enhance/postprocessing-balanced.conf) takes a middle ground between Stock and Responsive, moderately reducing eye, brow, mouth and tongue smoothing so small widening movements, blinks, gaze, brows and mouth shapes follow more closely while retaining more smoothing than Responsive. It weakens brow links and weak-shape compression, removes the second mouth smoothing pass, speeds up rest learning and sets the tongue threshold to 0.35. Blink snapping, rest removal and the lip closure boost stay on. Fine motion remains more visible than under stock settings. Unlisted fields use the defaults.
- **Responsive**: [`postprocessing-responsive.conf`](magisk-picoet-enhance/postprocessing-responsive.conf) keeps about half of the smoothing and turns the brow links off, so every movement follows the model within about 0.1 s. Small jitter shows while holding still. Blink snapping, rest removal and the MouthClose addition stay on.
- **No processing**: [`postprocessing-raw.conf`](magisk-picoet-enhance/postprocessing-raw.conf) emits bounded current model values and skips the stock postprocessing above. Per-eye quality, references, source identity, validity and 0–1 output bounds remain enabled. This is not raw camera data or a bypass of processing inside the model.
- **Stock / individual tuning**: [`postprocessing-default.conf`](magisk-picoet-enhance/postprocessing-default.conf) lists all 44 parameters with PICO's original values. Higher `adaptive.k` or `current_weight` follows current samples more closely. Increasing Kalman process noise or decreasing measurement noise generally reduces smoothing. Bypassing filters exposes more noise and abrupt changes; PC/VRCFT filters remain independent.

The bottom bar distinguishes unsaved changes, saved settings awaiting application, and a successful application in the current session. Save changes before applying; unsaved edits disable Apply. Reloading saved values alone does not verify the running configuration. Saving does not restart tracking. Stop Bridge and other eye/face clients, then tap **Apply**. This restarts the eye service only if no clients are connected and enhancement is already enabled. Alternatively, in a headset root shell:

```sh
MOD=/data/adb/modules/picoet-enhance
"$MOD/bin/picoet-config" write < "$MOD/postprocessing-balanced.conf"
sh "$MOD/picoet.sh" reload-processing
```

Changes load at the next payload startup and survive reinstalls/upgrades. To adopt an updated preset, select it again, then save and apply it. Invalid, duplicate or unknown fields reject a save; malformed or incorrectly owned files at startup report failure rather than silently restoring filters. A missing file keeps existing behavior. Headset native numerical tests, companion boot integration and rootless IPC save/apply/restore pass on PICO 4 Pro / PICO OS 5.13.7 with SELinux enforcing. Worn response and noise still need comparison.

## LAN broadcast and PC Studio

With LAN broadcast on, the headset serves the mouth, left-eye and right-eye cameras as ordinary MJPEG streams (default ports 45681, 45682 and 45683) and announces them for discovery. The wire formats, discovery and result messages are in [CAMERA-PROTOCOL.md](CAMERA-PROTOCOL.md).

PC Studio in [pc-studio](pc-studio) is a Windows app that forwards and previews these streams. Install it with `PicoET-PC-Studio-Setup-v<version>.exe` from [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases) (installs to `%LOCALAPPDATA%\Programs\PicoET PC Studio` without administrator rights, creates a Start menu shortcut, and opens in an Edge/Chrome app window), or unpack the portable archive `picoet-pc-studio-v<version>.zip` and run `PicoET-PC-Studio.exe`. You can also run it directly from source with Python 3 and Pillow:

```powershell
pip install -r pc-studio/requirements.txt
pc-studio\start.cmd          # opens http://127.0.0.1:8787
```

It offers:

- headset discovery on the local network, or a manually entered address;
- live preview switching across the mouth, left-eye and right-eye cameras, along with each stream's MJPEG URL (`http://<headset address>:<port>/stream.mjpg`) for other tools;
- an eye relay that sends the two eye streams' PTJ1 data to Cymple's native UDP input (`127.0.0.1:23303`), with configurable ports and per-eye rotation/flip;
- an in-app version check against GitHub Releases (with direct download links for new PC Studio and headset module releases) and a Quit button to stop the background service.

`python pc-studio/check.py` runs its tests, and `python pc-studio/package_release.py` builds the standalone bundle, portable ZIP and Inno Setup installer.

## About the enterprise gate

The firmware uses `PXR::EyeUtil::isBusinessDev()` to control per-eye output, including the real pupil diameter. This module makes the function return true inside the eye-tracking service. The device-wide enterprise-mode property `ro.pxr.externalfunc` keeps its existing value.

## Compatibility and verification

The startup-safety changes were validated with a local NDK 26.1.10909125 build on PICO 4 Pro / PICO OS 5.13.7 with SELinux permissive: normal boot, upgrade, mode switching, repeated control commands and targeted failure branches passed. A 45-second worn session confirmed live eye and mouth updates, blink and mouth response, profile 0 initialization without retries, and normal tracking shutdown without a service restart. The stock libraries were unchanged. The 2.5.0 settings app, configuration helper and preset files also passed boot and companion IPC checks with SELinux enforcing. The release workflow explicitly selects NDK 26.1.10909125 and CMake 3.22.1.

The following 90/90 measurements are from the earlier 2.4.0 validation, not a new throughput test for later versions. The PICO 4 Pro (PICO OS 5.13.7) was awake with tracking ready and not worn. The test replayed 2,700 recorded camera inputs (about 30 seconds) through a test harness that drives the module's expression-processing algorithm, with PicoFacialBridge 0.5.0, UnifiedPicoModule 0.4.0, VRCFT-Enhance and desktop VRChat:

| Stage | Rate |
|---|---:|
| Eye and mouth computation on the headset | 89.80 Hz (2,694 of 2,700 inputs; the other six hit the stock full-queue drop rule, with no inference failures) |
| Bridge → PC (`X` packets) | 89.84 Hz |
| VRCFT → OSC, eye and mouth from the same source | 87.80 Hz (2,633 sources, 97.5% of inputs) |

For every source that reached OSC, VRChat sent back at least one eye and one mouth value equal to the one written. Values matched their source at every stage; this covers the supported fields the test avatar actually sends. The loopback packet capture dropped packets, so the PC-side counts are verifiable lower bounds. From `X` arrival at the PC to OSC output, the median is 9.1 ms and the 95th percentile 12.5 ms. The replay was not run through the installed Magisk package, and it does not include live camera copying or Virtual Desktop decoding and rendering.

On version 2.3.0 (mouth at about 24 Hz), official VRCFT 5.4.5 was also run for 84 seconds worn on Virtual Desktop with Cymple mouth tracking and PICO eyes: about 89.7 `X` packets per second reached the PC, each with a new eye source.

Not yet covered: single-eye quality failure, staggered reference rebuild, a live mouth-output comparison and long live-camera sessions. Combined gaze remains unchanged, and the host-side brow parameters used by current avatars average the two sides even though the extension carries both.

## Disabling and uninstalling

Run `picoet.sh off` to disable enhancement and restore the stock eye-tracking service. To remove the module, uninstall it in Magisk and reboot the headset; the settings app is removed with the module and the postprocessing configuration file is deleted. Cleanup removes known state files without recursively entering unexpected directories or nested mounts, so those directories may remain.

## Building from source

Building requires Python 3, JDK 17 or newer (`JAVA_HOME`), Android platform 35 and build-tools 35.0.0 (`ANDROID_HOME`), plus NDK 26.1.10909125 and CMake 3.22.1/Ninja. Native scripts accept `-Ndk` / `-CmakeBin`, `PICOET_NDK` / `PICOET_CMAKE_BIN`, or the SDK layout under `ANDROID_HOME`.

```powershell
pwsh -File hook\build-hook.ps1                 # build the injector and payload
python camera-source/build.py android --ndk "$env:ANDROID_HOME/ndk/26.1.10909125" --cmake "$env:ANDROID_HOME/cmake/3.22.1/bin/cmake.exe" --ninja "$env:ANDROID_HOME/cmake/3.22.1/bin/ninja.exe"
pwsh -File build-picoet-enhance.ps1 -Python python # build companion and package the Magisk zip
```

The output is `artifacts/magisk-picoet-enhance-v<version>.zip`, containing the module scripts, native tools, hook payload and `system/app/PicoETEnhance/PicoETEnhance.apk`. Packaging checks the ELF file list and stock-library hashes. The companion uses the SDK directly, without Gradle or downloaded Android libraries.

Without a keystore, local builds create and reuse a development-only signing key. For distribution, pass a stable `-PanelKeystore` (or `PICOET_PANEL_KEYSTORE`), optionally `-PanelKeyAlias`, and set `PICOET_PANEL_PASSWORD`. Updates must use the same panel signing identity. Tag builds require the `PICOET_PANEL_KEYSTORE_BASE64` and `PICOET_PANEL_PASSWORD` repository secrets; `PICOET_PANEL_KEY_ALIAS` is optional and defaults to `panel`.

Pushing a `v<version>` tag makes GitHub Actions build on a Windows runner and publish the zip with a checksum.

## TODO

- [ ] Tongue-direction support: PICO currently exposes only the scalar `TongueOut`, while VRCFaceTracking already defines direction channels such as `TongueLeft/Right/Up/Down`. The plan is to capture raw frames from the lower-face camera, auto-label them with visual segmentation foundation models, train a small model, and let users collect their own data for a local online fine-tune.
- [ ] Worn comparison of the balanced, responsive and no-processing presets: stationary noise and avatar response.

## Feedback

Issues are welcome. Please include the headset model, PICO OS version, module version, the current mode, the output of `picoet.sh status`, and relevant log excerpts.

## Community

Join the Pico-Enhance community for help, discussion and release announcements.

<a href="https://discord.gg/ueSEydeVD8"><img src="https://img.shields.io/badge/Discord-Join%20Pico--Enhance-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join Pico-Enhance on Discord"></a>

**QQ group:** 1129699813. Scan the code below with QQ to join.

<img src="assets/community/qq-group-qr.png" alt="Pico-Enhance QQ group QR code" width="240">

## License

The original code and scripts of this project are licensed under [MIT](LICENSE). The vendored ShadowHook 2.0.1 sources include MIT- and BSD-licensed dependencies; the Magisk ZIP includes their notices and the project license. This project contains and distributes no PICO binary; it only references the device's own libraries by numeric offset at runtime. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
