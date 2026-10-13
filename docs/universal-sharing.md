# OmaDrop sharing panel

The native Omarchy panel uses one nearby list for AirDrop, Quick Share and
LocalSend. All three backends start together. A failed backend leaves the
others running and shows its reason in the panel. Send stages files, dropped
files or pasted content, then sends once to the selected recipient. Receive
shows incoming requests, approval controls and transfer progress. Unnamed
Apple devices stay hidden until their names resolve.

## Install

Requirements: Omarchy with Quickshell, Python 3.11 or newer, uv, openssl,
zenity, wl-clipboard, xdg-utils, libnotify, util-linux, Rust/cargo, protobuf's
`protoc`, and D-Bus development files. On Arch,
`protobuf` supplies `protoc`. The Quick Share bridge builds the pinned
RQuickShare protocol library, without its Tauri UI.

```sh
tools/build-quickshare.sh
tools/install-sharing.sh
```

The installer adds `monke.omadrop` to the right side of the bar and installs
`~/.local/bin/omadrop`. It backs up an existing plugin before replacing it.
Opening the panel advertises on all three protocols. Closing it releases the
menu lease after active operations finish. **Stay visible for 10 minutes**
opens a bounded window; **Stop early** ends that window. The shell sends a
desktop notification for incoming requests, and clicking it opens Receive.

The install is independent of the radio package described in the root README.
It does not replace the existing `omdrop` CLI or change Wi-Fi drivers.

## Backends

| Backend | Implementation | Requirements and limits |
| --- | --- | --- |
| LocalSend | LocalSend v2.2 HTTPS discovery, consent, upload and cancel | Same LAN, multicast UDP and TCP 53317 permitted; file transfers only |
| Quick Share | Pinned RQuickShare library through a native JSON-lines bridge | Same LAN with mDNS and TCP 53318 permitted; Bluetooth wake when BlueZ permits it; files and shared text/URLs |
| AirDrop | Pinned `omdrop` receiver and OWL AWDL radio backend | `tools/install-airdrop.sh`; shared radio mode is verified on MT7925 |

The installers do not change firewall rules. Review the LAN/interface-scoped
LocalSend, Quick Share, and AWDL examples in the root README before adding rules.

LocalSend uses a persistent self-signed certificate. Outbound HTTPS checks the
certificate against the fingerprint announced by the peer. That is a
consistency check, not an authenticated identity or trusted-sender list.

Incoming LocalSend and Quick Share files wait for explicit **Accept**.
Quick Share also displays the engine's verification code. Compare it on both
devices before accepting. Quick Share receives one approved transfer at a time.
Files arrive in private staging and are published into the selected folder
without overwriting existing files. Invalid names, oversized requests, and
LocalSend checksum mismatches are rejected.

AirDrop retains the installed receiver's contacts policy and also requires
explicit approval in Receive. The receiver's Ask waits up to 30
seconds; rejection, timeout, a stopped coordinator, or a broken approval bridge
refuses the transfer. Upload without an approved Ask is rejected. Approval
is bound to the sender address, certificate when supplied, transfer ID, and
exact file names and sizes. It expires after 60 seconds and is single use.
Link-only AirDrop requires the same approval. Accepted HTTP/HTTPS links open
in the default browser without creating a text document. Rejection opens
nothing, and a link approval cannot authorize a file upload. Receive progress
is reported for files. Quick Share URL payloads also open after approval;
other shared text is saved as a text file.
Outgoing AirDrop transfers do appear; its CLI provides completion rather than
byte progress. When OmaDrop finds an AirDrop receiver already running, it does
not take ownership or stop it, or change its receive folder. The common folder
applies to AirDrop when OmaDrop starts that receiver itself.

For an Ethernet desktop with an idle Wi-Fi card, `tools/install-airdrop.sh
standalone` installs an experimental dedicated-radio profile. The card must
already be disconnected. The helper reserves it from NetworkManager and
restores its prior managed/up state on stop. Channel 6 is the default;
`/etc/omdrop/standalone-channel` may select 6, 44, or 149. A connected Wi-Fi
station is refused rather than disconnected. Reliable unicast needs hardware
acknowledgments. On RTL8852BE, monitor receive, injection, peer synchronization,
and IPv6 replies were observed, but active monitor mode was rejected by the
driver and Mac HTTPS discovery stalled. This does not establish working AirDrop
transfers on RTL8852BE. Shared mode on a compatible AP channel remains a separate
test. Bluetooth wake and plain Ethernet mDNS replies run with the radio window.

`tools/install-airdrop.sh dualchan` uses a Wi-Fi Direct group on an independent
channel instead of borrowing the router's channel, following the
[upstream P2P-GO mechanism](https://github.com/jedbillyb/mt7921-dual-channel).
The packaged hostapd keeps
the P2P-GO interface type; monitor injection uses that interface's MAC address
to select its channel. No router or kernel changes are needed. The default is
149; `/etc/omdrop/airdrop-channel` may select 6, 44, or 149. On RTL8852BE,
regular Wi-Fi stayed connected on channel 161 while AWDL ran on 149. Mac native
AirDrop discovery reached the receiver, and a protocol-client upload from the
Mac produced matching file hashes. Native AirDrop photo and website sharing were confirmed during device testing;
sustained large transfers remain unverified. This experimental mode requires the
`allow-untested` opt-in and can increase Wi-Fi latency while active.

Devices remain separate protocol entries. Names are not reliable identity
proof, and AirDrop uses changing addresses. The panel does not merge unrelated
IDs or claim that a route is fastest. The local machine's hostname is the
advertised name; the upstream Quick Share engine also uses the hostname.

The installer enables `omadrop.service` for the graphical login session.
Start opens a bounded window of up to 10 minutes; Stop ends it immediately.
The deadline is saved, so login resumes only the remaining time and never
renews an expired window. Old unlimited settings do not resume. Menu-open-only
advertising uses `lease --seconds N` (5–120 seconds). Renew it while the menu
or its picker is active. `release` stops sharing unless an explicit window
is still active; a shell crash leaves only the short lease to expire. Leases
are not persisted across restarts. Status reports `mode` as `off`, `menu` or
`window`, with `remainingSeconds` only for an explicit window.

`pick` returns chosen file metadata without sending. `stage-clipboard`
returns local file URLs, or stages image/text bytes in private runtime files.
Clipboard copies are removed after sending or after one hour; original files
are kept. Send targets one selected peer and returns its queued transfer ID
immediately. Transfers report file count, first filename and update time.
Finished, failed, cancelled, and rejected transfers expire three hours after
their terminal state. History is saved privately in `~/.config/omadrop/transfers.json`
and expired rows are removed on restart as well as during use. `dismiss ID` or
`dismiss all` clears only finished transfers and updates that saved history.
Transfers with no state or byte progress for five minutes fail and release
their operation lease once the UI observes that result. Menu visibility can
continue while the menu remains open.
AirDrop readiness is checked every 20 seconds, so connecting Wi-Fi or fixing
its channel updates the error and retries the receiver without restarting the
other protocols. Running the regular LocalSend app at the same time
may occupy port 53317; OmaDrop reports that conflict rather than stopping it.

AirDrop files are saved and announced after approval. Website links open in
the default browser after approval. Arrivals never touch the clipboard, so
an incoming file cannot replace what you copied. The pinned receiver used to copy
every arrival (images as images, links as URLs, other files as their path);
`patches/receiver-clipboard.patch` removes that. To copy one yourself, run
`omdrop act copy FILE`. The installer applies the patch after
`receiver-consent.patch`; an existing receiver install is not changed by
re-running it, so apply the patch to `bin/omdrop` with `patch -p1` from the
receiver directory.

## CLI and diagnostics

```sh
omadrop status
omadrop start --seconds 600
omadrop stop
omadrop send --peer '<id from status>' /path/to/file
omadrop send --peer '<id from status>' --pick
omadrop accept '<incoming id>'
omadrop reject '<incoming id>'
omadrop cancel '<transfer id>'
omadrop directory --pick
omadrop pick
omadrop stage-clipboard
printf 'Shared text' | omadrop stage-text
omadrop dismiss all
```

Every command returns JSON. The coordinator listens on a user-only Unix socket
under `$XDG_RUNTIME_DIR/omadrop`. Settings, transfer history, and TLS files live under
`$XDG_CONFIG_HOME/omadrop`. The daemon log is in the runtime directory.
Runtime device names and tokens are not written into repository files.

Native checks confirmed AirDrop receiving and Android photo approval. Desktop
notifications and browser opening were exercised against the installed receiver.
Quick Share and LocalSend local-engine tests cover consent and transferred bytes;
full native outgoing interoperability across every device remains unverified.

## Verification

```sh
uv venv build/test-venv
uv pip install --python build/test-venv/bin/python -r scripts/requirements.lock
OMADROP_QUICKSHARE_BIN="$PWD/build/omadrop-quickshare" \
  build/test-venv/bin/python -m unittest discover -s tests -v
cargo fmt --manifest-path quickshare/Cargo.toml --check
cargo clippy --locked --manifest-path quickshare/Cargo.toml -- -D warnings
```

The protocol tests use real local HTTPS and Quick Share engines. They check
consent, transferred bytes, cancellation, bad tokens, invalid paths, checksums,
duplicate-name preservation, and independent backend failures. Local engine
tests do not establish interoperability with Android or Apple devices. Native
UI fixtures live under `plugins/monke.omadrop/test-harness`; the installer
does not copy those fixtures into the live plugin.
