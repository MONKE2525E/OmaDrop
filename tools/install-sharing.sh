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
if [[ -d "$destination" ]]; then
  backup="${XDG_STATE_HOME:-$HOME/.local/state}/omadrop/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup"
  cp -a "$destination" "$backup/plugin"
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
# Quick Share must keep a reachable port across visibility windows/restarts.
# The protocol engine is absent when visibility is off; permit LAN clients only.
if command -v ufw >/dev/null && sudo ufw status | rg -q '^Status: active'; then
  for network in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    sudo ufw allow from "$network" to any port 53317 proto tcp comment 'OmaDrop LocalSend LAN'
    sudo ufw allow from "$network" to any port 53317 proto udp comment 'OmaDrop LocalSend LAN'
    sudo ufw allow from "$network" to any port 53318 proto tcp comment 'OmaDrop Quick Share LAN'
  done
fi
# The existing on-demand daemon must exit before this service can take its lock.
# On an upgrade, start the service on the next login rather than stopping transfers.
if ! pgrep -f "^$environment/bin/python $destination/bin/omadrop.py daemon$" >/dev/null; then
  systemctl --user start omadrop.service
fi
OMARCHY_SHELL_IPC_TIMEOUT=15s omarchy-shell shell rescanPlugins
omarchy bar put monke.omadrop --section right
echo '[SUCCESS] Installed OmaDrop in the Omarchy bar'
