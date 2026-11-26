# PicoET-Enhance Module Reference (`v2.7.0`)

**English** | [简体中文](README_zh.md)

Technical reference for the installed Magisk module at `/data/adb/modules/picoet-enhance/`. For user installation, companion app walkthroughs, and PC Studio setup, see the [main README](../README.md).

## Target & Verification Boundary

- **Target**: PICO 4 Pro running PICO OS 5.13.7 (Android 10 / API 29, `arm64-v8a`) with Magisk under SELinux Enforcing.
- **Zero PICO bytes**: The module ships only our own scripts, binaries, presets, and companion APK. `customize.sh` checks the SHA-256 of `/system/lib64/libpxreyetrackingservice.so` and `/system/lib64/libpxreyetracking.phoenix.so` and aborts on mismatch.
- **Memory-only hooks**: Hooks live inside `pxreyetrackingservice` process memory via `ptrace` injection (`picoet-inject` + `libpicoet_hook.so`). No system library is modified on disk, and `ro.pxr.externalfunc` is never touched.

## Module Layout

| Path (under `/data/adb/modules/picoet-enhance/`) | Role |
| --- | --- |
| `module.prop`, `sepolicy.rule`, `hashes.sh` | Module metadata (`id=picoet-enhance`, `version=2.7.0`, `versionCode=31`), SELinux rules, and SHA-256 verification pins for `picoet-inject` and `libpicoet_hook.so` |
| `customize.sh` | Install-time library hash check, permission setup, `/data/system/package_cache/*/PicoETEnhance-*` cleanup, and mode normalization |
| `post-fs-data.sh` | Intentionally a no-op so boot is never blocked; transient properties reset on reboot |
| `service.sh` | `late_start` supervisor: waits for `sys.boot_completed=1`, launches `picoet-panel`, runs `picoet.sh ensure` and `picoet-camera reconcile` in a loop |
| `picoet.sh` | Root CLI controller and watchdog transaction script (`status`, `dual`/`on`, `off`, `toggle`, `reload-processing`, internal `boot`/`ensure`) |
| `action.sh` | Magisk **Action** button handler (`picoet.sh toggle --force`, then prints `status`) |
| `uninstall.sh` | Post-removal cleanup of state files, runtime directories, rate/post-processing configs, and transient properties |
| `bin/picoet-inject` | ARM64 `ptrace` injector, file/lock helper (`--run-locked`, `--prepare-runtime`, `--save-mode`, `--normalize-mode`, `--read-file`, `--read-log`) |
| `bin/picoet-config` | Root CLI for reading, validating, and atomically writing `/data/local/tmp/picoet-postprocessing.conf` |
| `bin/picoet-panel` | Module-owned root mailbox worker serving fixed requests from the zero-permission `dev.pico.etenhance` APK |
| `bin/picoet-camera` | Module-owned controller for the LAN camera adapter and IPC daemon (`on`, `off`, `status`, `reconcile`, `halt`, `results-on/off`, `eyes-on/off`) |
| `bin/ptj_ipc_daemon` | Standalone root transport daemon (libjpeg-turbo encoding, UDP discovery, HTTP MJPEG, `PTJ1`, `PSL1`, `PSE1`) |
| `lib/libpicoet_hook.so` | In-process hook payload (pupil gate, independent per-eye expressions, 90/90 Hz scheduling, `EPI1` v2 export, 44-parameter stock post-processing config) |
| `lib/libpicoet_camera_jpeg.so`, `lib/libptj_source.so` | In-process LAN camera adapter and shared-memory shim (`PTJIPC5`), staged per service instance on demand |
| `system/app/PicoETEnhance/PicoETEnhance.apk` | Systemlessly overlaid zero-permission companion app (**LAN broadcast** & **Post-processing** tabs) |
| `postprocessing-{default,balanced,responsive,raw}.conf` | Preset configurations for PICO's 44 built-in post-processing parameters |

## Runtime Properties, State Files & Logs

### Transient Android Properties (Published by `picoet.sh`)

- `picoet.enhance.mode` (`off` | `dual`) and `picoet.enhance.gate` (`off` | `on`): Read by `PicoFacialBridge` (`0.5.0+`) to detect enhanced mode and advertise `EPI1`/`X` v2 to `UnifiedPicoModule` (`0.4.0+`). Properties are cleared whenever the service restarts or exits.

### Persistent & Runtime Paths

| Path | Purpose |
| --- | --- |
| `/data/local/tmp/picoet-mode`, `/data/local/tmp/picoet-gate` | Saved mode (`dual`/`off`) and gate (`on`/`off`), written atomically with `u:object_r:shell_data_file:s0` |
| `/data/local/tmp/picoet-eye-hz`, `/data/local/tmp/picoet-mouth-hz` | Optional startup-only rate overrides (positive integers `1..90`; defaults `90/90`, mouth clamped to eye; survive upgrades) |
| `/data/local/tmp/picoet-postprocessing.conf` | Saved 44-parameter post-processing config (`version=1`, `key=value`, max 16 KiB; read once at payload startup; survives upgrades) |
| `/data/local/tmp/picoet-hook.log` | Payload runtime log |
| `/data/local/tmp/picoet-runtime/` | Private runtime directory holding per-PID payload state reports (`<pid>.state`) and async `unsafe` tokens |
| `/data/local/tmp/picoet-camera/` | Per-instance `0700` stage directories for `video-channel.bin`, staged camera `.so` copies, `camera-status.json`, and `daemon.log` |
| `/data/adb/picoet-enhance/` | Module control state (`control.lock`, `boot.id`, `recovery.used`, `injection.blocked`, `unsafe.instance`, `unsafe.handled`, `automatic.attempts`, `camera.enabled`, `camera.results`, `camera.eyes`, `camera.instance`, `camera.failure`) and control logs (`picoet.log`, `inject.log`, `service.log`, `panel.log`) |

## CLI Reference

Run as root on the headset:

```sh
MOD=/data/adb/modules/picoet-enhance

# Mode & status
sh "$MOD/picoet.sh" status
sh "$MOD/picoet.sh" dual              # alias: on (adds --force to drop connected clients)
sh "$MOD/picoet.sh" off               # restore stock service without injection
sh "$MOD/picoet.sh" toggle            # dual -> off when ready, otherwise enables dual
sh "$MOD/picoet.sh" reload-processing # validate config & restart if enhanced and idle (no --force)

# Post-processing config helper
"$MOD/bin/picoet-config" read|check|write|defaults|balanced|responsive|raw
```

- Fresh installs initialize `dual/on` at 90/90 Hz; upgrades preserve an existing `off` state.
- `status` reports `effective_eye_hz` and `effective_mouth_hz` (configured targets, not measured FPS), `phase`, `applied_mode`, `applied_gate`, active `dumpsys` clients, and the last 5 lines of `/data/local/tmp/picoet-hook.log`.
- To apply rate overrides, stop tracking clients, write `/data/local/tmp/picoet-eye-hz` and/or `/data/local/tmp/picoet-mouth-hz`, and run `picoet.sh dual`. Delete the files and re-run `dual` to restore 90/90 Hz.
- `picoet-config write` reads from `stdin` and rejects invalid, duplicate, or unknown keys. Malformed or improperly owned config files fail explicitly at payload startup rather than silently falling back.

## Injection Lifecycle & Recovery Budgets

All control actions (`picoet.sh` and `picoet-camera`) serialize on `/data/adb/picoet-enhance/control.lock`.

1. **Readiness gate**: `service.sh` waits for `sys.boot_completed=1`. Before any `ptrace` attach, `picoet.sh` verifies `init.svc.pxreyetrackingservice=running` and a bounded `dumpsys -t 1 pxreyetrackingservice` response containing all three stock algorithm lines (`ET`, `FT`, `IPD`) for the exact `pid:starttime` instance.
2. **Runtime verification**: An executable mapping of `libpicoet_hook.so` is never treated as ready on its own. The payload must atomically publish a schema-2 report matching the live `pid:starttime` with `phase=ready`. Wait for `phase=ready applied_mode=dual applied_gate=on` before starting tracking clients. Engines already initialized prior to injection are kept running and warm up enhancement state without forcing an engine rebuild.
3. **Injector exit codes & recovery rules**:
   - `0` — Attached and `dlopen` succeeded (readiness still confirmed via payload state file).
   - `2` — Safe/known failure; blocks further automatic injection until an explicit mode command or reboot.
   - `3` — No safe user-instruction executor thread or temporary signal stop; **no bytes written**. `service.sh` releases the lock and cools down **30 seconds** without consuming either budget.
   - `4` / `5` — Stale partial mapping (`4`) or unsafe remote/restoration state where target was terminated (`5`); consumes the shared recovery budget (**1** automatic restart-and-retry per boot or explicit mode command). Asynchronous rollback failures in the payload share this same 1-shot budget.
   - `6` — `--run-locked` busy (another control command owns `control.lock`).
4. **Consecutive instability limit**: Automatic injection attempts at most **3** consecutive unstable service instances (`automatic.attempts`). The counter resets only after the same instance stays continuously `ready` for **60 seconds**.
5. **Manual reset**: Running an explicit mode command (`picoet.sh dual`, `off`, `toggle`, or Magisk **Action**) clears `injection.blocked`, `recovery.used`, and `automatic.attempts`.

## Companion App & Camera Control Boundary

- The companion APK (`dev.pico.etenhance`) declares zero Android permissions and never runs shell commands or opens sockets.
- `picoet-panel` validates the caller UID against `dev.pico.etenhance` and processes fixed mailbox requests using descriptor-relative `openat` + `O_NOFOLLOW`.
- `picoet-camera` handles **LAN broadcast** (`on` / `off`), **Mouth** (`results-on` / `results-off`), and **Both eyes** (`eyes-on` / `eyes-off`) under `control.lock`. First camera attachment verifies idle tracking via `dumpsys` without restarting `pxreyetrackingservice`. If a separately started developer adapter (`camera-source/device.py`) is already resident, `picoet-camera` reports `foreign_adapter` and refuses until that session is stopped and the service restarted.
- For wire protocols and `PTJIPC5` details, see [CAMERA-PROTOCOL.md](../CAMERA-PROTOCOL.md).

## Uninstalling

Uninstall **PicoET-Enhance** in Magisk and reboot. `uninstall.sh` removes known state files, runtime directories, rate/post-processing configs, and transient properties without recursing into unexpected directories or nested mounts.
