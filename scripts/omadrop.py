#!/usr/bin/env python3
"""Native sharing coordinator. The control socket is private to this user.

LocalSend v2 over HTTPS, a JSON-lines RQuickShare bridge, and the existing
omdrop CLI operate independently. Never infer advertisement from a PID alone.
"""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import mimetypes
import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp import web

PROTOCOLS = (
    ("airdrop", "AirDrop"),
    ("quickshare", "Quick Share"),
    ("localsend", "LocalSend"),
)
GROUP = "224.0.0.167"
PORT = 53317
QUICK_PORT = 53318
API = "/api/localsend/v2"
ACTIVE_STATES = {"preparing", "waiting", "transferring"}


class RecipientRejected(RuntimeError):
    pass


class LocalSendConnector(aiohttp.TCPConnector):
    """Present our client certificate while retaining aiohttp's peer pin check.

    aiohttp's pinned-fingerprint mode otherwise selects a certificate-less
    context. Android LocalSend requires a client certificate as well.
    """
    client_context = None

    def _get_ssl_context(self, request):
        if request.is_ssl() and isinstance(request.ssl, aiohttp.Fingerprint) and self.client_context:
            return self.client_context
        return super()._get_ssl_context(request)


class Transfer(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setdefault("files", 1)
        self.setdefault("fileName", self.get("name", "Transfer"))
        self["updated"] = time.time()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key != "updated":
            super().__setitem__("updated", time.time())

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        super().__setitem__("updated", time.time())


async def lan_addresses():
    try:
        interfaces = json.loads(await run_command("ip", "-j", "-4", "address", "show", "up", timeout=3))
        addresses = []
        for interface in interfaces:
            name = interface.get("ifname", "")
            if name.startswith(("lo", "tailscale", "docker", "veth", "br-", "virbr", "awdl", "omgo", "mon", "tun", "tap", "wg")):
                continue
            for row in interface.get("addr_info", []):
                address = ipaddress.ip_address(row["local"])
                if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
                    addresses.append(str(address))
        return sorted(set(addresses))[:8]
    except (RuntimeError, TimeoutError, ValueError, KeyError):
        return []


def runtime_dir(create=True):
    base = os.environ.get("OMADROP_RUNTIME_DIR")
    path = (
        Path(base)
        if base
        else Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/omadrop-{os.getuid()}"))
        / "omadrop"
    )
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.is_symlink():
        raise RuntimeError("Unsafe runtime directory")
    if create:
        path.chmod(0o700)
    elif path.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe runtime directory permissions")
    return path


def config_dir():
    return Path(
        os.environ.get(
            "OMADROP_CONFIG_DIR",
            str(
                Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                / "omadrop"
            ),
        )
    )


def read_settings():
    path = config_dir() / "settings.json"
    default = {
        "name": socket.gethostname(),
        "directory": str(Path.home() / "Downloads"),
        "enabled": False,
        "advertiseUntil": 0,
    }
    if path.exists():
        default.update(json.loads(path.read_text()))
    return default


def save_settings(settings):
    base = config_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = base / "settings.tmp"
    temporary.write_text(json.dumps(settings))
    temporary.chmod(0o600)
    temporary.replace(base / "settings.json")


async def run_command(*args, timeout=15, accepted_codes=(0,)):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode not in accepted_codes:
        raise RuntimeError(
            err.decode(errors="replace").strip()[-500:]
            or out.decode(errors="replace").strip()[-500:]
            or f"Command exited {process.returncode}"
        )
    return out.decode(errors="replace")


def shared_urls(urls):
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise ValueError("Invalid shared websites")
    for url in urls:
        if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError("Invalid shared website")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Shared websites must use HTTP or HTTPS")
    if sum(len(url.encode("utf-8")) + 1 for url in urls) > 1024 * 1024:
        raise ValueError("Shared websites exceed the 1 MB limit")
    return urls


async def open_shared_urls(urls):
    urls = shared_urls(urls)
    try:
        async with asyncio.timeout(10):
            for url in urls:
                await run_command("xdg-open", url, timeout=10)
    except (OSError, RuntimeError, TimeoutError) as error:
        raise RuntimeError("Could not open the shared website in your browser") from error


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(c in name for c in ("/", "\\", "\x00"))
        or len(name.encode()) > 240
    ):
        raise ValueError("Invalid file name")
    return name


def peer_address(address):
    ip = ipaddress.ip_address(address)
    if (
        ip.version != 4
        or ip.is_multicast
        or ip.is_unspecified
        or not (ip.is_private or ip.is_link_local)
    ):
        raise ValueError("Only local IPv4 peers are supported")
    return str(ip)


def file_metadata(paths, kind="file"):
    result = []
    for value in paths:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError("Choose readable files")
        result.append({"path": str(path), "name": path.name, "size": path.stat().st_size, "kind": kind})
    if len(result) > 100:
        raise ValueError("Choose at most 100 files")
    return result


def cleanup_sent_files(files):
    try:
        root = runtime_dir(create=False) / "clipboard"
    except (OSError, RuntimeError):
        return
    for value in files:
        path = Path(value)
        if path.parent.parent == root and path.parent.name.startswith("clipboard-"):
            if not path.parent.is_symlink() and not root.is_symlink():
                path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    path.parent.rmdir()


def cleanup_old_clipboard(protected=()):
    try:
        root = runtime_dir(create=False) / "clipboard"
        if root.is_symlink():
            return
        protected_dirs = {Path(path).parent for path in protected}
        for folder in root.glob("clipboard-*"):
            if folder.is_dir() and not folder.is_symlink() and folder not in protected_dirs:
                if folder.stat().st_mtime < time.time() - 3600:
                    shutil.rmtree(folder)
    except (OSError, RuntimeError):
        pass


async def clipboard_read(mime=None, limit=1024 * 1024):
    command = ["wl-paste", "--no-newline"]
    command += ["--type", mime] if mime else ["--list-types"]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    async def read_bounded():
        raw = bytearray()
        while chunk := await process.stdout.read(65536):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("Clipboard content is too large")
        return bytes(raw)

    try:
        raw = await asyncio.wait_for(read_bounded(), 10)
        await asyncio.wait_for(process.wait(), 5)
        return raw if process.returncode == 0 else b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def stage_clipboard():
    if not shutil.which("wl-paste"):
        raise RuntimeError("Clipboard sharing requires wl-clipboard")
    types = (await clipboard_read(limit=16384)).decode(errors="replace").splitlines()
    if "text/uri-list" in types:
        paths = []
        for line in (await clipboard_read("text/uri-list", 65536)).decode().splitlines():
            if not line or line.startswith("#"):
                continue
            url = urlsplit(line)
            if url.scheme == "file" and url.netloc in ("", "localhost"):
                paths.append(unquote(url.path))
        if paths:
            return {"files": file_metadata(paths)}
    mime = "image/png" if "image/png" in types else next(
        (value for value in types if value in ("image/jpeg", "image/webp", "image/gif", "image/bmp")), None
    )
    kind = "image" if mime else "text"
    mime = mime or next((value for value in types if value.startswith("text/plain")), None)
    if not mime:
        return {"files": [], "error": "Clipboard is empty"}
    content = await clipboard_read(mime, 32 * 1024 * 1024 if kind == "image" else 1024 * 1024)
    if not content:
        return {"files": [], "error": "Clipboard is empty"}
    return stage_content(content, mime, kind)


def stage_text(content):
    if len(content) > 1024 * 1024:
        raise ValueError("Text exceeds the 1 MB limit")
    content.decode("utf-8")
    if not content:
        return {"files": [], "error": "Nothing to share"}
    return stage_content(content, "text/plain", "text")


def stage_content(content, mime, kind):
    cleanup_old_clipboard()
    root = runtime_dir() / "clipboard"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe clipboard directory")
    root.chmod(0o700)
    folder = Path(tempfile.mkdtemp(prefix="clipboard-", dir=root))
    extension = mimetypes.guess_extension(mime) if kind == "image" else ".txt"
    path = folder / ("clipboard-" + time.strftime("%H%M%S") + (extension or ".bin"))
    try:
        with path.open("xb") as output:
            path.chmod(0o600)
            output.write(content)
        return {"files": file_metadata([path], kind)}
    except Exception:
        shutil.rmtree(folder)
        raise


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner

    def datagram_received(self, data, addr):
        if len(data) > 16384:
            return
        try:
            packet = json.loads(data)
            peer = self.owner.register_peer(packet, addr[0])
            if peer and packet.get("announce") is True:
                self.owner.spawn(self.owner.register_remote(peer))
        except (ValueError, TypeError, KeyError):
            pass


class Coordinator:
    def __init__(self):
        self.settings = read_settings()
        self.running = False
        self.channels = {
            key: {"id": key, "name": name, "state": "stopped", "reason": ""}
            for key, name in PROTOCOLS
        }
        self.peers = {}
        self.incoming = {}
        self.transfers = {}
        self.sessions = {}
        self.tasks = set()
        self.local_runner = None
        self.udp = None
        self.local_addresses = []
        self.local_scan = None
        self.local_scan_at = 0
        self.quick = None
        self.quick_staging = None
        self.quick_active = None
        self.quick_files = {}
        self.quick_text = {}
        self.quick_publications = {}
        self.airdrop_progress = {}
        self.airdrop_owned = False
        self.airdrop_names = None
        self.window_timer = None
        self.menu_until = 0
        self.outgoing_files = {}
        self.transfer_watch = {}
        self.client = None
        self.error = ""
        self.port = int(os.environ.get("OMADROP_LOCALSEND_PORT", PORT))
        self.fingerprint = ""
        self.lock = asyncio.Lock()
        self.quick_path = (
            os.environ.get("OMADROP_QUICKSHARE_BIN")
            or shutil.which("omadrop-quickshare")
            or str(Path(__file__).with_name("omadrop-quickshare"))
        )

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def spawn_quick_publication(self, key):
        task = self.spawn(self.publish_quick_files(key))
        task.set_name(key)
        self.quick_publications[key] = task
        task.add_done_callback(
            lambda finished, transfer_id=key: self.quick_publication_done(
                transfer_id, finished
            )
        )
        return task

    def quick_publication_done(self, key, task):
        if self.quick_publications.get(key) is task:
            self.quick_publications.pop(key, None)
        transfer = self.transfers.get(key)
        if task.cancelled() and transfer and transfer.get("state") in (
            "cancelled", "rejected"
        ):
            self.cleanup_quick_staging(key)
            if self.quick_active == key:
                self.quick_active = None

    def cleanup_quick_staging(self, key):
        if self.quick_staging is not None:
            for filename in self.quick_files.pop(key, []):
                try:
                    (self.quick_staging / safe_name(filename)).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
        self.quick_text.pop(key, None)

    async def quick_publication_checkpoint(self):
        await asyncio.sleep(0)

    def channel(self, key, state, reason=""):
        if state != "advertising" and not reason:
            reason = "Starting nearby sharing." if state == "starting" else "Open the menu or start a receive window."
        self.channels[key].update(state=state, reason=reason)

    def info(self):
        return {
            "alias": self.settings["name"],
            "version": "2.2",
            "deviceModel": "OmaDrop",
            "deviceType": "desktop",
            "fingerprint": self.fingerprint,
            "port": self.port,
            "protocol": "https",
            "download": False,
        }

    def snapshot(self):
        now = time.monotonic()
        # LocalSend has no departure packet. Expire stale announcements.
        stale = [
            key
            for key, value in self.peers.items()
            if value["protocol"] == "localsend" and now - value["seen"] > 90
        ]
        for key in stale:
            self.peers.pop(key, None)
        peers = [
            {
                k: v
                for k, v in p.items()
                if k in ("id", "name", "protocol", "deviceType")
            }
            for p in self.peers.values()
            if not (p["protocol"] == "airdrop" and p.get("name") in (None, "", "Unnamed Apple device"))
        ]
        return {
            "running": self.running,
            "mode": "window" if self.settings.get("advertiseUntil", 0) > time.time()
            else "menu" if self.menu_until > time.time() else "off",
            "remainingSeconds": max(0, int(self.settings["advertiseUntil"] - time.time()))
            if self.settings.get("advertiseUntil", 0) > time.time() else None,
            "name": self.settings["name"],
            "directory": self.settings["directory"],
            "protocols": list(self.channels.values()),
            "peers": peers,
            "incoming": [v["public"] for v in self.incoming.values()],
            "transfers": list(self.transfers.values())[-50:],
            "error": self.error,
        }

    async def start(self):
        if self.running:
            await self.refresh_airdrop()
            return
        self.running = True
        self.error = ""
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120, connect=5),
            connector=LocalSendConnector(limit=32),
        )
        for key, starter in (
            ("localsend", self.start_local),
            ("quickshare", self.start_quick),
            ("airdrop", self.start_airdrop),
        ):
            self.channel(key, "starting")
            try:
                await starter()
            except Exception as error:
                self.channel(key, "unavailable", str(error)[:240])
        self.spawn(self.maintenance())

    def arm_window(self):
        if self.window_timer is not None:
            self.window_timer.cancel()
        self.window_timer = asyncio.create_task(self.expire_window())

    async def expire_window(self):
        await asyncio.sleep(max(0, max(self.settings.get("advertiseUntil", 0), self.menu_until) - time.time()))
        async with self.lock:
            self.window_timer = None
            if self.settings.get("advertiseUntil", 0) <= time.time():
                self.settings.update(enabled=False, advertiseUntil=0)
                save_settings(self.settings)
            if self.menu_until <= time.time():
                self.menu_until = 0
            if self.menu_until or self.settings.get("advertiseUntil", 0):
                self.arm_window()
            else:
                await self.stop()

    async def restore_window(self):
        if self.settings.get("enabled") is True and self.settings.get("advertiseUntil", 0) > time.time():
            await self.start()
            self.arm_window()
        else:
            self.settings.update(enabled=False, advertiseUntil=0)
            save_settings(self.settings)

    async def stop(self):
        self.running = False
        self.menu_until = 0
        if self.window_timer is not None:
            self.window_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.window_timer
            self.window_timer = None
        for pending in list(self.incoming.values()):
            if pending["decision"] is not None and not pending["decision"].done():
                pending["decision"].set_result(False)
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.quick and self.quick.returncode is None:
            self.quick.terminate()
            try:
                await asyncio.wait_for(self.quick.wait(), 8)
            except TimeoutError:
                self.quick.kill()
                await self.quick.wait()
        self.quick = None
        for files in self.outgoing_files.values():
            cleanup_sent_files(files)
        self.outgoing_files.clear()
        if self.quick_staging:
            shutil.rmtree(self.quick_staging, ignore_errors=True)
            self.quick_staging = None
        self.quick_active = None
        self.quick_files.clear()
        self.quick_text.clear()
        self.airdrop_progress.clear()
        if self.udp:
            self.udp.close()
            self.udp = None
        if self.local_runner:
            await self.local_runner.cleanup()
            self.local_runner = None
        if self.client:
            await self.client.close()
            self.client = None
        if self.airdrop_owned:
            with contextlib.suppress(Exception):
                await run_command("omdrop", "off", timeout=20)
            self.airdrop_owned = False
        self.peers.clear()
        self.incoming.clear()
        self.sessions.clear()
        for transfer in self.transfers.values():
            if transfer["state"] in ACTIVE_STATES:
                transfer.update(state="cancelled")
        for key, _ in PROTOCOLS:
            self.channel(key, "stopped")

    async def start_local(self):
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cert, key = directory / "certificate.pem", directory / "private-key.pem"
        if not cert.exists() or not key.exists():
            await run_command(
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "3650",
                "-subj",
                "/CN=OmaDrop",
                timeout=30,
            )
            key.chmod(0o600)
        self.fingerprint = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(cert.read_text())
        ).hexdigest()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.local_client_tls = ssl._create_unverified_context()
        self.local_client_tls.load_cert_chain(cert, key)
        self.client.connector.client_context = self.local_client_tls
        app = web.Application(client_max_size=1024 * 1024)
        app.add_routes(
            [
                web.get(API + "/info", self.local_info),
                web.post(API + "/register", self.local_register),
                web.post(API + "/prepare-upload", self.local_prepare),
                web.post(API + "/upload", self.local_upload),
                web.post(API + "/cancel", self.local_cancel),
            ]
        )
        runner = web.AppRunner(app, shutdown_timeout=3, handler_cancellation=True)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", self.port, ssl_context=tls).start()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.port))
            self.local_addresses = await lan_addresses()
            for address in self.local_addresses or ["0.0.0.0"]:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(GROUP) + socket.inet_aton(address))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setblocking(False)
            self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Discovery(self), sock=sock
            )
        except BaseException:
            await runner.cleanup()
            if "sock" in locals():
                sock.close()
            raise
        self.local_runner = runner
        self.channel("localsend", "advertising")
        self.announce()
        self.local_scan_at = time.monotonic()
        self.local_scan = self.spawn(self.discover_local())

    def announce(self, announce=True):
        if self.udp:
            sock = self.udp.get_extra_info("socket")
            for address in self.local_addresses or ["0.0.0.0"]:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                    self.udp.sendto(json.dumps(dict(self.info(), announce=announce)).encode(), (GROUP, self.port))
                except OSError:
                    continue

    async def discover_local(self, hosts=None):
        # LocalSend's HTTP fallback handles access points suppressing multicast.
        # Bound discovery to private, directly connected /24s, never VPNs or WAN.
        if hosts is None:
            hosts = set()
            for address in self.local_addresses:
                network = ipaddress.ip_network(address + "/24", strict=False)
                hosts.update(str(host) for host in network.hosts())
        else:
            hosts = set(hosts)
        hosts.difference_update(self.local_addresses)
        semaphore = asyncio.Semaphore(32)

        async def discover(host):
            async with semaphore:
                for scheme in ("https", "http"):
                    try:
                        # Discovery is unauthenticated by design. Pin the certificate
                        # actually presented here for all subsequent upload requests.
                        if scheme == "https":
                            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                                host, PORT, ssl=self.local_client_tls, server_hostname=host), 1)
                            try:
                                certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
                                fingerprint = hashlib.sha256(certificate).hexdigest()
                            finally:
                                writer.close()
                                with contextlib.suppress(Exception):
                                    await asyncio.wait_for(writer.wait_closed(), 1)
                            tls = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
                        else:
                            tls, fingerprint = None, None
                        async with self.client.post(
                            f"{scheme}://{host}:{PORT}{API}/register", json=self.info(), ssl=tls,
                            timeout=aiohttp.ClientTimeout(total=1),
                        ) as response:
                            if response.status != 200:
                                continue
                            packet = await response.json()
                            packet.update(protocol=scheme, port=PORT)
                            if fingerprint:
                                packet["fingerprint"] = fingerprint
                            self.register_peer(packet, host)
                            return
                    except (OSError, TimeoutError, aiohttp.ClientError, ValueError, TypeError):
                        continue
        await asyncio.gather(*(discover(host) for host in sorted(hosts)[:512]))

    def register_peer(self, packet, address):
        address = peer_address(address)
        fingerprint = str(packet.get("fingerprint", ""))[:128]
        if not fingerprint or fingerprint == self.fingerprint:
            return None
        port = int(packet.get("port", PORT))
        if not 1 <= port <= 65535 or packet.get("protocol") not in ("http", "https"):
            raise ValueError("Invalid peer endpoint")
        peer_id = "localsend:" + fingerprint
        peer = {
            "id": peer_id,
            "name": str(packet.get("alias") or "Unnamed device")[:100],
            "protocol": "localsend",
            "deviceType": str(packet.get("deviceType", "desktop")),
            "host": address,
            "port": port,
            "scheme": packet["protocol"],
            "seen": time.monotonic(),
            "fingerprint": fingerprint,
        }
        if len(self.peers) < 256 or peer_id in self.peers:
            self.peers[peer_id] = peer
            return peer
        return None

    def remote_tls(self, peer):
        # LocalSend uses self-signed TLS certificates with their SHA-256 as ID.
        if peer["scheme"] == "https":
            try:
                fingerprint = bytes.fromhex(peer["fingerprint"])
                if len(fingerprint) == 32:
                    return aiohttp.Fingerprint(fingerprint)
            except ValueError:
                pass
            raise ValueError("Invalid LocalSend certificate fingerprint")
        return None

    def remote_url(self, peer, endpoint):
        return f"{peer['scheme']}://{peer['host']}:{peer['port']}{API}/{endpoint}"

    async def register_remote(self, peer):
        try:
            async with self.client.post(
                self.remote_url(peer, "register"),
                json=self.info(),
                ssl=self.remote_tls(peer),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status != 200:
                    self.announce(False)
                    return
                packet = await response.json()
                packet.update(protocol=peer["scheme"], port=peer["port"], fingerprint=peer["fingerprint"])
                self.register_peer(packet, peer["host"])
        except (TimeoutError, aiohttp.ClientError, ValueError):
            self.announce(False)

    async def local_info(self, request):
        return web.json_response(self.info())

    async def local_register(self, request):
        try:
            self.register_peer(await request.json(), request.remote)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest()
        return web.json_response(self.info())

    async def local_prepare(self, request):
        if not self.running:
            raise web.HTTPServiceUnavailable()
        if len(self.incoming) >= 16 or len(self.sessions) >= 16:
            raise web.HTTPTooManyRequests()
        try:
            packet = await request.json()
            files = packet["files"]
            if not isinstance(files, dict) or not 1 <= len(files) <= 100:
                raise ValueError("Invalid file count")
            items = {}
            for file_id, metadata in files.items():
                if len(str(file_id)) > 128:
                    raise ValueError("Invalid file ID")
                name = safe_name(metadata["fileName"])
                size = metadata["size"]
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ValueError("Invalid file size")
                items[file_id] = {
                    "name": name,
                    "size": size,
                    "sha256": metadata.get("sha256"),
                    "token": secrets.token_urlsafe(32),
                    "state": "pending",
                }
            total = sum(f["size"] for f in items.values())
            folder = Path(self.settings["directory"])
            folder.mkdir(parents=True, exist_ok=True)
            if total > shutil.disk_usage(folder).free * 0.8:
                raise ValueError("Not enough free disk space")
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest()
        request_id = str(uuid.uuid4())
        decision = asyncio.get_running_loop().create_future()
        sender = str(packet.get("info", {}).get("alias") or "Unnamed device")[:100]
        self.incoming[request_id] = {
            "public": {
                "id": request_id,
                "name": sender,
                "protocol": "localsend",
                "files": [
                    {"name": f["name"], "size": f["size"]} for f in items.values()
                ],
                "totalSize": total,
                "pin": None,
            },
            "decision": decision,
        }
        try:
            accepted = await asyncio.wait_for(decision, 60)
        except TimeoutError:
            accepted = False
        finally:
            self.incoming.pop(request_id, None)
        if not accepted or not self.running:
            raise web.HTTPForbidden(text="Rejected")
        self.sessions[request_id] = {
            "files": items,
            "host": request.remote,
            "directory": folder,
            "expires": time.monotonic() + 600,
            "cancelled": False,
        }
        self.transfers[request_id] = Transfer({
            "id": request_id,
            "name": ", ".join(f["name"] for f in items.values()),
            "peer": sender,
            "protocol": "localsend",
            "direction": "in",
            "bytes": 0,
            "total": total,
            "state": "waiting",
            "error": "",
            "files": len(items),
            "fileName": next(iter(items.values()))["name"],
        })
        return web.json_response(
            {
                "sessionId": request_id,
                "files": {key: f["token"] for key, f in items.items()},
            }
        )

    async def local_upload(self, request):
        session_id, file_id, token = (
            request.query.get(k) for k in ("sessionId", "fileId", "token")
        )
        session = self.sessions.get(session_id)
        if (
            not session
            or session["host"] != request.remote
            or session["expires"] < time.monotonic()
            or session["cancelled"]
        ):
            raise web.HTTPForbidden()
        entry = session["files"].get(file_id)
        if not entry or not token or not secrets.compare_digest(token, entry["token"]):
            raise web.HTTPForbidden()
        if entry["state"] != "pending":
            raise web.HTTPConflict()
        entry["state"] = "uploading"
        transfer = self.transfers[session_id]
        transfer["state"] = "transferring"
        folder = session["directory"]
        temporary = folder / (".omadrop-" + secrets.token_hex(16) + ".part")
        digest, count = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as target:
                temporary.chmod(0o600)
                async with asyncio.timeout(300):
                    async for chunk in request.content.iter_chunked(256 * 1024):
                        if session["cancelled"]:
                            raise web.HTTPForbidden(text="Cancelled")
                        count += len(chunk)
                        if count > entry["size"]:
                            raise web.HTTPBadRequest(text="File larger than announced")
                        target.write(chunk)
                        digest.update(chunk)
                        transfer["bytes"] += len(chunk)
            if count != entry["size"] or (
                entry["sha256"] and digest.hexdigest() != entry["sha256"]
            ):
                raise web.HTTPUnprocessableEntity(text="Size or checksum mismatch")
            # Hard linking is atomic and never overwrites a pre-existing file.
            for suffix in range(1000):
                path = Path(entry["name"])
                name = (
                    path.name if suffix == 0 else f"{path.stem} ({suffix}){path.suffix}"
                )
                try:
                    os.link(temporary, folder / name)
                    break
                except FileExistsError:
                    continue
            else:
                raise web.HTTPConflict(text="Too many duplicate names")
            entry["state"] = "complete"
            if all(f["state"] == "complete" for f in session["files"].values()):
                transfer["state"] = "complete"
                self.sessions.pop(session_id, None)
            return web.Response()
        except BaseException as error:
            entry["state"] = "failed"
            transfer.update(
                state="cancelled" if session["cancelled"] else "failed",
                error=str(error)[:200],
            )
            raise
        finally:
            temporary.unlink(missing_ok=True)

    async def local_cancel(self, request):
        session_id = request.query.get("sessionId")
        session = self.sessions.get(session_id)
        if not session or session["host"] != request.remote:
            raise web.HTTPForbidden()
        session["cancelled"] = True
        self.transfers[session_id]["state"] = "cancelled"
        self.sessions.pop(session_id, None)
        return web.Response()

    async def start_quick(self):
        if not os.access(self.quick_path, os.X_OK):
            raise RuntimeError("Quick Share bridge is not installed")
        Path(self.settings["directory"]).mkdir(parents=True, exist_ok=True)
        # The engine writes only into private staging. Validate remote names
        # before consent, then publish completed files without overwriting.
        self.quick_staging = Path(
            tempfile.mkdtemp(prefix="quickshare-", dir=runtime_dir())
        )
        self.quick = await asyncio.create_subprocess_exec(
            self.quick_path,
            str(self.quick_staging),
            os.environ.get("OMADROP_QUICKSHARE_PORT", str(QUICK_PORT)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            first = await asyncio.wait_for(self.quick.stdout.readline(), 15)
            if not first or json.loads(first).get("event") != "ready":
                raise RuntimeError("Quick Share engine could not start")
        except BaseException:
            if self.quick.returncode is None:
                self.quick.kill()
            await self.quick.wait()
            self.quick = None
            raise
        self.channel("quickshare", "advertising")
        self.spawn(self.quick_events())
        self.spawn(self.quick_errors())

    async def quick_errors(self):
        # Drain stderr so diagnostics cannot block the protocol process.
        while self.quick and await self.quick.stderr.readline():
            pass

    async def quick_write(self, message):
        if not self.quick or self.quick.returncode is not None:
            raise RuntimeError("Quick Share is not running")
        self.quick.stdin.write((json.dumps(message) + "\n").encode())
        await self.quick.stdin.drain()

    async def quick_events(self):
        while self.quick:
            line = await self.quick.stdout.readline()
            if not line:
                self.channel("quickshare", "unavailable", "Quick Share engine stopped")
                for key in [k for k in self.peers if k.startswith("quickshare:")]:
                    self.peers.pop(key)
                break
            try:
                event = json.loads(line)
                data = event.get("data", {})
                if event["event"] == "peer":
                    key = "quickshare:" + data["id"]
                    if data.get("present") is False:
                        self.peers.pop(key, None)
                    elif data.get("ip") and data.get("port"):
                        self.peers[key] = {
                            "id": key,
                            "name": data.get("name") or "Unnamed device",
                            "protocol": "quickshare",
                            "deviceType": str(data.get("rtype") or "desktop"),
                            "host": peer_address(data["ip"]),
                            "port": int(data["port"]),
                            "seen": time.monotonic(),
                        }
                elif event["event"] == "transfer":
                    state = data.get("state")
                    previous = self.transfers.get("quickshare:" + data.get("id", ""), {})
                    if state not in ("SendingFiles", "ReceivingFiles") or previous.get("state") != "transferring":
                        meta = data.get("meta") or {}
                        print("Quick Share event: " + json.dumps({"state": state,
                            "direction": data.get("rtype"), "files": len(meta.get("files") or []),
                            "textType": meta.get("text_type"), "hasTextTitle": meta.get("text_description") is not None}),
                            file=sys.stderr, flush=True)
                    self.quick_transfer(data)
                elif event["event"] == "diagnostic":
                    print("Quick Share diagnostic: " + json.dumps({k: event[k] for k in ("phase", "file", "line") if k in event}),
                          file=sys.stderr, flush=True)
                elif event["event"] == "error":
                    self.error = str(event.get("error", "Quick Share error"))
            except (KeyError, TypeError, ValueError):
                continue

    def quick_transfer(self, data):
        key = "quickshare:" + data["id"]
        metadata = data.get("meta") or {}
        inbound = data.get("rtype") == "Inbound"
        name = (
            (metadata.get("source") or {}).get("name")
            or metadata.get("destination")
            or "Nearby device"
        )
        files = metadata.get("files") or []
        state = data.get("state")
        if state == "WaitingForUserConsent" and inbound:
            try:
                if len(files) > 100:
                    raise ValueError("Too many files in request")
                for filename in files:
                    safe_name(filename)
                if len(set(files)) != len(files):
                    raise ValueError("Duplicate file names in request")
                if (
                    metadata.get("total_bytes", 0)
                    > shutil.disk_usage(self.settings["directory"]).free * 0.8
                ):
                    raise ValueError("Not enough free disk space")
                self.quick_files[key] = list(files)
                if not files:
                    self.quick_text[key] = None
            except (ValueError, OSError) as error:
                self.error = str(error)
                self.spawn(
                    self.quick_write({"action": "RejectTransfer", "id": data["id"]})
                )
                return
            self.incoming[key] = {
                "public": {
                    "id": key,
                    "name": name,
                    "protocol": "quickshare",
                    "files": [{"name": Path(f).name, "size": 0} for f in files],
                    "totalSize": metadata.get("total_bytes", 0),
                    "pin": metadata.get("pin_code"),
                    "kind": "file" if files else "text",
                    "description": str(metadata.get("text_description") or "Shared text or link")[:200],
                },
                "decision": None,
            }
        elif state not in ("Initial", "ReceivedConnectionRequest"):
            self.incoming.pop(key, None)
        mapped = {
            "Finished": "complete",
            "Cancelled": "cancelled",
            "Rejected": "rejected",
            "Disconnected": "failed",
            "SendingFiles": "transferring",
            "ReceivingFiles": "transferring",
        }.get(state, "waiting")
        previous = self.transfers.get(key, Transfer())
        was_cancelled = previous.get("state") == "cancelled"
        publication = self.quick_publications.get(key)
        publication_running = publication is not None and not publication.done()
        if was_cancelled and state in ("Finished", "Cancelled", "Rejected", "Disconnected"):
            mapped = "cancelled"
        if previous.get("state") == "complete" and state == "Disconnected":
            mapped = "complete"
        if inbound and state == "Finished":
            if was_cancelled:
                self.cleanup_quick_staging(key)
                if self.quick_active == key:
                    self.quick_active = None
            else:
                mapped = "transferring"
                if key in self.quick_text:
                    self.quick_text[key] = metadata.get("text_payload")
                self.spawn_quick_publication(key)
        elif (
            inbound
            and state in ("Cancelled", "Rejected", "Disconnected")
            and self.quick_active == key
            and previous.get("state") != "complete"
        ):
            # Finished is followed by Disconnected; publication may still run.
            if state in ("Cancelled", "Rejected") and publication_running:
                publication.cancel()
            if was_cancelled or state == "Cancelled":
                mapped = "cancelled"
            if publication_running:
                if state == "Disconnected" and not was_cancelled:
                    mapped = "transferring"
            else:
                self.quick_active = None
                self.cleanup_quick_staging(key)
        update = {
            "id": key,
            "name": ", ".join(Path(f).name for f in files)
            or previous.get("name", "Transfer"),
            "peer": name,
            "protocol": "quickshare",
            "direction": "in" if inbound else "out",
            "bytes": metadata.get("ack_bytes", previous.get("bytes", 0)),
            "total": metadata.get("total_bytes", previous.get("total", 0)),
            "state": mapped,
            "error": "Recipient rejected the transfer" if mapped == "rejected"
            else "Device disconnected" if mapped == "failed" else "",
            "files": len(files) or previous.get("files", 1),
            "fileName": Path(files[0]).name if files else previous.get("fileName", "Transfer"),
        }
        if previous.get("state") == "complete" and state == "Disconnected":
            update.update(bytes=previous.get("bytes", 0), total=previous.get("total", 0))
        if inbound and not files and key in self.quick_text:
            update.update(name=str(metadata.get("text_description") or "Shared text or link")[:200],
                          fileName="website.txt" if metadata.get("text_type") == "Url" else "shared-text.txt")
        previous.update(update)
        self.transfers[key] = previous
        if not inbound and mapped not in ACTIVE_STATES:
            cleanup_sent_files(self.outgoing_files.pop(key, []))
        if inbound and state == "Finished" and not was_cancelled:
            self.transfers[key]["publishing"] = True
        elif inbound and publication_running:
            self.transfers[key]["publishing"] = True
        elif mapped != "transferring":
            self.transfers[key].pop("publishing", None)

    async def publish_quick_files(self, key):
        transfer = self.transfers[key]
        try:
            if transfer.get("state") == "cancelled":
                raise asyncio.CancelledError
            folder = Path(self.settings["directory"])
            if key in self.quick_text:
                payload = self.quick_text.pop(key)
                if not isinstance(payload, str) or not payload or len(payload.encode()) > 1024 * 1024:
                    raise ValueError("Shared text is empty or exceeds the 1 MB limit")
                if transfer["fileName"] == "website.txt":
                    await open_shared_urls([payload])
                    if transfer.get("state") == "cancelled":
                        raise asyncio.CancelledError
                    transfer.update(state="complete", bytes=len(payload.encode()), total=len(payload.encode()))
                    self.quick_files.pop(key, None)
                    return
                filename = transfer["fileName"]
                path = self.quick_staging / filename
                with path.open("xb") as output:
                    path.chmod(0o600)
                    output.write(payload.encode("utf-8"))
                self.quick_files[key] = [filename]
                transfer.update(bytes=len(payload.encode()), total=len(payload.encode()))
            for filename in self.quick_files.get(key, []):
                source_path = self.quick_staging / safe_name(filename)
                temporary = folder / (".omadrop-" + secrets.token_hex(16) + ".part")
                try:
                    descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
                    with (
                        os.fdopen(descriptor, "rb") as source,
                        temporary.open("xb") as destination,
                    ):
                        temporary.chmod(0o600)
                        while chunk := source.read(256 * 1024):
                            destination.write(chunk)
                            await self.quick_publication_checkpoint()
                            if transfer.get("state") == "cancelled":
                                raise asyncio.CancelledError
                    if transfer.get("state") == "cancelled":
                        raise asyncio.CancelledError
                    for suffix in range(1000):
                        path = Path(filename)
                        name = (
                            path.name
                            if suffix == 0
                            else f"{path.stem} ({suffix}){path.suffix}"
                        )
                        try:
                            os.link(temporary, folder / name)
                            break
                        except FileExistsError:
                            continue
                    else:
                        raise RuntimeError("Too many duplicate file names")
                    source_path.unlink()
                finally:
                    temporary.unlink(missing_ok=True)
            if transfer.get("state") == "cancelled":
                raise asyncio.CancelledError
            self.quick_files.pop(key, None)
            transfer["state"] = "complete"
        except asyncio.CancelledError:
            transfer["state"] = "cancelled"
            self.cleanup_quick_staging(key)
            raise
        except Exception as error:
            if transfer.get("state") == "cancelled":
                self.cleanup_quick_staging(key)
            else:
                transfer.update(state="failed", error=str(error)[:240])
                self.cleanup_quick_staging(key)
        finally:
            transfer.pop("publishing", None)
            if self.quick_active == key:
                self.quick_active = None

    async def start_airdrop(self):
        if not shutil.which("omdrop"):
            raise RuntimeError(
                "AirDrop receiver is not installed; needs a supported AWDL radio backend"
            )
        before = json.loads(await run_command("omdrop", "status", "--json"))
        # Do not take ownership of a receiver another application started.
        if not before.get("receiving"):
            await run_command("omdrop", "dir", self.settings["directory"])
        if not before.get("receiving") or before.get("visible") is not True:
            await run_command("omdrop", "on", "forever", timeout=30)
            if not before.get("receiving"):
                self.airdrop_owned = True
        await self.poll_airdrop()

    async def poll_airdrop(self):
        data = json.loads(await run_command("omdrop", "status", "--json"))
        if data.get("visible") is not True or not data.get("receiving"):
            for key in [key for key in self.peers if key.startswith("airdrop:")]:
                self.peers.pop(key, None)
            self.channel(
                "airdrop",
                "unavailable",
                data.get("reason") or "AirDrop radio is not advertising",
            )
            return
        self.channel("airdrop", "advertising")
        peers = json.loads(
            await run_command(
                "omdrop", "peers", "--json", timeout=8, accepted_codes=(0, 1)
            )
        )
        previous = {k: v for k, v in self.peers.items() if k.startswith("airdrop:")}
        for key in previous:
            self.peers.pop(key)
        for peer in peers:
            key = "airdrop:" + peer["mac"]
            self.peers[key] = {
                "id": key,
                "name": peer.get("name") or previous.get(key, {}).get("name") or "Unnamed Apple device",
                "protocol": "airdrop",
                "deviceType": "mobile",
                "mac": peer["mac"],
                "seen": time.monotonic(),
            }
        if self.running and peers and (self.airdrop_names is None or self.airdrop_names.done()):
            self.airdrop_names = self.spawn(self.resolve_airdrop_names())

    async def resolve_airdrop_names(self):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "omdrop", "peers", "-n", "--json", "--stream",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            # Name lookup actively wakes listening AirDrop devices, but only
            # during the existing visibility lease/window. Publish each answer
            # immediately instead of waiting for every silent peer to time out.
            async with asyncio.timeout(60):
                while line := await process.stdout.readline():
                    try:
                        peer = json.loads(line)
                        entry = self.peers.get("airdrop:" + peer["mac"])
                        if entry is not None and peer.get("name"):
                            entry["name"] = str(peer["name"])[:100]
                    except (ValueError, KeyError, TypeError):
                        continue
                await process.wait()
        except (OSError, TimeoutError, ValueError):
            # Failure to name a device doesn't mean the radio stopped advertising.
            pass
        finally:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

    async def maintenance(self):
        while self.running:
            await asyncio.sleep(20)
            self.announce()
            if self.local_runner and (self.local_scan is None or self.local_scan.done()) and time.monotonic() - self.local_scan_at >= 60:
                self.local_scan_at = time.monotonic()
                self.local_scan = self.spawn(self.discover_local())
            async with self.lock:
                if self.running:
                    await self.refresh_airdrop()
            for key, session in list(self.sessions.items()):
                if session["expires"] < time.monotonic():
                    session["cancelled"] = True
                    self.sessions.pop(key, None)
                    self.transfers[key].update(
                        state="failed", error="Transfer timed out"
                    )

    async def housekeeping(self):
        while True:
            for key, grant in list(self.airdrop_progress.items()):
                if grant["expires"] <= time.monotonic():
                    self.airdrop_progress.pop(key, None)
                    transfer = self.transfers.get(key)
                    if transfer and transfer["state"] in ACTIVE_STATES:
                        transfer.update(state="failed", error="AirDrop upload did not start or finish in time")
            await self.expire_stalled_transfers()
            finished = sorted(
                (key for key, value in self.transfers.items() if value["state"] not in ACTIVE_STATES),
                key=lambda key: self.transfers[key].get("updated", 0), reverse=True,
            )
            for key in finished[20:]:
                if self.transfers[key].get("updated", 0) < time.time() - 600:
                    self.transfers.pop(key, None)
            protected = [path for files in self.outgoing_files.values() for path in files]
            cleanup_old_clipboard(protected)
            await asyncio.sleep(30)

    async def expire_stalled_transfers(self):
        now = time.monotonic()
        for key, transfer in list(self.transfers.items()):
            if transfer["state"] not in ACTIVE_STATES:
                self.transfer_watch.pop(key, None)
                continue
            progress = (transfer["state"], transfer.get("bytes", 0))
            previous, last_progress = self.transfer_watch.get(key, (progress, now))
            if previous != progress:
                last_progress = now
            self.transfer_watch[key] = (progress, last_progress)
            if now - last_progress < 300:
                continue
            tasks = [task for task in self.tasks if task.get_name() == key]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if key.startswith("quickshare:"):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.quick_write(
                        {"action": "CancelTransfer", "id": key.split(":", 1)[1]}
                    ), 5)
            if key in self.sessions:
                self.sessions[key]["cancelled"] = True
            transfer.update(state="failed", error="Transfer timed out after 5 minutes without progress")
            cleanup_sent_files(self.outgoing_files.pop(key, []))
            self.transfer_watch.pop(key, None)

    async def refresh_airdrop(self):
        try:
            if self.channels["airdrop"]["state"] == "advertising":
                await self.poll_airdrop()
            elif shutil.which("omdrop"):
                readiness = json.loads(await run_command("omdrop", "doctor", "--json"))
                if readiness.get("ready") is True:
                    await self.start_airdrop()
                else:
                    reasons = [row["say"] for row in readiness.get("missing", []) if row.get("say")]
                    self.channel("airdrop", "unavailable", "; ".join(reasons)[:240]
                                 or "AirDrop radio is not ready")
        except Exception as error:
            self.channel("airdrop", "unavailable", str(error)[:240])

    def outgoing_transfer(self, peer, files, transfer_id):
        total = sum(Path(f).stat().st_size for f in files)
        return Transfer({
            "id": transfer_id,
            "name": ", ".join(Path(f).name for f in files),
            "peer": peer["name"],
            "protocol": peer["protocol"],
            "direction": "out",
            "bytes": 0,
            "total": total,
            "state": "waiting",
            "error": "",
            "files": len(files),
            "fileName": Path(files[0]).name,
        })

    async def send_files(self, peer, files, transfer_id):
        self.outgoing_files[transfer_id] = files
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            transfer = self.outgoing_transfer(peer, files, transfer_id)
            self.transfers[transfer_id] = transfer
        total = transfer["total"]
        quick_dispatched = False
        try:
            if peer["protocol"] == "quickshare":
                await self.quick_write(
                    {
                        "action": "send",
                        "id": transfer_id.split(":", 1)[1],
                        "name": peer["name"],
                        "addr": f"{peer['host']}:{peer['port']}",
                        "files": files,
                    }
                )
                quick_dispatched = True
                return
            if peer["protocol"] == "airdrop":
                transfer["state"] = "transferring"
                await run_command(
                    "omdrop", "send", "--to", peer["mac"], *files, timeout=300
                )
                transfer.update(state="complete", bytes=total)
                return
            metadata = {
                str(i): {
                    "id": str(i),
                    "fileName": Path(f).name,
                    "size": Path(f).stat().st_size,
                    "fileType": mimetypes.guess_type(f)[0]
                    or "application/octet-stream",
                }
                for i, f in enumerate(files)
            }
            async with self.client.post(
                self.remote_url(peer, "prepare-upload"),
                json={"info": self.info(), "files": metadata},
                ssl=self.remote_tls(peer),
            ) as response:
                if response.status != 200:
                    raise (RecipientRejected if response.status == 403 else RuntimeError)(
                        f"Recipient refused transfer ({response.status})"
                    )
                session = await response.json()
            transfer["state"] = "transferring"
            for file_id, token in session["files"].items():
                path = Path(files[int(file_id)])

                async def chunks():
                    with path.open("rb") as source:
                        while data := source.read(256 * 1024):
                            yield data
                            transfer["bytes"] += len(data)

                query = {
                    "sessionId": session["sessionId"],
                    "fileId": file_id,
                    "token": token,
                }
                async with self.client.post(
                    self.remote_url(peer, "upload"),
                    params=query,
                    data=chunks(),
                    ssl=self.remote_tls(peer),
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(path.stat().st_size),
                    },
                    timeout=aiohttp.ClientTimeout(total=600, connect=5),
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Upload failed ({response.status})")
            transfer["state"] = "complete"
        except asyncio.CancelledError:
            transfer["state"] = "cancelled"
            if (
                peer["protocol"] == "localsend"
                and "session" in locals()
                and self.client
            ):
                with contextlib.suppress(Exception):
                    async with self.client.post(
                        self.remote_url(peer, "cancel"),
                        params={"sessionId": session["sessionId"]},
                        ssl=self.remote_tls(peer),
                        timeout=aiohttp.ClientTimeout(total=3),
                    ):
                        pass
            raise
        except RecipientRejected as error:
            transfer.update(state="rejected", error=str(error)[:240])
        except Exception as error:
            transfer.update(state="failed", error=str(error)[:240])
        finally:
            if peer["protocol"] != "quickshare" or not quick_dispatched:
                cleanup_sent_files(files)
                self.outgoing_files.pop(transfer_id, None)

    async def action(self, request):
        packet = await request.json()
        command = packet.get("command", "status")
        try:
            if command == "receive-request":
                if not self.running or len(self.incoming) >= 8:
                    return web.json_response({"accepted": False})
                urls = shared_urls(packet["urls"]) if "urls" in packet else []
                files = packet["files"]
                if not isinstance(files, list) or not 1 <= len(files) <= 100:
                    raise ValueError("Invalid incoming files")
                files = [{"name": safe_name(row["name"]), "size": row["size"]} for row in files]
                if any(type(row["size"]) is not int or row["size"] < 0 for row in files):
                    raise ValueError("Invalid incoming size")
                key = "airdrop:" + str(uuid.uuid4())
                decision = asyncio.get_running_loop().create_future()
                self.incoming[key] = {"public": {
                    "id": key, "name": str(packet.get("peer") or "Apple device")[:100],
                    "protocol": "airdrop", "files": files,
                    "totalSize": sum(row["size"] for row in files), "pin": None,
                    "expiresAt": time.time() + 30,
                    "kind": "text" if packet.get("kind") == "text" else "file",
                    "description": str(packet.get("description") or "")[:200],
                }, "decision": decision}
                try:
                    accepted = await asyncio.wait_for(decision, 30)
                except TimeoutError:
                    accepted = False
                finally:
                    self.incoming.pop(key, None)
                if accepted is not True or not self.running:
                    return web.json_response({"accepted": False})
                token = secrets.token_urlsafe(32)
                self.airdrop_progress[key] = {"token": token, "expires": time.monotonic() + 60, "urls": urls}
                self.transfers[key] = Transfer(id=key, name=", ".join(row["name"] for row in files),
                    fileName=files[0]["name"], files=len(files), peer=str(packet.get("peer") or "Apple device")[:100],
                    protocol="airdrop", direction="in", bytes=0, total=sum(row["size"] for row in files),
                    state="waiting", error="")
                return web.json_response({"accepted": True, "transferId": key, "token": token})
            elif command == "receive-progress":
                key = packet["id"]
                transfer = self.transfers.get(key)
                if transfer and transfer["state"] == "cancelled":
                    raise ValueError("Receive session cancelled")
                grant = self.airdrop_progress.get(key)
                if not grant or grant["expires"] < time.monotonic() or not secrets.compare_digest(grant["token"], str(packet.get("token", ""))):
                    raise ValueError("Receive session expired")
                if transfer is None:
                    raise ValueError("Receive session expired")
                state, count = packet["state"], packet["bytes"]
                if transfer["state"] not in ACTIVE_STATES:
                    raise ValueError("Receive session finished")
                if state not in ("transferring", "complete", "failed") or type(count) is not int or not transfer["bytes"] <= count <= transfer["total"]:
                    raise ValueError("Invalid receive progress")
                if state == "complete" and grant.get("urls"):
                    # Consume before launching: a repeated progress packet cannot reopen tabs.
                    urls = grant.pop("urls")
                    try:
                        await open_shared_urls(urls)
                    except RuntimeError as error:
                        transfer.update(state="failed", error=str(error))
                        self.airdrop_progress.pop(key, None)
                        raise
                transfer.update(state=state, bytes=count, error=str(packet.get("error") or "")[:240])
                grant["expires"] = time.monotonic() + 600
                if state in ("complete", "failed"):
                    self.airdrop_progress.pop(key, None)
            elif command == "start":
                async with self.lock:
                    duration = packet.get("durationSeconds", 600)
                    if type(duration) is not int or not 1 <= duration <= 600:
                        raise ValueError("Advertising duration must be 1–600 seconds")
                    self.settings["enabled"] = True
                    self.settings["advertiseUntil"] = time.time() + duration
                    save_settings(self.settings)
                    await self.start()
                    self.arm_window()
            elif command == "lease":
                async with self.lock:
                    duration = packet.get("durationSeconds", 30)
                    if type(duration) is not int or not 5 <= duration <= 120:
                        raise ValueError("Menu lease must be 5–120 seconds")
                    if not self.running:
                        await self.start()
                    self.menu_until = max(self.menu_until, time.time() + duration)
                    self.arm_window()
            elif command == "release":
                async with self.lock:
                    self.menu_until = 0
                    if self.settings.get("advertiseUntil", 0) > time.time():
                        self.arm_window()
                    else:
                        self.settings.update(enabled=False, advertiseUntil=0)
                        save_settings(self.settings)
                        await self.stop()
            elif command == "stop":
                async with self.lock:
                    self.settings["enabled"] = False
                    self.settings["advertiseUntil"] = 0
                    save_settings(self.settings)
                    await self.stop()
            elif command in ("accept", "reject"):
                key = packet["id"]
                pending = self.incoming.get(key)
                if not pending:
                    raise ValueError("Request expired")
                if key.startswith("quickshare:"):
                    if command == "accept":
                        if self.quick_active is not None:
                            raise ValueError(
                                "Wait for the current Quick Share receive to finish"
                            )
                        if key not in self.quick_files:
                            raise ValueError("Request has no validated files")
                        self.quick_active = key
                    await self.quick_write(
                        {
                            "action": "AcceptTransfer"
                            if command == "accept"
                            else "RejectTransfer",
                            "id": key.split(":", 1)[1],
                        }
                    )
                    self.incoming.pop(key, None)
                elif not pending["decision"].done():
                    pending["decision"].set_result(command == "accept")
            elif command == "cancel":
                key = packet["id"]
                if key.startswith("quickshare:"):
                    transfer = self.transfers.get(key)
                    publication = self.quick_publications.get(key)
                    if transfer and transfer.get("state") in ACTIVE_STATES:
                        transfer["state"] = "cancelled"
                    if publication is not None and not publication.done():
                        publication.cancel()
                        await asyncio.gather(publication, return_exceptions=True)
                        self.cleanup_quick_staging(key)
                        if self.quick_active == key:
                            self.quick_active = None
                    await self.quick_write(
                        {"action": "CancelTransfer", "id": key.split(":", 1)[1]}
                    )
                elif key in self.sessions:
                    session = self.sessions[key]
                    session["cancelled"] = True
                    self.sessions.pop(key)
                elif key.startswith("airdrop:"):
                    self.airdrop_progress.pop(key, None)
                else:
                    for task in self.tasks:
                        if task.get_name() == key:
                            task.cancel()
                if (
                    key in self.transfers
                    and not key.startswith("quickshare:")
                    and self.transfers[key]["state"] in ACTIVE_STATES
                ):
                    self.transfers[key]["state"] = "cancelled"
            elif command == "send":
                peer = self.peers.get(packet["peer"])
                if not peer:
                    raise ValueError("Device is no longer nearby")
                files = [str(Path(f).resolve(strict=True)) for f in packet["files"]]
                if (
                    not files
                    or len(files) > 100
                    or any(not Path(f).is_file() for f in files)
                ):
                    raise ValueError("Choose 1 to 100 files")
                key = peer["protocol"] + ":" + str(uuid.uuid4())
                self.transfers[key] = self.outgoing_transfer(peer, files, key)
                self.spawn(self.send_files(dict(peer), files, key)).set_name(key)
                return web.json_response({**self.snapshot(), "ok": True, "transferId": key,
                                          "transfer": dict(self.transfers[key])})
            elif command == "dismiss":
                key = packet["id"]
                keys = list(self.transfers) if key == "all" else [key]
                for key in keys:
                    transfer = self.transfers.get(key)
                    if transfer and transfer["state"] not in ACTIVE_STATES:
                        self.transfers.pop(key)
            elif command == "directory":
                folder = Path(packet["path"]).expanduser().resolve()
                if not folder.is_dir() or not os.access(folder, os.W_OK):
                    raise ValueError("Choose a writable folder")
                if self.airdrop_owned:
                    await run_command("omdrop", "dir", str(folder))
                self.settings["directory"] = str(folder)
                save_settings(self.settings)
            elif command != "status":
                raise ValueError("Unknown command")
            return web.json_response(self.snapshot())
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            return web.json_response({"error": str(error)[:300]}, status=400)


async def daemon():
    os.umask(0o077)
    runtime = runtime_dir()
    with (runtime / "daemon.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        endpoint = runtime / "control.sock"
        endpoint.unlink(missing_ok=True)
        service = Coordinator()
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/", service.action)
        runner = web.AppRunner(app, shutdown_timeout=3, handler_cancellation=True)
        await runner.setup()
        await web.UnixSite(runner, str(endpoint)).start()
        endpoint.chmod(0o600)
        done = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, done.set)
        try:
            async with service.lock:
                await service.restore_window()
            housekeeping = asyncio.create_task(service.housekeeping())
            await done.wait()
        finally:
            if "housekeeping" in locals():
                housekeeping.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await housekeeping
            await service.stop()
            await runner.cleanup()
            endpoint.unlink(missing_ok=True)


async def control(packet):
    endpoint = runtime_dir(create=False) / "control.sock"
    async with aiohttp.ClientSession(
        connector=aiohttp.UnixConnector(path=str(endpoint)),
        timeout=aiohttp.ClientTimeout(total=90),
    ) as session:
        async with session.post("http://localhost/", json=packet) as response:
            data = await response.json()
            if response.status != 200:
                raise RuntimeError(data.get("error", "Action failed"))
            return data


def pick_files(directory=False):
    if not shutil.which("zenity"):
        raise RuntimeError("File picker requires zenity")
    command = ["zenity", "--file-selection", "--title=OmaDrop"]
    command += ["--directory"] if directory else ["--multiple", "--separator=\n"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode == 1:
        return []
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-240:] or "File picker failed")
    return result.stdout.rstrip("\n").split("\n")


async def cli(args):
    if args.command == "daemon":
        await daemon()
        return
    if args.command == "pick":
        print(json.dumps({"files": file_metadata(pick_files())}))
        return
    if args.command == "stage-clipboard":
        print(json.dumps(await stage_clipboard()))
        return
    if args.command == "stage-text":
        print(json.dumps(stage_text(sys.stdin.buffer.read(1024 * 1024 + 1))))
        return
    packet = {"command": args.command}
    if args.command in ("receive-request", "receive-progress"):
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Incoming metadata is too large")
        metadata = json.loads(raw)
        packet.update(metadata)
        packet["command"] = args.command
    if args.command in ("start", "lease"):
        packet["durationSeconds"] = args.seconds
    if args.command in ("accept", "reject", "cancel", "dismiss"):
        packet["id"] = args.id
    if args.command == "send":
        files = pick_files() if args.pick else args.files
        if not files:
            print(json.dumps({"cancelled": True}))
            return
        packet.update(peer=args.peer, files=files)
    if args.command == "directory":
        folder = pick_files(True) if args.pick else [args.path]
        if not folder or not folder[0]:
            print(json.dumps({"cancelled": True}))
            return
        packet["path"] = folder[0]
    try:
        data = await control(packet)
    except (aiohttp.ClientError, FileNotFoundError, ConnectionRefusedError):
        if args.command == "status":
            service = Coordinator()
            for key, _ in PROTOCOLS:
                service.channel(key, "stopped")
            print(json.dumps(service.snapshot()))
            return
        if args.command not in ("start", "lease"):
            raise RuntimeError("Sharing service is stopped")
        log = runtime_dir() / "daemon.log"
        with log.open("ab") as out:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "daemon"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                data = await control(packet)
                break
            except (aiohttp.ClientError, FileNotFoundError, ConnectionRefusedError):
                continue
        else:
            raise RuntimeError("Sharing service did not start; inspect its daemon.log")
    print(json.dumps(data))


def main():
    parser = argparse.ArgumentParser(description="OmaDrop unified nearby sharing")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "start", "lease", "release", "stop", "daemon", "receive-request", "receive-progress", "pick", "stage-clipboard", "stage-text"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument(
            "--json", action="store_true", help=argparse.SUPPRESS
        )
        if command in ("start", "lease"):
            command_parser.add_argument("--seconds", type=int, default=600 if command == "start" else 30)
    for command in ("accept", "reject", "cancel", "dismiss"):
        sub.add_parser(command).add_argument("id")
    send = sub.add_parser("send")
    send.add_argument("--peer", required=True)
    send.add_argument("--pick", action="store_true")
    send.add_argument("files", nargs="*")
    directory = sub.add_parser("directory")
    directory.add_argument("--pick", action="store_true")
    directory.add_argument("path", nargs="?")
    try:
        asyncio.run(cli(parser.parse_args()))
    except (TimeoutError, RuntimeError, ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
