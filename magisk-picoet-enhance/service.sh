#!/system/bin/sh
# late_start: apply the saved mode once the eye-tracking service is up, then keep
# re-injecting if the service is restarted (the enhancement lives in process memory only).
# Nothing is mounted and no persistent property is written; ro.pxr.externalfunc is never
# touched. The companion app state (picoet.enhance.mode/gate) is transient and re-published
# by picoet.sh whenever the injection state changes.

MODDIR=${0%/*}
STATE_DIR=/data/adb/picoet-enhance
LOG=/data/local/tmp/picoet-hook.log             # payload-hardcoded path; keep despite the module rename
MODE_FILE=/data/local/tmp/picoet-mode
GATE_FILE=/data/local/tmp/picoet-gate

# The redirects below must not fail on a fresh install / first boot: create the
# state directory before anything writes into it.
mkdir -p "$STATE_DIR" 2>/dev/null

# clear the transient companion-app state first: a boot where the module is disabled must
# not look "enhanced"
resetprop --delete picoet.enhance.mode 2>/dev/null
resetprop --delete picoet.enhance.gate 2>/dev/null

case "$(cat "$MODE_FILE" 2>/dev/null)" in
    off|left|right|dual) ;;
    *) echo off > "$MODE_FILE" ;;
esac
case "$(cat "$GATE_FILE" 2>/dev/null)" in
    on|off) ;;
    *) echo on > "$GATE_FILE" ;;
esac

# wait for the service (up to ~30 s), then apply the saved state
i=0
while [ $i -lt 15 ]; do
    if [ -n "$(pidof pxreyetrackingservice)" ]; then
        /system/bin/sh "$MODDIR/picoet.sh" ensure >> "$STATE_DIR/service.log" 2>&1
        break
    fi
    sleep 2
    i=$((i + 1))
done

# watchdog: the service can be restarted at any time (init, settings, other modules), which
# unloads the hook; re-inject whenever a mode is active and the library is not mapped.
(
    while [ -d "$MODDIR" ]; do
        sleep 10
        mode=$(cat "$MODE_FILE" 2>/dev/null)
        [ "$mode" = off ] && continue
        case "$mode" in left|right|dual) ;; *) continue ;; esac
        pid=$(pidof pxreyetrackingservice)
        [ -z "$pid" ] && continue
        if ! grep -q libpicoet_hook "/proc/$pid/maps" 2>/dev/null; then
            /system/bin/sh "$MODDIR/picoet.sh" ensure >> "$STATE_DIR/service.log" 2>&1
        fi
    done
) &

echo "$(date) service.sh: mode=$(cat "$MODE_FILE" 2>/dev/null) gate=$(cat "$GATE_FILE" 2>/dev/null) ro.pxr.externalfunc=$(getprop ro.pxr.externalfunc)" >> "$LOG" 2>/dev/null
