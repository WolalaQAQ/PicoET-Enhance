#!/system/bin/sh
# Runs at the boot after removal. The enhancement only lived in the eye-tracking service's
# memory, so a reboot restores the stock service by itself; only the saved state and the
# mode/gate files need cleaning. No system property was ever written by this module.
rm -rf /data/adb/picoet-enhance
rm -rf /data/local/tmp/picoet-runtime
rm -f /data/local/tmp/picoet-mode /data/local/tmp/picoet-gate /data/local/tmp/picoet-hook.log
resetprop --delete picoet.enhance.mode 2>/dev/null
resetprop --delete picoet.enhance.gate 2>/dev/null
