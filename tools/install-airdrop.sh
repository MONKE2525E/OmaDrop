#!/usr/bin/env bash
# Install Owly and the consent-enabled receiver used by the OmaDrop panel.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
. "$root/pins"
if [[ $# -gt 0 ]]; then
  echo '[ERROR] Radio modes are configured in /etc/owly/drivers.toml. Run omadrop triage to check your card; legacy dualchan/standalone profiles are unsupported.' >&2
  exit 2
fi
destination="${XDG_DATA_HOME:-$HOME/.local/share}/omadrop/airdrop"
existing=$(command -v omdrop || true)
if [[ -n "$existing" && "$(readlink -f "$existing")" != "$destination/bin/omdrop" ]]; then
  echo '[ERROR] A separate omdrop installation exists; preserve it and update it separately.' >&2
  exit 1
fi
if [[ -e "$destination" ]]; then
  echo '[ERROR] The receiver is already installed. Update Owly with just install; run omadrop triage for migration checks.' >&2
  exit 1
fi
omarchy pkg add rust git libpcap libnl openssl hostapd python-libarchive-c python-pillow python-zeroconf python-ifaddr python-dbus python-gobject keyutils bluez iw polkit
cargo build --manifest-path "$root/Cargo.toml" --release --locked -p owlyd -p owly-cli -p omdrop-owl
sudo "$root/packaging/install.sh" owly
sudo "$root/packaging/install.sh" omdrop-owl
sudo systemctl daemon-reload
sudo busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig
sudo nmcli general reload conf
work=$(mktemp -d "${TMPDIR:-/tmp}/omadrop-airdrop-install.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
git clone -q "$OMDROP_RECEIVER_REPO" "$work/receiver"
git -C "$work/receiver" checkout -q --detach "$OMDROP_RECEIVER_COMMIT"
for patch in receiver-consent receiver-progress receiver-clipboard receiver-links; do
  patch -d "$work/receiver" -p1 < "$root/patches/$patch.patch"
done
install -m644 "$root/scripts/airdrop_consent.py" "$work/receiver/bin/airdrop_consent.py"
mkdir -p "$destination"
cp -a "$work/receiver/bin" "$work/receiver/share" "$work/receiver/manifest.json" "$work/receiver/LICENSE" "$work/receiver/README.md" "$destination/"
"$destination/bin/omdrop" setup
echo '[SUCCESS] AirDrop installed with Owly. Run omadrop triage before trying an untested card.'
echo '[INFO] Existing /etc/omdrop profiles are preserved; Owly uses /etc/owly. Firewall rules were not changed.'
