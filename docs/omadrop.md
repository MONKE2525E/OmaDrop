<p align="center"><img src="../assets/omadrop.svg" width="80" height="80" alt="OmaDrop icon"></p>

# OmaDrop

OmaDrop is Monke's nearby file-sharing panel for [Omarchy](https://omarchy.org/).
Send files from the bar, approve incoming transfers, and see nearby devices in
one place. It supports LocalSend over the LAN, Quick Share through RQuickShare,
and AirDrop through the Owly radio backend when the Wi-Fi hardware supports it.

This fork builds on [t4t5/owly](https://github.com/t4t5/owly), formerly omdrop-owl.
The OmaDrop panel, coordinator, and icon are maintained here. The radio package
can also be used independently with the original omdrop plugin.

## Features

- One Send/Receive panel with file picking, drag and drop, clipboard staging,
  incoming approval, cancellation, progress, and a configurable receive folder.
- LocalSend HTTPS transfers with discovery on each eligible LAN interface and
  a bounded subnet scan when multicast discovery misses a device.
- Quick Share files and text through a pinned native RQuickShare bridge.
- Optional AirDrop with upstream's shared, exclusive, and active-monitor modes.
- Visibility while the panel is open, or a saved window of up to ten minutes.
  One unavailable backend leaves the others usable.
- Finished, failed, and cancelled transfers clear after three hours, including
  history restored after a restart. The Clear button removes them sooner.
- Omarchy theme colors and fonts, with the original OmaDrop bar icon.

LocalSend requires the receiving device to run LocalSend. Quick Share and
AirDrop have their own hardware and protocol requirements; a visible device
name is not proof of the sender's identity.

## Install on Omarchy

Use a recent Omarchy release with the Quickshell shell/plugin system. Install
the build and desktop helpers, then clone this fork:

```sh
omarchy pkg add git rust protobuf dbus python uv openssl zenity \
  wl-clipboard xdg-utils libnotify util-linux

git clone https://github.com/MONKE2525E/OmaDrop.git
cd OmaDrop
tools/build-quickshare.sh
tools/install-sharing.sh
```

The coordinator needs Python 3.11 or newer. The installer creates a private
Python environment, installs the plugin in
`~/.config/omarchy/plugins/monke.omadrop`, adds its icon to the right side of
the bar, and enables the user service `omadrop.service`. It backs up an existing
plugin before replacing it. Updates use the same build/install commands.
An already-running coordinator is left to restart at the next login so that
installation does not interrupt a transfer.

The installer does not change firewall rules. Review the examples below if
discovery or receiving fails. LocalSend and Quick Share do not need Owly or a
Wi-Fi driver change.

### AirDrop with Owly

This checkout includes Owly 0.4.0, the Rust successor to omdrop-owl. Install the
radio daemon and compatibility helper, plus OmaDrop's consent-enabled receiver:

```sh
tools/install-airdrop.sh
```

If the receiver is already installed, update only the radio from this checkout:

```sh
just install
```

Owly uses `/etc/owly/radio.toml` and `/etc/owly/drivers.toml`. It does not read old
`/etc/omdrop/owl-profiles` or `allow-untested` files. The fork's former `dualchan`
and `standalone` modes have been removed. Preserve those files until triage
confirms which backend is installed. No automatic migration changes the radio.

Shared mode keeps Wi-Fi connected and requires the AP on channel 6, 44, or 149.
Exclusive and active-monitor modes disconnect Wi-Fi during the AirDrop window.
Check [upstream's hardware reports](../README.md#supported-hardware) before
opting in to an untested adapter. LocalSend and Quick Share do not require Owly.

### Diagnose AirDrop with an agent

```sh
omadrop triage
omadrop triage --agent codex --problem 'AirDrop unavailable: dualchan unsupported'
omadrop triage --agent claude
omadrop triage --collect-only
```

The command saves hardware IDs, backend versions, radio capabilities, channel,
service state, and selected configuration fields in a private session folder.
It then launches your installed Claude Code or Codex with a playbook and normal
permission prompts. With both installed, it asks which to use. `--model` passes
an explicit model through. Collection requires Linux and Python 3.11 or newer,
and does not require the sharing service, Python packages, or administrator rights.

Sessions live under `$XDG_STATE_HOME/omadrop/triage`, or `~/.local/state/omadrop/triage`.
Use `--output-dir` to choose another parent directory. Missing tools and probe
failures remain in the report. Without an agent CLI, open the generated
`prompt.md` in your coding agent. The helper omits network passwords and
redacts SSIDs, MAC addresses, IP addresses, and the home path from collected
output. Review evidence before sharing it. Collection never uploads anything.

From a local checkout, test the npm entry point without installing OmaDrop:

```sh
npx --package . omadrop-triage --collect-only
```

The same package can run from this development branch on GitHub:

```sh
npx --package github:MONKE2525E/OmaDrop#t3/sync-fork-wifi-triage omadrop-triage
```

The package is not published to the npm registry. This follows
[T3 Code's triage pattern](https://github.com/pingdotgg/t3code/tree/main/.github/triage):
local facts and a playbook passed to your own interactive agent. The agent checks
upstream support, proposes a reversible fix, and asks before disrupting Wi-Fi or
changing privileged settings. A real file transfer each way and Wi-Fi recovery
are required before it can report that the card works.

## Use

1. Open OmaDrop from the bar. Nearby devices appear as their protocols become
   ready. Keep LocalSend open on the phone and connect both devices to the LAN.
2. In Send, choose or drop files, or paste clipboard content. Select a device
   and send. Staging content does not send it immediately.
3. In Receive, inspect the sender and files before accepting. Compare the
   Quick Share verification code on both devices. Rejected requests save nothing.
4. Choose **Stay visible for 10 minutes** to receive with the panel closed.
   **Stop early** ends that window. Closing the panel otherwise releases its
   short visibility lease after active operations finish.
5. Use **Clear** to remove finished history. Files already saved stay in the
   receive folder. OmaDrop does not overwrite an existing filename.

Useful commands return JSON:

```sh
omadrop status
omadrop start --seconds 600
omadrop stop
omadrop directory --pick
omadrop dismiss all
systemctl --user status omadrop.service
journalctl --user -u omadrop.service -n 50
```

Settings, TLS certificates, and transfer history live in `~/.config/omadrop`.
Transient sockets, staged content, and logs
use `$XDG_RUNTIME_DIR/omadrop`. The protocol and installation details are in
[docs/universal-sharing.md](universal-sharing.md).

## Discovery and firewall troubleshooting

LocalSend's default discovery group is **224.0.0.167:53317/UDP**. Registration
and transfers use **53317/TCP**. OmaDrop joins and announces on eligible IPv4
LAN interfaces, excluding common container, VPN, and AWDL interfaces. Announce
bursts and subnet registration help when a phone starts late or the AP drops
multicast. LocalSend v2 uses the `/api/localsend/v2` endpoints; minor protocol
versions are compatible.

If the phone is missing:

- Keep LocalSend open and check that both devices use the same reachable LAN.
  Guest Wi-Fi/client isolation can block discovery and transfers.
- Check `omadrop status` for a LocalSend error. Another LocalSend process may
  already own port 53317: inspect `ss -lntup | rg '53317|53318'`.
- Check addresses with `ip -4 address show up`. Multiple Ethernet/Wi-Fi links
  should be reachable from the phone; Docker or Tailscale routes can complicate
  routing. A custom LocalSend port or multicast group must match on both ends.
- Inspect the firewall with `sudo ufw status verbose` or `sudo nft list ruleset`.
  A subnet scan cannot bypass blocked TCP, AP isolation, or phone battery limits.
- Keep the Android app awake during testing. Disable a VPN temporarily if it
  blocks access to the local network.

For UFW, use the actual trusted subnet and interface, replacing the examples:

```sh
sudo ufw allow in on wlan0 from 192.168.1.0/24 to any port 53317 proto tcp
sudo ufw allow in on wlan0 from 192.168.1.0/24 to 224.0.0.167 port 53317 proto udp
# Quick Share receiving, if used:
sudo ufw allow in on wlan0 from 192.168.1.0/24 to any port 53318 proto tcp
# AirDrop receiving over the separate AWDL link, if used:
sudo ufw allow in on awdl0 from fe80::/10 to any port 8771 proto tcp
```

Quick Share discovery also needs mDNS (5353/UDP). Check the existing mDNS rule
before adding one. With nftables, allow the equivalent traffic in the managed
input chain; do not add an unrelated parallel ruleset or disable the firewall.
The examples above are manual choices, not actions performed by installation.

If the header remains **Starting**, check the channel error and service log.
Fix the reported missing dependency or port conflict, then retry from the panel.
A stalled radio backend should not hold LocalSend startup hostage.

## Development and checks

```sh
tools/build-quickshare.sh
cargo build --release --locked -p owlyd -p owly-cli -p omdrop-owl
uv venv build/test-venv
uv pip install --require-hashes --python build/test-venv/bin/python \
  -r scripts/requirements.lock
OMADROP_QUICKSHARE_BIN="$PWD/build/omadrop-quickshare" \
  build/test-venv/bin/python -m unittest discover -s tests -v
cargo fmt --all --check
cargo fmt --manifest-path quickshare/Cargo.toml --check
cargo clippy --locked --manifest-path quickshare/Cargo.toml -- -D warnings
```

The native UI harness in `plugins/monke.omadrop/test-harness` exercises the real
panel with isolated fixtures. Protocol tests use local HTTPS/Quick Share peers;
physical Android and Apple interoperability still needs device testing.
`makepkg` builds the radio package; the panel is installed separately.

## Credits and licenses

- [t4t5/omdrop-owl](https://github.com/t4t5/omdrop-owl): radio backend, hardware
  profiles, reliability improvements, and OWL patches.
- [omdrop-plugin](https://github.com/brentkearney/omdrop-plugin) and
  [omdrop-awdl](https://github.com/brentkearney/omdrop-awdl): receiver, sender,
  and portable AirDrop helpers.
- [OWL](https://github.com/jedbillyb/owl): userspace AWDL.
- [LocalSend](https://localsend.org/) and its
  [protocol](https://github.com/localsend/protocol): LAN discovery and transfers.
- [RQuickShare](https://github.com/Martichou/RQuickShare): Quick Share engine.

Bundled programs retain their own licenses: Owly MIT, omdrop-awdl
GPL-2.0-only, and the Quick Share bridge GPL-3.0-or-later.
See `quickshare/COPYING` and the radio package's installed license files.
