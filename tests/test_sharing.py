"""Real HTTPS LocalSend consent/upload checks, using isolated local endpoints."""

import asyncio
import hashlib
import importlib.util
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

SPEC = importlib.util.spec_from_file_location(
    "omadrop", Path(__file__).parents[1] / "scripts/omadrop.py"
)
omadrop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadrop)


class LocalOnly(omadrop.Coordinator):
    async def discover_local(self):
        pass

    async def start_quick(self):
        raise RuntimeError("Test has no Quick Share adapter")

    async def start_airdrop(self):
        raise RuntimeError("Test has no AWDL adapter")


class LocalSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_airdrop_name_stream_updates_early_and_cancellation_stops_lookup(self):
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        marker = Path(self.temp.name) / "lookup-stopped"
        executable = bin_dir / "omdrop"
        executable.write_text(f"#!{sys.executable}\nimport signal,time,pathlib\n"
            f"def stop(*args):\n pathlib.Path({str(marker)!r}).touch()\n raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "print('{\"mac\":\"test\",\"name\":\"Named phone\"}',flush=True)\n"
            "time.sleep(10)\n")
        executable.chmod(0o700)
        self.service.peers["airdrop:test"] = {"name": "Unnamed Apple device"}
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            task = asyncio.create_task(self.service.resolve_airdrop_names())
            try:
                for _ in range(100):
                    if self.service.peers["airdrop:test"]["name"] == "Named phone":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(self.service.peers["airdrop:test"]["name"], "Named phone")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(marker.exists())
        self.service.peers.pop("airdrop:test")

    async def test_localsend_presents_client_certificate_and_still_pins_peer(self):
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        directory = omadrop.config_dir()
        context.load_cert_chain(directory / "certificate.pem", directory / "private-key.pem")
        context.load_verify_locations(directory / "certificate.pem")
        context.verify_mode = ssl.CERT_REQUIRED
        app = web.Application()
        async def info(request):
            return web.json_response({"ok": True})
        app.router.add_get("/", info)
        server = TestServer(app)
        await server.start_server(ssl=context)
        try:
            async with self.service.client.get(server.make_url("/"), ssl=aiohttp.Fingerprint(bytes.fromhex(self.service.fingerprint))) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(aiohttp.ServerFingerprintMismatch):
                await self.service.client.get(server.make_url("/"), ssl=aiohttp.Fingerprint(bytes(32)))
        finally:
            await server.close()

    async def test_localsend_http_fallback_discovers_minimal_register_response(self):
        app = web.Application()
        async def register(request):
            info = await request.json()
            self.assertEqual(info["alias"], self.service.settings["name"])
            return web.json_response({"alias": "Test phone", "version": "2.2", "fingerprint": "test-phone"})
        app.router.add_post(omadrop.API + "/register", register)
        server = TestServer(app)
        await server.start_server()
        try:
            self.service.local_addresses = ["127.0.0.2"]
            with patch.object(omadrop, "PORT", server.port):
                await omadrop.Coordinator.discover_local(self.service)
            peer = self.service.peers["localsend:test-phone"]
            self.assertEqual(peer["host"], "127.0.0.1")
            self.assertEqual(peer["scheme"], "http")
            self.assertEqual(peer["name"], "Test phone")
        finally:
            await server.close()

    async def test_airdrop_sender_disconnect_removes_pending_approval(self):
        task = asyncio.create_task(self.control.post("/", json={
            "command": "receive-request", "peer": "Test Mac", "files": [{"name": "test.txt", "size": 4}],
        }))
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.service.incoming)
        pending = next(iter(self.service.incoming.values()))["public"]
        self.assertGreater(pending["expiresAt"], time.time())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(100):
            if not self.service.incoming:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.service.incoming)

    async def test_quickshare_website_opens_instead_of_saving_text(self):
        self.directory.mkdir()
        self.service.quick_staging = Path(self.temp.name) / "quick-staging"
        self.service.quick_staging.mkdir()
        metadata = {"source": {"name": "Test phone"}, "text_description": "Test website", "total_bytes": 0}
        self.service.quick_transfer({"id": "url", "rtype": "Inbound", "state": "WaitingForUserConsent", "meta": metadata})
        key = "quickshare:url"
        self.assertEqual(self.service.incoming[key]["public"]["kind"], "text")
        self.assertFalse((self.directory / "website.txt").exists())
        self.service.quick_active = key
        with patch.object(omadrop, "run_command", new=AsyncMock()) as opener:
            self.service.quick_transfer({"id": "url", "rtype": "Inbound", "state": "Finished", "meta": dict(metadata, text_type="Url", text_payload="https://example.test/path")})
            for _ in range(100):
                if self.service.transfers[key]["state"] == "complete":
                    break
                await asyncio.sleep(0.01)
            self.assertFalse((self.directory / "website.txt").exists())
            self.assertEqual(self.service.transfers[key]["state"], "complete")
            opener.assert_awaited_once_with("xdg-open", "https://example.test/path", timeout=10)
        self.service.quick_transfer({"id": "url", "rtype": "Inbound", "state": "Disconnected", "meta": metadata})
        self.assertEqual(self.service.transfers[key]["bytes"], len("https://example.test/path"))

    async def test_airdrop_progress_requires_approved_session_token(self):
        task = asyncio.create_task(self.control.post("/", json={
            "command": "receive-request", "peer": "Test Mac", "files": [{"name": "test.txt", "size": 4}],
        }))
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        key = next(iter(self.service.incoming))
        await self.control.post("/", json={"command": "accept", "id": key})
        response = await task
        approval = await response.json()
        packet = {"command": "receive-progress", "id": key, "token": "wrong", "bytes": 1, "state": "transferring"}
        response = await self.control.post("/", json=packet)
        self.assertEqual(response.status, 400)
        packet["token"] = approval["token"]
        response = await self.control.post("/", json=packet)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.service.transfers[key]["bytes"], 1)
        packet.update(bytes=4, state="complete")
        await self.control.post("/", json=packet)
        self.assertEqual(self.service.transfers[key]["state"], "complete")
        response = await self.control.post("/", json=packet)
        self.assertEqual(response.status, 400)

    async def test_airdrop_websites_open_only_after_approval_and_once(self):
        url = "https://example.test/?literal=$(no-shell)"
        with patch.object(omadrop, "run_command", new=AsyncMock()) as opener:
            task = asyncio.create_task(self.control.post("/", json={
                "command": "receive-request", "peer": "Test Mac", "urls": [url],
                "files": [{"name": "Website link.txt", "size": len(url) + 1}],
            }))
            for _ in range(100):
                if self.service.incoming:
                    break
                await asyncio.sleep(0.01)
            opener.assert_not_awaited()
            key = next(iter(self.service.incoming))
            await self.control.post("/", json={"command": "accept", "id": key})
            approval = await (await task).json()
            packet = {"command": "receive-progress", "id": key, "token": approval["token"],
                      "bytes": len(url) + 1, "state": "complete"}
            response = await self.control.post("/", json=packet)
            self.assertEqual(response.status, 200)
            opener.assert_awaited_once_with("xdg-open", url, timeout=10)
            self.assertEqual((await self.control.post("/", json=packet)).status, 400)
            self.assertEqual(opener.await_count, 1)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://example.test/\n"):
            response = await self.control.post("/", json={"command": "receive-request", "urls": [url], "files": []})
            self.assertEqual(response.status, 400)

    async def test_stalled_transfer_expires_but_real_progress_resets_timeout(self):
        transfer = omadrop.Transfer(id="stalled", state="transferring", bytes=0)
        self.service.transfers["stalled"] = transfer
        with patch.object(omadrop.time, "monotonic", return_value=1000):
            await self.service.expire_stalled_transfers()
        transfer["bytes"] = 1
        with patch.object(omadrop.time, "monotonic", return_value=1299):
            await self.service.expire_stalled_transfers()
        self.assertEqual(transfer["state"], "transferring")
        with patch.object(omadrop.time, "monotonic", return_value=1598):
            await self.service.expire_stalled_transfers()
        self.assertEqual(transfer["state"], "transferring")
        with patch.object(omadrop.time, "monotonic", return_value=1600):
            await self.service.expire_stalled_transfers()
        self.assertEqual(transfer["state"], "failed")
        self.assertIn("without progress", transfer["error"])

    async def test_menu_lease_expires_and_release_preserves_explicit_window(self):
        response = await self.control.post("/", json={"command": "lease", "durationSeconds": 5})
        self.assertEqual((await response.json())["mode"], "menu")
        self.assertIsNone(self.service.snapshot()["remainingSeconds"])
        self.assertFalse(self.service.settings["enabled"])
        response = await self.control.post("/", json={"command": "start", "durationSeconds": 60})
        self.assertEqual((await response.json())["mode"], "window")
        deadline = self.service.settings["advertiseUntil"]
        await self.control.post("/", json={"command": "lease", "durationSeconds": 5})
        self.assertEqual(self.service.settings["advertiseUntil"], deadline)
        await self.control.post("/", json={"command": "release"})
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.snapshot()["mode"], "window")
        await self.control.post("/", json={"command": "stop"})
        await self.control.post("/", json={"command": "lease", "durationSeconds": 5})
        self.service.window_timer.cancel()
        await asyncio.gather(self.service.window_timer, return_exceptions=True)
        self.service.menu_until = time.time() - 1
        await self.service.expire_window()
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.snapshot()["mode"], "off")

    async def test_dismiss_does_not_remove_active_transfers(self):
        self.service.transfers.update({
            "done": omadrop.Transfer(id="done", state="complete"),
            "active": omadrop.Transfer(id="active", state="waiting"),
        })
        await self.control.post("/", json={"command": "dismiss", "id": "all"})
        self.assertNotIn("done", self.service.transfers)
        self.assertIn("active", self.service.transfers)

    async def test_send_response_contains_queued_transfer_and_later_failure(self):
        source = Path(self.temp.name) / "send-test.txt"
        source.write_text("test")
        peer = {"id": "airdrop:test", "name": "Test Mac", "protocol": "airdrop", "mac": "test"}
        self.service.peers[peer["id"]] = peer
        from unittest.mock import AsyncMock
        with patch.object(omadrop, "run_command", new=AsyncMock(side_effect=RuntimeError("Recipient unreachable"))):
            response = await self.control.post("/", json={
                "command": "send", "peer": peer["id"], "files": [str(source)],
            })
            data = await response.json()
            self.assertEqual(response.status, 200)
            transfer_id = data["transferId"]
            self.assertEqual(data["transfers"][0]["id"], transfer_id)
            self.assertEqual(data["transfers"][0]["state"], "waiting")
            for _ in range(20):
                if self.service.transfers[transfer_id]["state"] == "failed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.service.transfers[transfer_id]["error"], "Recipient unreachable")

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omadrop-test-")
        self.environment = patch.dict(
            os.environ, {"OMADROP_CONFIG_DIR": self.temp.name + "/config"}
        )
        self.environment.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.service = LocalOnly()
        self.service.port = self.port
        self.directory = Path(self.temp.name) / "received"
        self.service.settings["directory"] = str(self.directory)
        app = web.Application()
        app.router.add_post("/", self.service.action)
        self.control = TestClient(TestServer(app, handler_cancellation=True))
        await self.control.start_server()
        await self.service.start()
        self.client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                ssl=aiohttp.Fingerprint(bytes.fromhex(self.service.fingerprint))
            )
        )
        self.url = f"https://127.0.0.1:{self.port}{omadrop.API}"

    async def asyncTearDown(self):
        await self.service.stop()
        await self.control.close()
        await self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    async def prepare(self, name="test.txt", content=b"hello", checksum=None):
        metadata = {
            "id": "file",
            "fileName": name,
            "size": len(content),
            "fileType": "text/plain",
            "sha256": checksum,
        }
        task = asyncio.create_task(
            self.client.post(
                self.url + "/prepare-upload",
                json={"info": {"alias": "Test sender"}, "files": {"file": metadata}},
            )
        )
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.service.incoming)
        return task, next(iter(self.service.incoming))

    async def approve(self, task, request_id):
        response = await self.control.post(
            "/", json={"command": "accept", "id": request_id}
        )
        self.assertEqual(response.status, 200)
        result = await task
        self.assertEqual(result.status, 200)
        return await result.json()

    async def upload(self, session, content, token=None):
        return await self.client.post(
            self.url + "/upload",
            params={
                "sessionId": session["sessionId"],
                "fileId": "file",
                "token": token or session["files"]["file"],
            },
            data=content,
        )

    async def test_protocol_failure_does_not_stop_localsend(self):
        status = self.service.snapshot()
        self.assertEqual(
            {p["id"]: p["state"] for p in status["protocols"]},
            {
                "airdrop": "unavailable",
                "quickshare": "unavailable",
                "localsend": "advertising",
            },
        )
        async with self.client.get(self.url + "/info") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["protocol"], "https")

    async def test_consent_upload_and_no_overwrite(self):
        self.directory.mkdir(exist_ok=True)
        (self.directory / "test.txt").write_bytes(b"keep")
        task, request_id = await self.prepare(
            checksum=hashlib.sha256(b"hello").hexdigest()
        )
        self.assertFalse(task.done(), "Data must wait for explicit consent")
        session = await self.approve(task, request_id)
        response = await self.upload(session, b"hello")
        self.assertEqual(response.status, 200)
        self.assertEqual((self.directory / "test.txt").read_bytes(), b"keep")
        self.assertEqual((self.directory / "test (1).txt").read_bytes(), b"hello")
        self.assertEqual(self.service.transfers[request_id]["state"], "complete")
        self.assertFalse(list(self.directory.glob("*.part")))

    async def test_decline(self):
        task, request_id = await self.prepare()
        await self.control.post("/", json={"command": "reject", "id": request_id})
        response = await task
        self.assertEqual(response.status, 403)
        self.assertFalse(self.service.sessions)

    async def test_token_and_checksum(self):
        task, request_id = await self.prepare(checksum="0" * 64)
        session = await self.approve(task, request_id)
        self.assertEqual((await self.upload(session, b"hello", "wrong")).status, 403)
        self.assertEqual((await self.upload(session, b"hello")).status, 422)
        self.assertFalse((self.directory / "test.txt").exists())

    async def test_path_traversal_and_invalid_size(self):
        for name, size in (
            ("../escape.txt", 2),
            ("/tmp/escape.txt", 2),
            ("fine.txt", -1),
        ):
            async with self.client.post(
                self.url + "/prepare-upload",
                json={"files": {"f": {"fileName": name, "size": size}}},
            ) as response:
                self.assertEqual(response.status, 400)
        self.assertFalse(self.service.incoming)

    async def test_cancel_session(self):
        task, request_id = await self.prepare()
        session = await self.approve(task, request_id)
        async with self.client.post(
            self.url + "/cancel", params={"sessionId": request_id}
        ) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual((await self.upload(session, b"hello")).status, 403)
        self.assertEqual(self.service.transfers[request_id]["state"], "cancelled")

    async def test_stop_with_pending_quickshare_consent(self):
        self.service.incoming["quickshare:test"] = {"decision": None, "public": {}}
        await self.service.stop()
        self.assertFalse(self.service.running)

    async def test_real_outgoing_localsend_transfer(self):
        receiver = LocalOnly()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            receiver.port = sock.getsockname()[1]
        receiver.settings["directory"] = self.temp.name + "/second-receiver"
        await receiver.start()
        source = Path(self.temp.name) / "outgoing.txt"
        source.write_bytes(b"Outbound LocalSend transfer")
        peer = {
            "id": "localsend:test-receiver",
            "name": "Test receiver",
            "protocol": "localsend",
            "host": "127.0.0.1",
            "port": receiver.port,
            "scheme": "https",
            "fingerprint": receiver.fingerprint,
        }
        self.service.peers[peer["id"]] = dict(peer, seen=0, deviceType="desktop")
        sending = asyncio.create_task(
            self.service.send_files(peer, [str(source)], "outgoing-test")
        )
        try:
            for _ in range(100):
                if receiver.incoming:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(receiver.incoming)
            next(iter(receiver.incoming.values()))["decision"].set_result(True)
            await asyncio.wait_for(sending, 5)
            self.assertEqual(
                self.service.transfers["outgoing-test"]["state"], "complete"
            )
            self.assertEqual(
                (Path(receiver.settings["directory"]) / "outgoing.txt").read_bytes(),
                source.read_bytes(),
            )
        finally:
            await receiver.stop()

    async def test_unsafe_quickshare_filename_is_rejected_before_consent(self):
        actions = []

        async def record(message):
            actions.append(message)

        self.service.quick_write = record
        self.service.quick_transfer(
            {
                "id": "unsafe",
                "rtype": "Inbound",
                "state": "WaitingForUserConsent",
                "meta": {"files": ["../escape"], "total_bytes": 5},
            }
        )
        await asyncio.sleep(0)
        self.assertFalse(self.service.incoming)
        self.assertEqual(actions[0]["action"], "RejectTransfer")


if __name__ == "__main__":
    unittest.main()
