#!/usr/bin/env bash
# Opens the real popup (ui/OmaDropPanel.qml + KeyboardPanel) under a stand-in
# bar, backed by fake-omadrop.py, runs scripted steps and quits. Test use only.
#
# Usage: panel-real.sh <fixture.json> <screen> "<steps>" [max-height-px]
#
# Steps and the OMADROP_* switches are documented in panel-real.qml.
# OMADROP_UI=<dir> tests another copy of ui/. OMADROP_FAKE_* variables drive
# the fake backend. OMADROP_RAW_LOG=<file> keeps the whole quickshell output,
# warnings included; stdout shows only the lines below.
# Prints the harness log and, when done, the backend commands the panel sent.
# The popup is a layer-shell overlay on the named output and no window takes
# focus; the fake backend never touches the live service.
#
# KeyboardPanel is taken from a test-only copy (isolate-dismiss.py) in the run's
# temporary directory, so pointer presses elsewhere on the desktop cannot close a
# run. The packaged shell files are only read.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ui=${OMADROP_UI:-$here/../ui}
shell=${OMARCHY_SHELL_DIR:-/usr/share/omarchy/shell}
fixture=$(realpath "$1")
cfg=$(mktemp -d "${TMPDIR:-/tmp}/omadrop-real.XXXXXX")
fake=$cfg/fake; mkdir "$fake"
trap 'rm -rf "$cfg"' EXIT
mkdir "$cfg/bin" "$cfg/ui" "$cfg/Ui"
ln -s "$shell/Commons" "$cfg/Commons"
for f in "$shell"/Ui/*; do ln -s "$f" "$cfg/Ui/"; done
rm "$cfg/Ui/KeyboardPanel.qml"
python3 -I "$here/isolate-dismiss.py" "$shell/Ui/KeyboardPanel.qml" "$cfg/Ui/KeyboardPanel.qml"
ln -s "$(realpath "$ui")/OmaDropPanel.qml" "$cfg/ui/OmaDropPanel.qml"
ln -s "$(realpath "$ui")/OmaDropView.qml" "$cfg/ui/OmaDropView.qml"
ln -s "$(realpath "$ui")/OmaDropMark.qml" "$cfg/ui/OmaDropMark.qml"
cp "$here/panel-real.qml" "$cfg/shell.qml"
cp "$fixture" "$fake/state.json"
printf '#!/usr/bin/env bash\nexec python3 -I "%s" "$@"\n' "$here/fake-omadrop.py" > "$cfg/bin/omadrop"
chmod +x "$cfg/bin/omadrop"
export OMADROP_FAKE_DIR=$fake OMADROP_SCREEN=$2 OMADROP_STEPS=$3 OMADROP_MAXH=${4:-} OMADROP_SHIFT=${OMADROP_SHIFT:-0}
timeout 90 quickshell -p "$cfg" 2>&1 | tee -a "${OMADROP_RAW_LOG:-/dev/null}" | grep -E "PANEL|STEP|ERROR|error|WARN|Unable|TypeError|ReferenceError" || true
echo "--- backend argv"; cat "$fake/argv.log" 2>/dev/null | sed -e 's/^\(status\)$/\1/' | sort | uniq -c
[ -f "$fake/stdin.log" ] && { echo "--- stage-text stdin"; cat "$fake/stdin.log"; echo; } || true
