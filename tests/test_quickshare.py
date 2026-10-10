"""Exercise real Quick Share crypto, consent, transfer and the coordinator.

Run with OMADROP_QUICKSHARE_BIN pointing to the built bridge. Both endpoints
are local test processes, not a claim of Android interoperability.
"""

import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sharing import omadrop


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(
    os.environ.get("OMADROP_QUICKSHARE_BIN"), "Build the Quick Share bridge first"
)
class QuickShareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omadrop-quickshare-test-")
        self.environment = patch.dict(
            os.environ,
            {
                "OMADROP_CONFIG_DIR": self.temp.name + "/config",
                "OMADROP_RUNTIME_DIR": self.temp.name + "/runtime",
                "OMADROP_QUICKSHARE_PORT": "0",
            },
        )
        self.environment.start()
        self.service = omadrop.Coordinator()
        self.service.settings["directory"] = self.temp.name + "/received"
        self.service.running = True
        await self.service.start_quick()
        self.sender_port = unused_port()
        self.sender = await asyncio.create_subprocess_exec(
            os.environ["OMADROP_QUICKSHARE_BIN"],
            self.temp.name,
            str(self.sender_port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.assertEqual(
            json.loads(await asyncio.wait_for(self.sender.stdout.readline(), 10))[
                "event"
            ],
            "ready",
        )
        # Find the coordinator's actual listening port in its owned process.
        output = await omadrop.run_command("ss", "-lntpH")
        rows = [
            line
            for line in output.splitlines()
            if f"pid={self.service.quick.pid}," in line
        ]
        self.assertEqual(len(rows), 1)
        self.receiver_port = int(rows[0].split()[3].rsplit(":", 1)[1])

    async def asyncTearDown(self):
        if self.sender.returncode is None:
            self.sender.stdin.close()
            try:
                await asyncio.wait_for(self.sender.wait(), 5)
            except TimeoutError:
                self.sender.kill()
                await self.sender.wait()
        await self.service.stop()
        self.environment.stop()
        self.temp.cleanup()

    async def send(self, name, data=b"Real Quick Share bytes"):
        source = Path(self.temp.name) / name
        source.write_bytes(data)
        message = {
            "action": "send",
            "id": "test-send",
            "name": "Test receiver",
            "addr": f"127.0.0.1:{self.receiver_port}",
            "files": [str(source)],
        }
        self.sender.stdin.write((json.dumps(message) + "\n").encode())
        await self.sender.stdin.drain()
        for _ in range(500):
            if self.service.incoming:
                return next(iter(self.service.incoming))
            await asyncio.sleep(0.02)
        self.fail("No real Quick Share consent request arrived")

    async def test_real_receive_requires_consent_and_publishes_bytes(self):
        request_id = await self.send("sample.txt")
        pending = self.service.incoming[request_id]["public"]
        self.assertTrue(pending["pin"])
        self.assertEqual(pending["files"][0]["name"], "sample.txt")
        self.assertFalse(
            (Path(self.service.settings["directory"]) / "sample.txt").exists()
        )
        # This is precisely the action used by the native panel's Accept button.
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        app.router.add_post("/", self.service.action)
        control = TestClient(TestServer(app))
        await control.start_server()
        try:
            response = await control.post(
                "/", json={"command": "accept", "id": request_id}
            )
            self.assertEqual(response.status, 200)
            for _ in range(500):
                if (
                    self.service.transfers.get(request_id, {}).get("state")
                    == "complete"
                ):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(self.service.transfers[request_id]["state"], "complete")
            self.assertEqual(
                (Path(self.service.settings["directory"]) / "sample.txt").read_bytes(),
                b"Real Quick Share bytes",
            )
        finally:
            await control.close()


if __name__ == "__main__":
    unittest.main()
