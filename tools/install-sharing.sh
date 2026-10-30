#!/usr/bin/env bash
# Install only the universal panel and unprivileged sharing coordinator.
# The existing AirDrop/AWDL radio package keeps its own installation path.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
destination="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/monke.omadrop"
binary="${OMADROP_QUICKSHARE_BIN:-$root/build/omadrop-quickshare}"
environment="${XDG_DATA_HOME:-$HOME/.local/share}/omadrop/venv"
if [[ -e "$HOME/.local/bin/omadrop" && ! -L "$HOME/.local/bin/omadrop" ]]; then
  echo '[ERROR] ~/.local/bin/omadrop already exists and is not a symlink' >&2
  exit 1
fi
[[ -x "$binary" ]] || { echo '[ERROR] Run tools/build-quickshare.sh first' >&2; exit 1; }
command -v uv >/dev/null || { echo '[ERROR] Install uv first' >&2; exit 1; }
command -v zenity >/dev/null || { echo '[ERROR] Install zenity for the file picker' >&2; exit 1; }
# Update only OmaDrop's managed receiver, and stage the patch before changing
# the installed copy. An already-running receiver picks it up on restart.
receiver="${XDG_DATA_HOME:-$HOME/.local/share}/omadrop/airdrop"
receiver_stage=""
if [[ -d "$receiver" ]]; then
  [[ -f "$receiver/bin/airdrop-serve.py" && -f "$receiver/bin/airdrop_consent.py" ]] || {
    echo '[ERROR] Managed AirDrop receiver is incomplete; update it separately.' >&2; exit 1;
  }
  receiver_stage=$(mktemp -d "${TMPDIR:-/tmp}/omadrop-receiver-upgrade.XXXXXX")
  trap 'rm -rf -- "$receiver_stage"' EXIT
  mkdir -p "$receiver_stage/bin"
  cp -a "$receiver/bin/airdrop-serve.py" "$receiver/bin/airdrop_consent.py" "$receiver_stage/bin/"
  if ! grep -Fq "airdrop_consent.report(consent, 'complete', names=written)" "$receiver_stage/bin/airdrop-serve.py"; then
    patch --batch --forward --fuzz=0 -d "$receiver_stage" -p1 < "$root/patches/receiver-action-upgrade-serve.patch" || {
      echo '[ERROR] Managed AirDrop receiver differs from the supported version; update it separately.' >&2; exit 1;
    }
  fi
  if ! grep -Fq 'def report(grant, state="transferring", count=0, names=None):' "$receiver_stage/bin/airdrop_consent.py"; then
    patch --batch --forward --fuzz=0 -d "$receiver_stage" -p1 < "$root/patches/receiver-action-upgrade-consent.patch" || {
      echo '[ERROR] Managed AirDrop consent bridge differs from the supported version; update it separately.' >&2; exit 1;
    }
  fi
fi
if [[ -d "$destination" ]]; then
  backup="${XDG_STATE_HOME:-$HOME/.local/state}/omadrop/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup"
  cp -a "$destination" "$backup/plugin"
fi
if [[ -n "$receiver_stage" ]]; then
  receiver_backup="${XDG_STATE_HOME:-$HOME/.local/state}/omadrop/backups/$(date +%Y%m%d-%H%M%S)/airdrop-bin"
  mkdir -p "$receiver_backup"
  cp -a "$receiver/bin/airdrop-serve.py" "$receiver/bin/airdrop_consent.py" "$receiver_backup/"
  install -m755 "$receiver_stage/bin/airdrop-serve.py" "$receiver/bin/airdrop-serve.py"
  install -m644 "$receiver_stage/bin/airdrop_consent.py" "$receiver/bin/airdrop_consent.py"
  echo '[INFO] Updated managed AirDrop receiver; restart sharing to use the new receive actions.'
fi
mkdir -p "$destination/bin" "$destination/ui"
for file in ui/OmaDropPanel.qml ui/OmaDropView.qml ui/OmaDropMark.qml manifest.json; do
  install -m644 "$root/plugins/monke.omadrop/$file" "$destination/$file"
done
install -m755 "$root/scripts/omadrop" "$root/scripts/omadrop.py" "$destination/bin/"
install -m755 "$binary" "$destination/bin/omadrop-quickshare"
install -Dm644 "$root/quickshare/COPYING" "$destination/licenses/RQuickShare-GPL-3.0"
uv venv --allow-existing "$environment"
uv pip install --require-hashes --python "$environment/bin/python" -r "$root/scripts/requirements.lock"
mkdir -p "$HOME/.local/bin"
ln -sfn "$destination/bin/omadrop" "$HOME/.local/bin/omadrop"
install -Dm644 "$root/scripts/omadrop.service" "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omadrop.service"
systemctl --user daemon-reload
systemctl --user enable omadrop.service
# The existing on-demand daemon must exit before this service can take its lock.
# On an upgrade, start the service on the next login rather than stopping transfers.
if ! pgrep -f "^$environment/bin/python $destination/bin/omadrop.py daemon$" >/dev/null; then
  systemctl --user start omadrop.service
fi
OMARCHY_SHELL_IPC_TIMEOUT=15s omarchy-shell shell rescanPlugins
omarchy bar put monke.omadrop --section right
echo '[SUCCESS] Installed OmaDrop in the Omarchy bar'
echo '[INFO] Firewall rules were not changed. See README.md for LAN-scoped LocalSend and Quick Share rules.'
