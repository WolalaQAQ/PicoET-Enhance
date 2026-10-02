#!/system/bin/sh
# PicoET-Enhance: runtime switch for the eye-tracking enhancement.
#
#   picoet.sh status
#   picoet.sh off|left|right|dual [--force]
#   picoet.sh gate on|off         [--force]
#   picoet.sh ensure              # (internal) inject into the running service if needed
#
# The enhancement lives only in the eye-tracking service's memory: bin/picoet-inject
# ptrace-attaches to pxreyetrackingservice and dlopens lib/libpicoet_hook.so, which installs
# our hooks (per-eye gate; dual or left/right gaze). No system file is written, no mount is
# made, no persistent property is set and ro.pxr.externalfunc is never touched. A switch
# restarts the service and drops every eye/face tracking client; that is refused while
# clients are connected unless --force is given.
#
#   /data/local/tmp/picoet-mode : off|left|right|dual   (off = no injection at all)
#   /data/local/tmp/picoet-gate : on|off                (default on; only used when active)

MODDIR=${0%/*}
BIN=$MODDIR/bin/picoet-inject
LIB=$MODDIR/lib/libpicoet_hook.so
MODE_FILE=/data/local/tmp/picoet-mode
GATE_FILE=/data/local/tmp/picoet-gate
STATE_DIR=/data/adb/picoet-enhance
LOG=$STATE_DIR/picoet.log
PLOG=/data/local/tmp/picoet-hook.log            # payload-hardcoded path; keep despite the module rename
SERVICE=pxreyetrackingservice

if [ -f "$MODDIR/hashes.sh" ]; then
    . "$MODDIR/hashes.sh"
else
    INJECT_SHA=
    PAYLOAD_SHA=
fi

# Create the state directory up front: inject_now() redirects into it before any
# log() call gets a chance to mkdir it (first run / fresh install).
mkdir -p "$STATE_DIR" 2>/dev/null

log() { mkdir -p "$STATE_DIR"; echo "$*"; echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
saved_mode() { case "$(cat "$MODE_FILE" 2>/dev/null)" in off|left|right|dual) cat "$MODE_FILE" ;; *) echo off ;; esac; }
saved_gate() { case "$(cat "$GATE_FILE" 2>/dev/null)" in on|off) cat "$GATE_FILE" ;; *) echo on ;; esac; }
clients() { dumpsys "$SERVICE" 2>/dev/null | grep -E 'There is [1-9][0-9]* clients'; }
injected() {
    pid=$(pidof "$SERVICE")
    [ -n "$pid" ] || return 1
    # Require an executable mapping (a completed dlopen). Read-only fragments left
    # by an interrupted attempt must not count as injected.
    grep -Eq '^[0-9a-f]+-[0-9a-f]+ r-xp .*libpicoet_hook' "/proc/$pid/maps" 2>/dev/null
}

# Publish the state the service actually sees, so a companion app can auto-detect the mode
# without root. The names are transient (no persist. prefix): a reboot, an uninstall or a
# disabled module always leaves them unset, so a stale "enhanced" is impossible.
publish() {
    mode=$(saved_mode)
    gate=$(saved_gate)
    if [ "$mode" != off ] && injected; then
        resetprop -n picoet.enhance.mode "$mode" 2>/dev/null
        if [ "$gate" = on ]; then
            resetprop -n picoet.enhance.gate on 2>/dev/null
        else
            resetprop -n picoet.enhance.gate off 2>/dev/null
        fi
    else
        resetprop -n picoet.enhance.mode off 2>/dev/null
        resetprop -n picoet.enhance.gate off 2>/dev/null
    fi
}

verify_artifacts() {
    [ -x "$BIN" ] || die "injector missing or not executable: $BIN"
    [ -f "$LIB" ] || die "payload missing: $LIB"
    if [ -n "$INJECT_SHA" ] && [ "$(sha "$BIN")" != "$INJECT_SHA" ]; then
        die "injector sha256 mismatch ($BIN)"
    fi
    if [ -n "$PAYLOAD_SHA" ] && [ "$(sha "$LIB")" != "$PAYLOAD_SHA" ]; then
        die "payload sha256 mismatch ($LIB)"
    fi
}

# Inject into the currently running service (no restart). Used at boot and by the watchdog.
inject_now() {
    [ "$(saved_mode)" = off ] && return 0
    verify_artifacts
    pid=$(pidof "$SERVICE")
    [ -n "$pid" ] || die "service not running"
    if injected; then
        log "ensure: already injected (pid $pid)"
        return 0
    fi
    # Right after a restart the service is still starting up: the injector refuses
    # (exit 3) instead of hijacking a thread that is inside the linker. A stale
    # partial mapping from an interrupted attempt (exit 4) blocks a new dlopen, so
    # restart the service once and continue on a clean process.
    n=0
    rc=1
    recovered=0
    while [ $n -lt 20 ]; do
        "$BIN" "$pid" "$LIB" >> "$STATE_DIR/inject.log" 2>&1
        rc=$?
        if [ $rc -eq 0 ] && injected; then
            log "injected mode=$(saved_mode) gate=$(saved_gate) pid=$pid"
            return 0
        fi
        n=$((n + 1))
        if [ $rc -eq 4 ] && [ $recovered -eq 0 ]; then
            recovered=1
            log "stale partial mapping (pid $pid); restarting service and retrying"
            prev=$pid
            setprop ctl.restart "$SERVICE" || die "cannot restart service"
            m=0
            while [ $m -lt 20 ]; do
                pid=$(pidof "$SERVICE")
                [ -n "$pid" ] && [ "$pid" != "$prev" ] && break
                sleep 0.5
                m=$((m + 1))
            done
            [ -n "$pid" ] && [ "$pid" != "$prev" ] || die "service did not restart (old pid ${prev:-none})"
            sleep 2
        elif [ $rc -eq 3 ]; then
            sleep 1
        else
            break
        fi
    done
    die "injection failed (pid $pid rc=$rc)"
}

# Restart the service (unloads any previous hook) and apply the saved mode.
restart_and_inject() {
    old=$(pidof "$SERVICE")
    if [ -n "$old" ]; then
        setprop ctl.restart "$SERVICE" || die "cannot restart service"
    else
        setprop ctl.start "$SERVICE" || die "cannot start service"
    fi
    i=0
    new=
    while [ $i -lt 20 ]; do
        new=$(pidof "$SERVICE")
        [ -n "$new" ] && [ "$new" != "$old" ] && break
        sleep 0.5
        i=$((i + 1))
    done
    [ -n "$new" ] && [ "$new" != "$old" ] || die "service did not restart (old pid ${old:-none})"
    if [ "$(saved_mode)" = off ]; then
        log "switched off: service pid ${old:-none} -> $new, no injection"
    else
        inject_now
    fi
}

[ "$(id -u)" = 0 ] || die "must run as root"

MODE=$1
case "$MODE" in
    status)
        pid=$(pidof "$SERVICE")
        echo "saved_mode=$(saved_mode) saved_gate=$(saved_gate) injected=$(injected && echo yes || echo no) service_pid=${pid:-none} externalfunc=$(getprop ro.pxr.externalfunc)"
        if [ -f "$PLOG" ]; then
            echo "log_tail:"
            tail -n 5 "$PLOG"
        fi
        clients
        exit 0 ;;
    ensure)
        inject_now
        publish
        exit 0 ;;
    off|left|right|dual)
        FORCE=0
        for a in "$@"; do
            [ "$a" = --force ] && FORCE=1
        done
        if [ "$FORCE" = 0 ] && [ -n "$(clients)" ]; then
            die "tracking clients connected, restart would drop them; rerun with --force: $(clients | tr '\n' ' ')"
        fi
        echo "$MODE" > "$MODE_FILE" || die "cannot write $MODE_FILE"
        [ -f "$GATE_FILE" ] || echo on > "$GATE_FILE"
        restart_and_inject
        publish
        log "mode=$MODE gate=$(saved_gate) service pid ${old:-none} -> ${new:-none} injected=$(injected && echo yes || echo no)"
        exit 0 ;;
    gate)
        case "$2" in on|off) ;; *) echo "usage: $0 status | off|left|right|dual [--force] | gate on|off [--force] | ensure"; exit 2 ;; esac
        FORCE=0
        for a in "$@"; do
            [ "$a" = --force ] && FORCE=1
        done
        if [ "$FORCE" = 0 ] && [ -n "$(clients)" ]; then
            die "tracking clients connected, restart would drop them; rerun with --force: $(clients | tr '\n' ' ')"
        fi
        echo "$2" > "$GATE_FILE" || die "cannot write $GATE_FILE"
        restart_and_inject
        publish
        log "gate=$2 mode=$(saved_mode) service pid ${old:-none} -> ${new:-none} injected=$(injected && echo yes || echo no)"
        exit 0 ;;
    *)
        echo "usage: $0 status | off|left|right|dual [--force] | gate on|off [--force] | ensure"
        exit 2 ;;
esac
