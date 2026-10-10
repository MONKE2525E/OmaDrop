"""Real HTTPS cancellation checks for the installed AirDrop receiver patch."""

import asyncio
import gzip
import http.client
import importlib.util
import os
import plistlib
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_sharing import LocalOnly

RECEIVER_BIN = Path(
    os.environ.get(
        "OMADROP_AIRDROP_RECEIVER_DIR",
        Path.home() / ".local/share/omadrop/airdrop/bin",
    )
)
RECEIVER_SCRIPT = RECEIVER_BIN / "airdrop-serve.py"
PATCHED_RECEIVER = RECEIVER_SCRIPT.is_file() and all(
    marker in RECEIVER_SCRIPT.read_text(errors="replace")
    for marker in (
        "on_chunk=lambda: airdrop_consent.report(consent)",
        "airdrop_consent.report(consent, 'complete')",
    )
)
LIBARCHIVE_SITE = None
try:
    spec = importlib.util.find_spec("libarchive")
except (ImportError, ValueError):
    spec = None
if spec and spec.origin:
    LIBARCHIVE_SITE = str(Path(spec.origin).parent.parent)
else:
    for candidate in (
        *Path("/usr/lib").glob("python*/site-packages/libarchive/__init__.py"),
        *Path("/usr/local/lib").glob("python*/site-packages/libarchive/__init__.py"),
    ):
        LIBARCHIVE_SITE = str(candidate.parent.parent)
        break


def _newc_entry(name, data, inode):
    encoded_name = name.encode("utf-8") + b"\0"
    fields = (
        inode,
        stat.S_IFREG | 0o600,
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(encoded_name),
        0,
    )
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
    record = header + encoded_name
    record += b"\0" * (-len(record) % 4)
    record += data
    record += b"\0" * (-len(data) % 4)
    return record


def _archive(name, data):
    cpio = _newc_entry(name, data, 1) + _newc_entry("TRAILER!!!", b"", 2)
    return gzip.compress(cpio, mtime=0)


def _unused_ipv6_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        listener.bind(("::1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(
    PATCHED_RECEIVER and LIBARCHIVE_SITE,
    "Install the pinned patched AirDrop receiver and libarchive-c first",
)
class AirDropReceiverCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omadrop-airdrop-receiver-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "runtime-home").mkdir(mode=0o700)
        self.destination = self.root / "received"
        self.destination.mkdir()
        (self.destination / "keep.txt").write_bytes(b"pre-existing file")
        (self.destination / "new.txt").write_bytes(b"pre-existing target")
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.root / "config-home"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime-home"),
                "OMADROP_CONFIG_DIR": str(self.root / "coordinator-config"),
                "OMADROP_RUNTIME_DIR": str(self.root / "coordinator-runtime"),
            },
        )
        self.environment.start()

        self.service = LocalOnly()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.service.port = sock.getsockname()[1]
        self.service.settings["directory"] = str(self.root / "localsend")
        await self.service.start()
        app = web.Application()
        app.router.add_post("/", self.service.action)
        self.control = TestClient(TestServer(app))
        await self.control.start_server()

        self.receiver_root = self.root / "receiver"
        shutil.copytree(
            RECEIVER_BIN.parent,
            self.receiver_root,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.receiver_bin = self.receiver_root / "bin"
        shutil.copy2(
            Path(__file__).parents[1] / "scripts/airdrop_consent.py",
            self.receiver_bin / "airdrop_consent.py",
        )
        bridge = self.home / ".local/bin/omadrop"
        bridge.parent.mkdir(parents=True)
        self.progress_log = self.root / "progress.log"
        self.cancel_marker = self.root / "cancelled"
        control_url = str(self.control.server.make_url("/"))
        bridge.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys, time\n"
            "from urllib.error import HTTPError\n"
            "from urllib.request import Request, urlopen\n"
            f"CONTROL_URL = {control_url!r}\n"
            f"PROGRESS_LOG = {str(self.progress_log)!r}\n"
            f"CANCEL_MARKER = {str(self.cancel_marker)!r}\n"
            "command = sys.argv[1]\n"
            "packet = json.load(sys.stdin)\n"
            "packet['command'] = command\n"
            "def post(payload):\n"
            "    request = Request(CONTROL_URL, data=json.dumps(payload).encode(),\n"
            "        headers={'Content-Type': 'application/json'}, method='POST')\n"
            "    try:\n"
            "        with urlopen(request, timeout=8) as response:\n"
            "            return response.status, response.read()\n"
            "    except HTTPError as error:\n"
            "        return error.code, error.read()\n"
            "if command == 'receive-progress':\n"
            "    with open(PROGRESS_LOG, 'a') as log:\n"
            "        log.write(f\"{packet.get('state')} {packet.get('bytes')}\\n\")\n"
            "    if packet.get('state') == 'transferring' and packet.get('bytes') == 0:\n"
            "        if pathlib.Path(PROGRESS_LOG).read_text().count('\\n') <= 2:\n"
            "            time.sleep(0.35)\n"
            "    if packet.get('state') == 'transferring' and packet.get('bytes', 0) > 0:\n"
            "        post({'command': 'cancel', 'id': packet['id']})\n"
            "        pathlib.Path(CANCEL_MARKER).touch()\n"
            "status, body = post(packet)\n"
            "sys.stdout.buffer.write(body)\n"
            "raise SystemExit(1 if status >= 400 else 0)\n"
        )
        bridge.chmod(0o700)

        self.receiver_port = _unused_ipv6_port()
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (LIBARCHIVE_SITE, child_env.get("PYTHONPATH", ""))
            if part
        )
        self.receiver = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.receiver_bin / "airdrop-serve.py"),
            "--iface",
            "lo",
            "--port",
            str(self.receiver_port),
            "--name",
            "Test receiver",
            "--keys",
            str(self.root / "identity"),
            "--outdir",
            str(self.destination),
            "--config",
            str(self.root / "receiver-config.toml"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env,
        )
        async with asyncio.timeout(15):
            while True:
                line = await self.receiver.stdout.readline()
                if not line:
                    output = await self.receiver.stdout.read()
                    self.fail(f"AirDrop receiver exited during startup: {output.decode(errors='replace')}")
                if b"serving " in line:
                    break

    async def asyncTearDown(self):
        if getattr(self, "receiver", None) and self.receiver.returncode is None:
            self.receiver.terminate()
            try:
                await asyncio.wait_for(self.receiver.wait(), 5)
            except TimeoutError:
                self.receiver.kill()
                await self.receiver.wait()
        if getattr(self, "control", None):
            await self.control.close()
        if getattr(self, "service", None):
            await self.service.stop()
        self.environment.stop()
        self.temp.cleanup()

    async def test_https_upload_cancel_rolls_back_extracted_file(self):
        transfer_id = "test-transfer-123"
        body = _archive("new.txt", b"new data")
        ask = plistlib.dumps(
            {
                "TransferID": transfer_id,
                "SenderComputerName": "Test sender",
                "Files": [{"FileName": "new.txt", "FileSize": len(b"new data")}],
            },
            fmt=plistlib.FMT_BINARY,
        )

        def send_over_https():
            connection = http.client.HTTPSConnection(
                "::1",
                self.receiver_port,
                timeout=12,
                context=ssl._create_unverified_context(),
            )
            try:
                connection.request(
                    "POST",
                    "/Ask",
                    body=ask,
                    headers={"Content-Type": "application/x-apple-plist"},
                )
                approval = connection.getresponse()
                ask_status = approval.status
                approval.read()
                if ask_status != 200:
                    return ask_status, None
                connection.request(
                    "POST",
                    "/Upload",
                    body=body,
                    headers={
                        "Content-Type": "application/x-dvzip",
                        "TransferID": transfer_id,
                    },
                )
                response = connection.getresponse()
                status = response.status
                response.read()
                return ask_status, status
            finally:
                connection.close()

        upload = asyncio.create_task(asyncio.to_thread(send_over_https))
        try:
            async with asyncio.timeout(10):
                while not self.service.incoming:
                    await asyncio.sleep(0.01)
            key = next(iter(self.service.incoming))
            response = await self.control.post(
                "/", json={"command": "accept", "id": key}
            )
            self.assertEqual(response.status, 200)
            ask_status, upload_status = await asyncio.wait_for(upload, 15)
        finally:
            if not upload.done():
                upload.cancel()

        self.assertEqual(ask_status, 200)
        self.assertEqual(upload_status, 422)
        self.assertTrue(self.cancel_marker.exists())
        self.assertEqual(self.service.transfers[key]["state"], "cancelled")
        self.assertNotIn(key, self.service.airdrop_progress)
        self.assertFalse((self.destination / "new (1).txt").exists())
        self.assertEqual((self.destination / "new.txt").read_bytes(), b"pre-existing target")
        self.assertEqual((self.destination / "keep.txt").read_bytes(), b"pre-existing file")
        progress = self.progress_log.read_text().splitlines()
        self.assertGreaterEqual(progress.count("transferring 0"), 2)
        self.assertIn("transferring 8", progress)


if __name__ == "__main__":
    unittest.main()
