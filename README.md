# PicoET-Enhance

English | [简体中文](README_zh.md)

An eye-tracking enhancement Magisk module for the PICO 4 Pro: it makes the headset's eye-tracking service output true per-eye gaze and pupil data, for use with [PicoFacialBridge](https://github.com/WolalaQAQ/PicoFacialBridge) and [UnifiedPicoModule](https://github.com/WolalaQAQ/UnifiedPicoModule).

> Current version v2.0.3 targets PICO 4 Pro running PICO OS 5.13.7. Version 2.0.2 was verified on that setup; this release's fixes have passed build and static checks and await headset revalidation. See the [changelog](CHANGELOG.md).

## What it does

The PICO 4 Pro firmware already computes gaze for each eye, but by default it only exposes the fused result. This module corrects that at runtime in two ways:

- it opens the per-eye gate inside the eye-tracking service, so per-eye gaze and real pupil data are written to shared memory;
- it makes the gaze algorithm run the left and right single-eye models so the per-eye vectors are genuine (`dual` mode), or uses only one of them (`left` / `right`).

The correction happens entirely inside the eye-tracking service's memory. The module does not repackage any app, does not write to `/system`, and does not change any persistent property.

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

This module only makes the headset produce better data. It publishes the active mode as two transient properties that PicoFacialBridge reads without root, so the bridge can detect normal versus enhanced mode by itself and tell the PC module. No extra PC-side configuration is needed for this module.

## Requirements

| Item | Requirement |
|---|---|
| Headset | PICO 4 Pro |
| Firmware | PICO OS 5.13.7 (Android 10 / API 29, arm64-v8a) |
| Root | Magisk installed |
| Companion components | PicoFacialBridge and UnifiedPicoModule, to feed the data into VRCFaceTracking |

Other models and firmware versions have not been confirmed to work. The installer verifies the SHA-256 of both device eye-tracking libraries and refuses to install on a different build; even if that check is bypassed, the runtime payload's anchor and hash guards refuse to load and the service keeps running unmodified.

## Installation

1. Download `magisk-picoet-enhance-v<version>.zip` from [Releases](https://github.com/WolalaQAQ/PicoET-Enhance/releases).
2. Flash it in the Magisk app under Install from storage, or from a command line:

   ```sh
   adb push magisk-picoet-enhance-v2.0.3.zip /data/local/tmp/
   adb shell su -c 'magisk --install-module /data/local/tmp/magisk-picoet-enhance-v2.0.3.zip'
   ```

3. Reboot the headset.

A fresh install starts in `off`, that is, with no injection and stock behavior. Choose a mode after rebooting.

## Usage

Run as root on the headset:

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual
sh /data/adb/modules/picoet-enhance/picoet.sh left
sh /data/adb/modules/picoet-enhance/picoet.sh right
sh /data/adb/modules/picoet-enhance/picoet.sh off
sh /data/adb/modules/picoet-enhance/picoet.sh gate on
sh /data/adb/modules/picoet-enhance/picoet.sh gate off
```

| Command | Effect |
|---|---|
| `status` | Shows the saved mode, injection state, service PID and the current `ro.pxr.externalfunc` value |
| `dual` | Per-eye gaze. Runs both single-eye models and outputs genuine per-eye vectors (recommended) |
| `left` / `right` | The fused gaze comes from the selected eye's single-eye model only |
| `off` | Restarts the service without injection; fully stock |
| `gate on` / `gate off` | Controls the gate layer alone. With it open the firmware writes per-eye and pupil fields, but gaze is still the fixed-depth split of the fused result |

Switching restarts the eye-tracking service, so any active eye or face tracking client is disconnected. The switch is refused while clients are connected unless `--force` is given. The Magisk app's Action button cycles `off → left → right → dual → off`.

The state files are `/data/local/tmp/picoet-mode` and `/data/local/tmp/picoet-gate`, and the log is `/data/local/tmp/picoet-hook.log`. These paths are compiled into the payload and do not follow the module id.

## About the enterprise gate

The switch that controls per-eye output in the firmware is `PXR::EyeUtil::isBusinessDev()`. Besides per-eye tracking, it is also the device-wide enterprise (ToB) license flag: when the system property `ro.pxr.externalfunc` is 1, the framework, VR shell, settings and a set of enterprise apps all switch to enterprise behavior, and the product name changes.

This module does not touch that property and does not modify any system file. It only uses a runtime hook so that calls to that function inside the eye-tracking service process return true, scoped to that process. The module's status output shows `ro.pxr.externalfunc` staying at 0, and removing the module leaves no persistent change behind.

## Compatibility and verification

On the supported setup (PICO 4 Pro / PICO OS 5.13.7 / SELinux permissive) the following have been verified:

- genuine vergence on real eye frames in `dual` mode (following the fixation distance, not a fixed-depth split);
- blink regression in `left` / `right` mode;
- returning to stock after `off` or uninstall;
- automatic injection at boot, and re-injection by a watchdog after the eye-tracking service restarts.

Not yet verified: the SEPolicy rules when SELinux is enforcing (a best-effort rule file ships with the module, but the current device is permissive, so it has never taken effect), the `right` mode with the left eye closed, and full PC/VRCFT-side evidence.

## Risks and rollback

- Switching modes restarts the eye-tracking service, disconnecting all eye and face tracking clients.
- The payload refuses to load on unknown firmware because the guards do not match, but injection is a low-level operation and could in principle upset the service. The service's init entry is not marked critical, so the system brings it back up if it fails.
- Rollback: `picoet.sh off` returns to stock immediately; uninstalling the module and rebooting clears everything. The hook only ever lives in process memory and disappears when the service restarts.

## Building from source

Building requires the Android NDK and CMake/Ninja. The scripts look for the toolchain in the `-Ndk` / `-CmakeBin` parameters, the `PICOET_NDK` / `PICOET_CMAKE_BIN` environment variables, or a standard layout under `ANDROID_HOME`.

```powershell
pwsh -File hook\build-hook.ps1                 # build the injector and payload
pwsh -File build-picoet-enhance.ps1            # package the Magisk zip
```

The output is `artifacts/magisk-picoet-enhance-v<version>.zip`. The packaging script scans every staged file, rejects any known PICO library hash, and only allows our own two ELF artifacts, so the release zip never contains PICO bytes.

Pushing a `v<version>` tag makes GitHub Actions build on a Windows runner and publish the zip with a checksum.

## TODO

- [ ] Tongue-direction support: PICO currently exposes only the scalar `TongueOut`, while VRCFaceTracking already defines direction channels such as `TongueLeft/Right/Up/Down`. The plan is to capture raw frames from the lower-face camera, auto-label them with visual segmentation foundation models, train a small model, and let users collect their own data for a local online fine-tune.
- [ ] Unfiltered face output: make the on-device FaceTrackor temporal smoothing optional so the module can forward per-frame raw face data. Filtering then moves to the PC side, where it can be tuned as needed.

## Feedback

Issues are welcome. Please include the headset model, PICO OS version, module version, the current mode, the output of `picoet.sh status`, and relevant log excerpts. This module is intended only for your own device.

## License

The original code and scripts of this project are licensed under [MIT](LICENSE). `hook/third_party/shadowhook-2.0.1/` is a source copy of ByteDance ShadowHook 2.0.1, licensed under MIT. This project contains and distributes no PICO binary; it only references the device's own libraries by numeric offset at runtime. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
