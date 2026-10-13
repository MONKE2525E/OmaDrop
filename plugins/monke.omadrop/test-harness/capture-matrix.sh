#!/usr/bin/env bash
# Captures the real popup (ui/OmaDropPanel.qml under KeyboardPanel) through
# panel-real.sh in one fixed state matrix, so a before run and an after run
# differ only in ui/. Test use only.
# Usage: capture-matrix.sh <out-dir>
# OMADROP_SCREEN picks the output (default DP-1). ONLY=<name> runs a single state.
# Writes <name>.log per run (harness log), raw/<name>.txt (whole quickshell output,
# warnings included), notes.txt (closed attempts), focus.txt (active window before
# and after each run) and summary.txt (attempts and validity per run).
# A run is valid only when the popup was still open at the end ("PANEL done
# opened=true"). The frames of a closed attempt are erased before the next try.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?usage: capture-matrix.sh <out-dir>}
screen=${OMADROP_SCREEN:-DP-1}
fixtures=$here/fixtures
mkdir -p "$out/raw"
export TMPDIR=${TMPDIR:-/tmp}

focus() {
  hyprctl -j activewindow 2>/dev/null | python3 -I -c '
import json, sys
try:
    w = json.load(sys.stdin)
    print(w.get("address"), (w.get("workspace") or {}).get("id"), w.get("monitor"))
except ValueError:
    print("none")' || echo none
}

# run <name> <fixture> <max-height-px or ""> <steps> [VAR=value ...]
run() {
  local name=$1 fixture=$2 maxh=$3 steps=$4
  shift 4
  local before after attempt valid=no
  if [ -n "${ONLY:-}" ] && [ "$ONLY" != "$name" ]; then return 0; fi
  before=$(focus)
  for attempt in 1 2 3 4; do
    : > "$out/raw/$name.txt"
    env "$@" OMADROP_RAW_LOG="$out/raw/$name.txt" "$here/panel-real.sh" "$fixtures/$fixture" "$screen" "$steps" "$maxh" > "$out/$name.log" 2>&1 \
      || echo "$name: harness exit $?" >> "$out/notes.txt"
    if grep -q "PANEL done opened=true" "$out/$name.log"; then valid=yes; break; fi
    echo "$name: popup closed during attempt $attempt; its frames erased" >> "$out/notes.txt"
    rm -f "$out/$name"*.png
  done
  after=$(focus)
  echo "$name: focus before=[$before] after=[$after]" >> "$out/focus.txt"
  echo "$name attempts=$attempt valid=$valid" >> "$out/summary.txt"
}

# Send tab: the states the person sees first.
run send-empty send-empty.json "" "wait:900;audit;buttons;shot:$out/send-empty.png"
run send-active send-active.json "" "wait:900;audit;buttons;shot:$out/send-active.png"
run send-preparing send-preparing.json "" "wait:900;audit;buttons;shot:$out/send-preparing.png"
run send-errors send-errors.json "" "wait:900;audit;buttons;shot:$out/send-errors.png"
run send-unavailable all-unavailable.json "" "wait:900;audit;buttons;shot:$out/send-unavailable.png"
run send-many send-many.json "" "wait:900;audit;buttons;shot:$out/send-many.png"

# Share list: a file count past the five shown.
run staged-many send-empty.json "" "wait:900;stage:/a/f1.txt|/a/f2.txt|/a/f3.txt|/a/f4.txt|/a/f5.txt|/a/f6.txt|/a/f7.txt;wait:900;audit;buttons;shot:$out/staged-many-collapsed.png;click:Show all 7;wait:600;shot:$out/staged-many-expanded.png;click:×#6;wait:600;dump;buttons;shot:$out/staged-many-removed.png"

# Clear: the row leaves only after the backend acknowledges it; a refusal keeps it.
run clear-ok send-active.json "" "wait:900;click:Clear#0;wait:400;dump;buttons;shot:$out/clear-ok-pending.png;wait:2600;dump;buttons;shot:$out/clear-ok.png" OMADROP_FAKE_DISMISS_DELAY=2
run clear-fail send-active.json "" "wait:900;click:Clear#0;wait:1500;dump;buttons;shot:$out/clear-fail.png" OMADROP_FAKE_DISMISS_FAIL="Could not forget this transfer."

# Receive tab: requests, live and finished transfers, starting and history.
run receive-empty receive-off.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-empty.png"
run receive-incoming receive-incoming.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-incoming.png"
run receive-window receive-window.json "" "tab:1;wait:900;audit;shot:$out/receive-window.png"
run receive-kinds receive-kinds.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-kinds.png"
run receive-one-unavailable receive-one-unavailable.json "" "tab:1;wait:900;audit;shot:$out/receive-one-unavailable.png"
run receive-expired receive-expired.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-expired.png"
run receive-history receive-history.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-history.png"
run receive-starting startup-stuck.json "" "tab:1;wait:900;audit;buttons;shot:$out/receive-starting.png" OMADROP_FAKE_LEASE_HANG=20

# Startup: a lease that fails at once, and the stuck timeline in shortened bounds.
# The default bounds are 65 s and 70 s; the harness shortens them to 8 s and 12 s.
# The first lease hangs past 12 s: starting, a stall at 8 s with Retry hidden, a
# timeout at 12 s with Retry shown, and a Retry that answers before the 20 s renewal.
run startup-failed startup-stuck.json "" "wait:900;shot:$out/startup-failed-early.png;wait:2500;audit;buttons;shot:$out/startup-failed.png" OMADROP_FAKE_LEASE_FAIL="Port 53317 is already in use by another program."
run startup-stuck startup-stuck.json "" "wait:900;shot:$out/startup-stuck-early.png;wait:7000;shot:$out/startup-stuck-stalled.png;buttons;wait:3500;audit;buttons;shot:$out/startup-stuck-timeout.png;click:Retry;wait:1500;buttons;shot:$out/startup-stuck-retry.png" OMADROP_STARTUP_SECONDS=8 OMADROP_LEASE_SECONDS=12 OMADROP_FAKE_LEASE_HANG=30

# Short screen (300 px body): overflow, scrolling and keyboard reach.
run short-send-many send-many.json 300 "wait:900;audit;buttons;shot:$out/short-send-many.png;scroll:260;wait:500;shot:$out/short-send-many-scrolled.png"
run short-focus send-many.json 300 "wait:900;focusnext:8;wait:400;focusreport;shot:$out/short-focus-8.png;focusall;wait:400;audit;shot:$out/short-focus-end.png"
run short-focus-peers send-many.json 300 "wait:900;stage:/a/f1.txt;wait:500;focusall;wait:400;audit;shot:$out/short-focus-peers-end.png"
run short-receive-kinds receive-kinds.json 300 "tab:1;wait:900;focusall;wait:400;audit;shot:$out/short-receive-kinds.png"

echo "done: $(ls "$out"/*.png 2>/dev/null | wc -l) screenshots in $out"
