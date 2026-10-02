#!/system/bin/sh
# Deliberately does nothing but clear the transient companion-app state: the enhancement is
# applied by service.sh at late_start, once the eye-tracking service is up. No mounts, no
# properties, no system files.

resetprop --delete picoet.enhance.mode 2>/dev/null
resetprop --delete picoet.enhance.gate 2>/dev/null
# Runtime reports never survive a boot (PID/starttime values may repeat across boots).
rm -f /data/local/tmp/picoet-runtime/*.state /data/local/tmp/picoet-runtime/*.tmp
