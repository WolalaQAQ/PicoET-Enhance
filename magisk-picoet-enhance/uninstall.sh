#!/system/bin/sh
# Runs at the boot after removal. The enhancement only lived in the eye-tracking service's
# memory, so a reboot restores the stock service by itself; only the saved state and the
# mode/gate files need cleaning. No system property was ever written by this module.
MODDIR=${0%/*}
# The native helper uses the verified directory FD and only unlinks our report
# leaves. Never recursively descend into unexpected directories or submounts.
if [ -x "$MODDIR/bin/picoet-inject" ]; then
    "$MODDIR/bin/picoet-inject" --prepare-runtime --reset 2>/dev/null
fi
rmdir /data/local/tmp/picoet-runtime 2>/dev/null || true
rm -f /data/local/tmp/picoet-runtime 2>/dev/null || true
rm -rf /data/local/tmp/picoet-camera 2>/dev/null || true
state=/data/adb/picoet-enhance
rm -f "$state/boot.id" "$state/control.lock" "$state/recovery.used" \
    "$state/injection.blocked" "$state/unsafe.instance" "$state/unsafe.handled" \
    "$state/automatic.attempts" "$state/automatic.attempts.tmp" \
    "$state/picoet.log" "$state/inject.log" "$state/service.log" "$state/panel.log" "$state/panel.lock" "$state"/setting.* 2>/dev/null || true
rm -f "$state/camera.enabled" "$state/camera.enabled.new" "$state/camera.failure" \
    "$state/camera.results" "$state/camera.results.new" \
    "$state/camera.eyes" "$state/camera.eyes.new" \
    "$state/camera.failure.new" "$state/camera.instance" "$state/camera.instance.new"
rmdir "$state" 2>/dev/null || true
rm -f /data/local/tmp/picoet-mode /data/local/tmp/picoet-gate /data/local/tmp/picoet-hook.log
rm -f /data/local/tmp/picoet-eye-hz /data/local/tmp/picoet-mouth-hz
rm -f /data/local/tmp/picoet-postprocessing.conf
resetprop --delete picoet.enhance.mode 2>/dev/null
resetprop --delete picoet.enhance.gate 2>/dev/null
