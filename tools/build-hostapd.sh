#!/usr/bin/env bash
# Build the fork's Wi-Fi Direct helper without changing the running radio.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=2.11
checksum=2b3facb632fd4f65e32f4bf82a76b4b72c501f995a4f62e330219fe7aed1747a
archive="$root/upstream/hostapd-$version.tar.gz"
source="$root/upstream/hostapd-$version"
mkdir -p "$root/upstream"
if [[ ! -f "$archive" ]]; then
  temporary=$(mktemp "$root/upstream/hostapd-download.XXXXXX")
  trap 'rm -f "$temporary"' EXIT
  curl --fail --location --retry 2 --max-time 90 \
    "https://w1.fi/releases/hostapd-$version.tar.gz" -o "$temporary"
  printf '%s  %s\n' "$checksum" "$temporary" | sha256sum --check --status
  mv "$temporary" "$archive"
  trap - EXIT
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
if [[ ! -f "$source/.omadrop-patched" ]]; then
  # This directory contains generated build sources owned by this tool.
  rm -rf "$source"
  tar -xzf "$archive" -C "$root/upstream"
  patch -d "$source" -p1 < "$root/patches/hostapd-p2p-go.patch"
  cp "$source/hostapd/defconfig" "$source/hostapd/.config"
  sed -i 's/^#CONFIG_LIBNL32=y/CONFIG_LIBNL32=y/' "$source/hostapd/.config"
  touch "$source/.omadrop-patched"
fi
make -C "$source/hostapd" -j"${OMADROP_BUILD_JOBS:-2}"
echo "[SUCCESS] Built $source/hostapd/hostapd"
