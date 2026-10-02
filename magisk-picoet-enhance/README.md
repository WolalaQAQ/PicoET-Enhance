# Magisk module

This directory holds the Magisk module that applies PicoET-Enhance at runtime. The
packaged zip contains only our own artifacts:

```
module.prop            # id=picoet-enhance
customize.sh           # verifies the device firmware SHA-256, refuses unknown builds
post-fs-data.sh        # clears the transient companion-app state
service.sh             # late_start: inject once the service is up; watchdog re-injects
picoet.sh              # status / off|left|right|dual / gate on|off / ensure
action.sh              # Magisk app Action button: off -> left -> right -> dual -> off
uninstall.sh
sepolicy.rule          # best-effort rules for an enforcing build
bin/picoet-inject      # our arm64 ptrace injector
lib/libpicoet_hook.so  # our payload (gate + plugin capture + left/right + dual)
hashes.sh              # generated at build time: SHA-256 of the two artifacts
```

No PICO binary is included. The device libraries are referenced only by name and
numeric offset at runtime, behind firmware guards.

## Usage

```sh
sh /data/adb/modules/picoet-enhance/picoet.sh status
sh /data/adb/modules/picoet-enhance/picoet.sh dual            # per-eye gaze (gate on)
sh /data/adb/modules/picoet-enhance/picoet.sh left|right      # select one eye
sh /data/adb/modules/picoet-enhance/picoet.sh off             # restart without injection = stock
sh /data/adb/modules/picoet-enhance/picoet.sh gate on|off     # gate layer only
```

Switching restarts the eye-tracking service and disconnects eye/face tracking clients.
When clients are connected the switch is refused unless `--force` is given.

State files are `/data/local/tmp/picoet-mode` and `/data/local/tmp/picoet-gate`; these names
are compiled into the payload, so they do not follow the module id. Logs are
`/data/local/tmp/picoet-hook.log` (payload) and `/data/adb/picoet-enhance/*.log` (scripts).
The companion-app properties `picoet.enhance.mode` and `picoet.enhance.gate` are transient:
a reboot, an uninstall or a disabled module always clears them.

## Rollback

- `picoet.sh off` restarts the service without injection and returns to stock.
- Uninstalling the module and rebooting is fully clean: the hook only ever lives in the
  target process's memory.

## Limits

- Supports PICO OS 5.13.7 only, pinned by the SHA-256 of both device libraries.
  `customize.sh` refuses to install on an unknown build, and the payload's own anchor and
  hash guards refuse to load.
- The SEPolicy rules are best-effort and have not been exercised: the supported device runs
  with SELinux permissive.
