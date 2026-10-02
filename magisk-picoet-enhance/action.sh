#!/system/bin/sh
# Magisk app "Action" button: cycle off -> left -> right -> dual -> off.
# Pressing it is an explicit request, so a running tracking session is restarted (--force).
MODDIR=${0%/*}
case "$(cat /data/local/tmp/picoet-mode 2>/dev/null)" in
    off) NEXT=left ;;
    left) NEXT=right ;;
    right) NEXT=dual ;;
    *) NEXT=off ;;
esac
echo "- switching to $NEXT (eye/face tracking restarts)"
/system/bin/sh "$MODDIR/picoet.sh" "$NEXT" --force
/system/bin/sh "$MODDIR/picoet.sh" status
