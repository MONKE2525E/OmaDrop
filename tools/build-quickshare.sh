#!/usr/bin/env bash
# Build the pinned RQuickShare bridge without the upstream Tauri window.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v cargo >/dev/null || { echo '[ERROR] Install Rust/cargo first' >&2; exit 1; }
if ! command -v protoc >/dev/null && [[ ! -x "${PROTOC:-}" ]]; then
  echo '[ERROR] Install protobuf (protoc), or set PROTOC to its executable' >&2
  exit 1
fi
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$root/build/quickshare-target}"
cargo build --locked --release --manifest-path "$root/quickshare/Cargo.toml"
install -Dm755 "$CARGO_TARGET_DIR/release/omadrop-quickshare" "$root/build/omadrop-quickshare"
echo '[SUCCESS] Built build/omadrop-quickshare'
