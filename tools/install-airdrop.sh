#!/usr/bin/env bash
# Install the OWL radio package and pinned receiver, without a second bar UI.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
. "$root/pins"
mode=${1:-shared}
case "$mode" in shared|standalone|dualchan) ;; *) echo 'Usage: install-airdrop.sh [shared|standalone|dualchan]' >&2; exit 2 ;; esac
command -v omdrop >/dev/null && { echo '[ERROR] An omdrop installation already exists; update it separately.' >&2; exit 1; }
destination="${XDG_DATA_HOME:-$HOME/.local/share}/omadrop/airdrop"
[[ ! -e "$destination" ]] || { echo "[ERROR] Receiver already exists at $destination" >&2; exit 1; }
if [[ "$mode" != shared ]]; then
  for file in owl-profiles allow-untested; do
    [[ ! -e "/etc/omdrop/$file" ]] || { echo "[ERROR] /etc/omdrop/$file already exists; preserve and configure it manually." >&2; exit 1; }
  done
  station=$(iw dev | awk '/Interface/ {iface=$2} /type managed/ {print iface; exit}')
  [[ -n "$station" ]] || { echo '[ERROR] No idle managed Wi-Fi interface.' >&2; exit 1; }
  if [[ "$mode" = standalone ]] && iw dev "$station" link | rg -q 'Connected to'; then
    echo '[ERROR] Standalone mode requires disconnected Wi-Fi; Ethernet can stay connected.' >&2
    exit 1
  fi
  driver=$(basename "$(readlink -f "/sys/class/net/$station/device/driver")")
fi
omarchy pkg add libev libpcap libnl openssl cmake gcc git python-libarchive-c python-pillow python-zeroconf python-ifaddr python-dbus python-gobject keyutils bluez iw polkit
(cd "$root" && makepkg --force --nodeps --noconfirm)
sudo pacman -U --noconfirm "$root/omdrop-owl-0.1.0-1-$(uname -m).pkg.tar.zst"
work=$(mktemp -d /tmp/omadrop-airdrop-install.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
git clone -q "$OMDROP_RECEIVER_REPO" "$work/receiver"
git -C "$work/receiver" checkout -q --detach "$OMDROP_RECEIVER_COMMIT"
mkdir -p "$destination"
cp -a "$work/receiver/bin" "$work/receiver/share" "$work/receiver/manifest.json" "$work/receiver/LICENSE" "$work/receiver/README.md" "$destination/"
patch -d "$destination" -p1 < "$root/patches/receiver-consent.patch"
patch -d "$destination" -p1 < "$root/patches/receiver-progress.patch"
patch -d "$destination" -p1 < "$root/patches/receiver-clipboard.patch"
patch -d "$destination" -p1 < "$root/patches/receiver-links.patch"
install -m644 "$root/scripts/airdrop_consent.py" "$destination/bin/airdrop_consent.py"
if [[ "$mode" != shared ]]; then
  sudo install -d -m755 /etc/omdrop
  printf '%s untested %s station no no Explicit experimental radio mode; Apple interoperability must be checked.\n' "$driver" "$mode" > "$work/owl-profiles"
  printf '1\n' > "$work/allow-untested"
  sudo install -m644 "$work/owl-profiles" "$work/allow-untested" /etc/omdrop/
fi
"$destination/bin/omdrop" setup
if sudo ufw status | rg -q '^Status: active'; then
  sudo ufw allow in on awdl0 from fe80::/10 to any port 8771 proto tcp comment 'OmaDrop AirDrop AWDL receiver'
fi
echo '[SUCCESS] AirDrop installed. Restart sharing with: omadrop stop; omadrop start'
