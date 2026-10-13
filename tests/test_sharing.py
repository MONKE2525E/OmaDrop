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

    async def test_airdrop_send_outlives_no_byte_watchdog_without_fake_progress(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        stage_dir = runtime / "clipboard" / "clipboard-airdrop"
        unrelated_dir = runtime / "clipboard" / "clipboard-unrelated"
        stage_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        original = Path(self.temp.name) / "original.bin"
        staged = stage_dir / "clipboard.bin"
        unrelated = unrelated_dir / "keep.bin"
        original.write_bytes(b"original")
        staged.write_bytes(b"staged")
        unrelated.write_bytes(b"unrelated")

        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        arguments = Path(self.temp.name) / "arguments"
        executable = bin_dir / "omdrop"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            f"pathlib.Path({str(arguments)!r}).write_text(' '.join(sys.argv[1:]))\n"
            "for _ in range(12):\n"
            " print('sender operation active', flush=True)\n"
            " time.sleep(0.02)\n"
        )
        executable.chmod(0o700)
        peer = {"protocol": "airdrop", "name": "Apple device", "mac": "aa:bb"}
        key = "airdrop:slow-cli-send"
        observed_bytes = []
        run_calls = []
        real_run_command = omadrop.run_command

        async def record_command(*args, **kwargs):
            run_calls.append((args, kwargs))
            return await real_run_command(*args, **kwargs)

        with patch.dict(
            os.environ,
            {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "OMADROP_RUNTIME_DIR": str(runtime),
            },
        ), patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.05), patch.object(
            omadrop, "run_command", new=record_command
        ):
            started = time.monotonic()
            sending = self.service.spawn(
                self.service.send_files(peer, [str(original), str(staged)], key)
            )
            sending.set_name(key)

            async def monitor():
                while not sending.done():
                    await asyncio.sleep(0.01)
                    await self.service.expire_stalled_transfers()
                    if self.service.transfers[key]["state"] in omadrop.ACTIVE_STATES:
                        observed_bytes.append(self.service.transfers[key]["bytes"])

            monitoring = asyncio.create_task(monitor())
            try:
                await asyncio.wait_for(sending, 3)
            finally:
                monitoring.cancel()
                await asyncio.gather(monitoring, return_exceptions=True)

        self.assertGreater(time.monotonic() - started, 0.05)
        self.assertTrue(observed_bytes)
        self.assertEqual(set(observed_bytes), {0})
        self.assertEqual(len(run_calls), 1)
        self.assertIsNone(run_calls[0][1]["timeout"])
        self.assertIn("--wait 30 --to aa:bb", arguments.read_text())
        self.assertEqual(self.service.transfers[key]["state"], "complete")
        self.assertEqual(self.service.transfers[key]["bytes"], original.stat().st_size + 6)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())
        self.assertTrue(unrelated.exists())

    async def test_cancel_airdrop_cli_send_stops_process_and_cleans_staged_input(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        stage_dir = runtime / "clipboard" / "clipboard-airdrop-cancel"
        unrelated_dir = runtime / "clipboard" / "clipboard-unrelated"
        stage_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        original = Path(self.temp.name) / "original.bin"
        staged = stage_dir / "clipboard.bin"
        unrelated = unrelated_dir / "keep.bin"
        original.write_bytes(b"original")
        staged.write_bytes(b"staged")
        unrelated.write_bytes(b"unrelated")

        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        child_pid_file = Path(self.temp.name) / "child.pid"
        executable = bin_dir / "omdrop"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,signal,subprocess,sys,time\n"
            "child=None\n"
            "def stop(signum, frame):\n"
            " try: child.terminate()\n"
            " except ProcessLookupError: pass\n"
            " try: child.wait(timeout=2)\n"
            " except subprocess.TimeoutExpired:\n"
            "  child.kill(); child.wait()\n"
            " raise SystemExit(128 + signum)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
            "while True: time.sleep(0.1)\n"
        )
        executable.chmod(0o700)
        peer = {"protocol": "airdrop", "name": "Apple device", "mac": "aa:bb"}
        key = "airdrop:cancel-cli-send"

        with patch.dict(
            os.environ,
            {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "OMADROP_RUNTIME_DIR": str(runtime),
            },
        ):
            sending = self.service.spawn(
                self.service.send_files(peer, [str(original), str(staged)], key)
            )
            sending.set_name(key)
            for _ in range(200):
                if child_pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(child_pid_file.exists())
            response = await self.control.post(
                "/", json={"command": "cancel", "id": key}
            )
            self.assertEqual(response.status, 200)
            self.assertTrue(sending.done())

        child_pid = int(child_pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("AirDrop sender child survived cancellation")
        self.assertEqual(self.service.transfers[key]["state"], "cancelled")
        self.assertNotIn(key, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())
        self.assertTrue(unrelated.exists())

    async def test_quickshare_eof_waits_for_process_exit_before_cleaning_cancelled_inputs(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        stage_dir = runtime / "clipboard" / "clipboard-quick-cancel"
        unrelated_dir = runtime / "clipboard" / "clipboard-unrelated"
        stage_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        original = Path(self.temp.name) / "original.bin"
        staged = stage_dir / "clipboard.bin"
        unrelated = unrelated_dir / "keep.bin"
        original.write_bytes(b"original")
        staged.write_bytes(b"staged")
        unrelated.write_bytes(b"unrelated")
        bridge_staging = Path(self.temp.name) / "quickshare-staging"
        bridge_staging.mkdir()
        release = Path(self.temp.name) / "bridge-exit"
        peer = {
            "protocol": "quickshare",
            "name": "Test phone",
            "host": "127.0.0.1",
            "port": 12345,
        }
        key = "quickshare:eof-cancel"

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os,pathlib,sys,time; os.close(1); release=pathlib.Path(sys.argv[1]); "
            "exec('while not release.exists(): time.sleep(0.01)')",
            str(release),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.service.quick = process
        self.service.quick_staging = bridge_staging
        self.service.quick_process_staging[id(process)] = bridge_staging
        self.service.transfers[key] = self.service.outgoing_transfer(
            peer, [str(original), str(staged)], key
        )
        self.service.transfers[key]["state"] = "transferring"
        self.service.outgoing_files[key] = [str(original), str(staged)]
        self.service.quick_transfer_processes[key] = process
        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}):
            events = self.service.spawn(self.service.quick_events(process))
            await asyncio.wait_for(events, 2)
            self.assertIsNone(process.returncode)
            self.assertTrue(staged.exists())
            response = await self.control.post(
                "/", json={"command": "cancel", "id": key}
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(self.service.transfers[key]["state"], "cancelled")
            self.assertTrue(staged.exists())
            release.touch()
            watcher = self.service.quick_exit_watchers[id(process)]
            await asyncio.wait_for(watcher, 2)

        self.assertEqual(self.service.transfers[key]["state"], "cancelled")
        self.assertNotIn(key, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())
        self.assertTrue(unrelated.exists())

    async def test_quickshare_bridge_exit_retires_pending_incoming_approval(self):
        self.directory.mkdir()
        staging = Path(self.temp.name) / "quickshare-incoming"
        staging.mkdir()
        partial = staging / "incoming.bin"
        partial.write_bytes(b"partial")
        process = type("ExitedProcess", (), {"returncode": 0, "pid": 1})()
        self.service.quick = process
        self.service.quick_staging = staging
        self.service.quick_process_staging[id(process)] = staging
        metadata = {"source": {"name": "Test phone"}, "files": [partial.name], "total_bytes": 7}
        self.service.quick_transfer(
            {
                "id": "pending-on-exit",
                "rtype": "Inbound",
                "state": "WaitingForUserConsent",
                "meta": metadata,
            },
            process=process,
        )
        key = "quickshare:pending-on-exit"
        decision = asyncio.get_running_loop().create_future()
        self.service.incoming[key]["decision"] = decision
        self.service.quick_active = key

        self.service.quick_process_exited(process)

        self.assertTrue(decision.done())
        self.assertFalse(decision.result())
        self.assertNotIn(key, self.service.incoming)
        self.assertEqual(self.service.transfers[key]["state"], "failed")
        self.assertFalse(partial.exists())
        self.assertFalse(staging.exists())
        self.assertIsNone(self.service.quick_active)

    async def test_quickshare_stall_restarts_owned_bridge_and_retires_inbound_state(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        starts = Path(self.temp.name) / "bridge-starts"
        cancelled = Path(self.temp.name) / "cancelled-transfer"
        accepted = Path(self.temp.name) / "accepted-transfer"
        executable = Path(self.temp.name) / "fake-quickshare"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,sys\n"
            "starts=pathlib.Path(os.environ['FAKE_QUICKSHARE_STARTS'])\n"
            "count=int(starts.read_text()) if starts.exists() else 0\n"
            "starts.write_text(str(count+1))\n"
            "print(json.dumps({'event':'ready'}),flush=True)\n"
            "for line in sys.stdin:\n"
            " packet=json.loads(line)\n"
            " if packet.get('action')=='CancelTransfer':\n"
            "  pathlib.Path(os.environ['FAKE_QUICKSHARE_CANCEL']).write_text(packet['id'])\n"
            " elif packet.get('action')=='AcceptTransfer':\n"
            "  pathlib.Path(os.environ['FAKE_QUICKSHARE_ACCEPT']).write_text(packet['id'])\n"
        )
        executable.chmod(0o700)
        self.directory.mkdir()
        self.service.quick_path = str(executable)
        self.service.start_quick = lambda: omadrop.Coordinator.start_quick(
            self.service
        )

        with patch.dict(
            os.environ,
            {
                "OMADROP_RUNTIME_DIR": str(runtime),
                "FAKE_QUICKSHARE_STARTS": str(starts),
                "FAKE_QUICKSHARE_CANCEL": str(cancelled),
                "FAKE_QUICKSHARE_ACCEPT": str(accepted),
            },
        ), patch.object(omadrop, "QUICK_CANCEL_ACK_TIMEOUT", 0.05), patch.object(
            omadrop, "TRANSFER_IDLE_TIMEOUT", 0.01
        ):
            await omadrop.Coordinator.start_quick(self.service)
            old_process = self.service.quick
            old_staging = self.service.quick_staging
            partial = old_staging / "incoming.bin"
            unrelated = old_staging / "unrelated.bin"
            partial.write_bytes(b"partial inbound data")
            unrelated.write_bytes(b"preserve this file")

            old_key = "quickshare:stalled-inbound"
            self.service.quick_files[old_key] = [partial.name]
            self.service.quick_transfer_staging[old_key] = old_staging
            self.service.quick_transfer_processes[old_key] = old_process
            self.service.quick_active = old_key
            self.service.transfers[old_key] = omadrop.Transfer({
                "id": old_key,
                "name": partial.name,
                "peer": "Stalled sender",
                "protocol": "quickshare",
                "direction": "in",
                "bytes": 0,
                "total": 100,
                "state": "transferring",
                "error": "",
                "files": 1,
                "fileName": partial.name,
            })
            self.service.transfer_watch[old_key] = (
                ("transferring", 0), time.monotonic() - 1
            )

            await self.service.expire_stalled_transfers()

            new_process = self.service.quick
            self.assertIsNotNone(
                new_process, self.service.channels["quickshare"]["reason"]
            )
            self.assertNotEqual(new_process.pid, old_process.pid)
            self.assertIsNotNone(old_process.returncode)
            self.assertTrue(self.service.running)
            self.assertEqual(starts.read_text(), "2")
            self.assertEqual(cancelled.read_text(), "stalled-inbound")
            self.assertEqual(self.service.transfers[old_key]["state"], "failed")
            self.assertNotIn(old_key, self.service.quick_files)
            self.assertIsNone(self.service.quick_active)
            self.assertFalse(partial.exists())
            self.assertEqual(unrelated.read_bytes(), b"preserve this file")

            new_metadata = {
                "source": {"name": "New sender"},
                "files": ["new.bin"],
                "total_bytes": 4,
            }
            self.service.quick_transfer(
                {
                    "id": "new-inbound",
                    "rtype": "Inbound",
                    "state": "WaitingForUserConsent",
                    "meta": new_metadata,
                },
                process=new_process,
            )
            new_key = "quickshare:new-inbound"
            response = await self.control.post(
                "/", json={"command": "accept", "id": new_key}
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(self.service.quick_active, new_key)
            for _ in range(100):
                if accepted.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(accepted.read_text(), "new-inbound")

            self.service.quick_transfer(
                {
                    "id": "stalled-inbound",
                    "rtype": "Inbound",
                    "state": "Finished",
                    "meta": {
                        "source": {"name": "Stalled sender"},
                        "files": [partial.name],
                        "total_bytes": 100,
                    },
                },
                process=old_process,
            )

        self.assertEqual(self.service.transfers[old_key]["state"], "failed")
        self.assertFalse((self.directory / partial.name).exists())
        self.assertEqual(self.service.quick_active, new_key)
        self.assertIn(new_key, self.service.quick_files)
        self.assertTrue(self.service.quick_staging.exists())

    async def test_quickshare_bridge_exit_preserves_independent_publication(self):
        self.directory.mkdir()
        staging = Path(self.temp.name) / "quickshare-publishing"
        staging.mkdir()
        source = staging / "large.bin"
        expected = b"x" * (1024 * 1024)
        source.write_bytes(expected)
        process = type("ExitedProcess", (), {"returncode": 0, "pid": 1})()
        self.service.quick = process
        self.service.quick_staging = staging
        self.service.quick_process_staging[id(process)] = staging
        metadata = {
            "source": {"name": "Test phone"},
            "files": [source.name],
            "total_bytes": source.stat().st_size,
        }
        self.service.quick_transfer(
            {
                "id": "publish-on-exit",
                "rtype": "Inbound",
                "state": "WaitingForUserConsent",
                "meta": metadata,
            },
            process=process,
        )
        key = "quickshare:publish-on-exit"
        self.service.quick_active = key
        entered_copy = asyncio.Event()
        continue_copy = asyncio.Event()

        async def block_copy():
            entered_copy.set()
            await continue_copy.wait()

        with patch.object(self.service, "quick_publication_checkpoint", new=block_copy):
            self.service.quick_transfer(
                {
                    "id": "publish-on-exit",
                    "rtype": "Inbound",
                    "state": "Finished",
                    "meta": metadata,
                },
                process=process,
            )
            publication = self.service.quick_publications[key]
            await asyncio.wait_for(entered_copy.wait(), 2)
            self.service.quick_process_exited(process)
            self.assertFalse(publication.cancelled())
            self.assertFalse(publication.done())
            self.assertTrue(source.exists())
            self.assertEqual(self.service.quick_active, key)
            continue_copy.set()
            await asyncio.wait_for(publication, 3)

        self.assertEqual(self.service.transfers[key]["state"], "complete")
        self.assertEqual((self.directory / source.name).read_bytes(), expected)
        self.assertFalse(source.exists())
        self.assertFalse(staging.exists())
        self.assertIsNone(self.service.quick_active)

    async def test_slow_quickshare_publication_progress_resets_idle_watchdog(self):
        self.directory.mkdir()
        staging = Path(self.temp.name) / "quickshare-slow-publication"
        staging.mkdir()
        source = staging / "large.bin"
        content = b"x" * (1024 * 1024)
        source.write_bytes(content)
        key = "quickshare:slow-publication"
        self.service.quick_staging = staging
        self.service.quick_files[key] = [source.name]
        self.service.transfers[key] = omadrop.Transfer({
            "id": key, "name": source.name, "fileName": source.name,
            "peer": "Test phone", "protocol": "quickshare", "direction": "in",
            "bytes": 0, "total": len(content), "state": "transferring",
            "error": "", "files": 1,
        })
        self.service.quick_active = key

        async def paced_copy():
            await asyncio.sleep(0.03)

        publication = self.service.spawn_quick_publication(key)

        async def monitor():
            while not publication.done():
                await asyncio.sleep(0.01)
                await self.service.expire_stalled_transfers()

        with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.05), patch.object(
            self.service, "quick_publication_checkpoint", new=paced_copy
        ):
            watcher = asyncio.create_task(monitor())
            try:
                await asyncio.wait_for(publication, 3)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        self.assertEqual(self.service.transfers[key]["state"], "complete")
        self.assertEqual((self.directory / source.name).read_bytes(), content)
        self.assertFalse(source.exists())
        self.assertNotIn(key, self.service.quick_publication_progress)
        self.assertIsNone(self.service.quick_active)

    async def test_stalled_quickshare_publication_cancels_without_touching_destination(self):
        self.directory.mkdir()
        staging = Path(self.temp.name) / "quickshare-stalled-publication"
        staging.mkdir()
        source = staging / "large.bin"
        source.write_bytes(b"x" * (1024 * 1024))
        unrelated = staging / "unrelated.bin"
        unrelated.write_bytes(b"keep staged data")
        destination = self.directory / source.name
        destination.write_bytes(b"keep existing destination")
        key = "quickshare:stalled-publication"
        self.service.quick_staging = staging
        self.service.quick_files[key] = [source.name]
        self.service.transfers[key] = omadrop.Transfer({
            "id": key, "name": source.name, "fileName": source.name,
            "peer": "Test phone", "protocol": "quickshare", "direction": "in",
            "bytes": 0, "total": source.stat().st_size, "state": "transferring",
            "error": "", "files": 1,
        })
        self.service.quick_active = key
        entered_copy = asyncio.Event()

        async def block_copy():
            entered_copy.set()
            await asyncio.Event().wait()

        publication = self.service.spawn_quick_publication(key)

        async def monitor():
            while not publication.done():
                await asyncio.sleep(0.01)
                await self.service.expire_stalled_transfers()

        with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.05), patch.object(
            self.service, "quick_publication_checkpoint", new=block_copy
        ):
            watcher = asyncio.create_task(monitor())
            try:
                await asyncio.wait_for(entered_copy.wait(), 2)
                await asyncio.wait_for(watcher, 2)
            finally:
                if not watcher.done():
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                if not publication.done():
                    publication.cancel()
                    await asyncio.gather(publication, return_exceptions=True)

        self.assertEqual(self.service.transfers[key]["state"], "failed")
        self.assertIn("without progress", self.service.transfers[key]["error"])
        self.assertEqual(destination.read_bytes(), b"keep existing destination")
        self.assertFalse((self.directory / "large (1).bin").exists())
        self.assertFalse(list(self.directory.glob(".omadrop-*.part")))
        self.assertFalse(source.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep staged data")
        self.assertNotIn(key, self.service.quick_publication_progress)
        self.assertIsNone(self.service.quick_active)

    async def test_stale_quickshare_process_exit_does_not_touch_replacement(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        old_stage_dir = runtime / "clipboard" / "clipboard-old-bridge"
        new_stage_dir = runtime / "clipboard" / "clipboard-new-bridge"
        old_stage_dir.mkdir(parents=True)
        new_stage_dir.mkdir()
        old_original = Path(self.temp.name) / "old-original.bin"
        old_staged = old_stage_dir / "clipboard.bin"
        new_original = Path(self.temp.name) / "new-original.bin"
        new_staged = new_stage_dir / "clipboard.bin"
        old_original.write_bytes(b"old original")
        old_staged.write_bytes(b"old staged")
        new_original.write_bytes(b"new original")
        new_staged.write_bytes(b"new staged")
        old_bridge_staging = Path(self.temp.name) / "old-bridge-staging"
        new_bridge_staging = Path(self.temp.name) / "new-bridge-staging"
        old_bridge_staging.mkdir()
        new_bridge_staging.mkdir()
        old_process = type("ExitedProcess", (), {"returncode": 0, "pid": 11})()
        new_process = type("ActiveProcess", (), {"returncode": None, "pid": 12})()
        self.service.quick = new_process
        self.service.quick_staging = new_bridge_staging
        self.service.quick_process_staging[id(old_process)] = old_bridge_staging
        self.service.quick_process_staging[id(new_process)] = new_bridge_staging
        self.service.channel("quickshare", "advertising")
        self.service.peers["quickshare:new-peer"] = {"id": "quickshare:new-peer"}
        old_key = "quickshare:old-send"
        new_key = "quickshare:new-send"
        peer = {"protocol": "quickshare", "name": "Test phone"}
        self.service.transfers[old_key] = self.service.outgoing_transfer(
            peer, [str(old_original), str(old_staged)], old_key
        )
        self.service.transfers[old_key]["state"] = "cancelled"
        self.service.outgoing_files[old_key] = [str(old_original), str(old_staged)]
        self.service.quick_transfer_processes[old_key] = old_process
        self.service.transfers[new_key] = self.service.outgoing_transfer(
            peer, [str(new_original), str(new_staged)], new_key
        )
        self.service.transfers[new_key]["state"] = "transferring"
        self.service.outgoing_files[new_key] = [str(new_original), str(new_staged)]
        self.service.quick_transfer_processes[new_key] = new_process

        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}):
            self.service.quick_process_exited(old_process)

        self.assertIs(self.service.quick, new_process)
        self.assertEqual(self.service.channels["quickshare"]["state"], "advertising")
        self.assertIn("quickshare:new-peer", self.service.peers)
        self.assertNotIn(old_key, self.service.outgoing_files)
        self.assertFalse(old_staged.exists())
        self.assertTrue(old_original.exists())
        self.assertIn(new_key, self.service.outgoing_files)
        self.assertIs(
            self.service.quick_transfer_processes[new_key], new_process
        )
        self.assertTrue(new_staged.exists())
        self.assertTrue(new_original.exists())
        self.assertTrue(new_bridge_staging.exists())
        self.service.quick = None

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
                await omadrop.Coordinator.discover_local(self.service, ["127.0.0.1"])
            peer = self.service.peers["localsend:test-phone"]
            self.assertEqual(peer["host"], "127.0.0.1")
            self.assertEqual(peer["scheme"], "http")
            self.assertEqual(peer["name"], "Test phone")
        finally:
            await server.close()

    async def test_https_register_uses_listening_certificate_instead_of_json_identity(self):
        # Model LocalSend's allowed omission of fingerprint in HTTPS register.
        actual_fingerprint = self.service.fingerprint
        self.service.fingerprint = "different-local-identity"
        response = await self.client.post(self.url + "/register", json={
            "alias": "Test peer", "version": "2.2", "protocol": "https",
            "port": self.port, "fingerprint": "untrusted-json-identity",
        })
        self.assertEqual(response.status, 200)
        self.assertIn("localsend:" + actual_fingerprint, self.service.peers)
        self.assertNotIn("localsend:untrusted-json-identity", self.service.peers)

    async def test_multicast_socket_failure_keeps_https_receiver_available(self):
        await self.service.stop()
        real_socket = socket.socket

        def socket_with_unavailable_multicast(*args, **kwargs):
            sock = real_socket(*args, **kwargs)
            if len(args) > 1 and args[1] == socket.SOCK_DGRAM:
                from unittest.mock import Mock
                blocked = Mock(wraps=sock)
                blocked.bind.side_effect = OSError("Multicast port unavailable")
                return blocked
            return sock

        with patch.object(omadrop.socket, "socket", new=socket_with_unavailable_multicast):
            await self.service.start()
        self.assertIsNone(self.service.udp)
        self.assertEqual(self.service.channels["localsend"]["state"], "advertising")
        self.assertIn("LAN discovery", self.service.channels["localsend"]["reason"])
        async with self.client.get(self.url + "/info") as response:
            self.assertEqual(response.status, 200)

    async def test_start_publishes_menu_state_and_runs_protocol_setup_concurrently(self):
        await self.service.stop()
        entered = set()
        all_entered = asyncio.Event()
        release = asyncio.Event()

        async def start_channel(key):
            entered.add(key)
            if len(entered) == 3:
                all_entered.set()
            await release.wait()
            self.service.channel(key, "advertising")

        with patch.object(self.service, "start_local", new=lambda: start_channel("localsend")), patch.object(
            self.service, "start_quick", new=lambda: start_channel("quickshare")
        ), patch.object(self.service, "start_airdrop", new=lambda: start_channel("airdrop")):
            task = asyncio.create_task(self.control.post("/", json={
                "command": "lease", "leaseId": "starting-test", "durationSeconds": 30,
            }))
            try:
                await asyncio.wait_for(all_entered.wait(), 2)
                status = self.service.snapshot()
                self.assertEqual(status["mode"], "menu")
                self.assertTrue(status["starting"])
            finally:
                release.set()
            response = await task
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["starting"])

    async def test_stalled_protocol_setup_is_bounded_without_hiding_ready_channels(self):
        await self.service.stop()

        async def ready(key):
            self.service.channel(key, "advertising")

        async def stalled():
            await asyncio.Event().wait()

        with patch.object(omadrop, "PROTOCOL_STARTUP_TIMEOUT", 0.05), patch.object(
            self.service, "start_local", new=lambda: ready("localsend")
        ), patch.object(self.service, "start_airdrop", new=lambda: ready("airdrop")), patch.object(
            self.service, "start_quick", new=stalled
        ):
            response = await asyncio.wait_for(self.control.post("/", json={
                "command": "lease", "leaseId": "bounded-start", "durationSeconds": 30,
            }), 2)
        status = await response.json()
        self.assertEqual(status["mode"], "menu")
        self.assertFalse(status["starting"])
        channels = {row["id"]: row for row in status["protocols"]}
        self.assertEqual(channels["localsend"]["state"], "advertising")
        self.assertEqual(channels["quickshare"]["state"], "unavailable")
        self.assertEqual(channels["quickshare"]["reason"], "Startup timed out")

    async def test_cancelled_startup_cleans_started_resources_and_states(self):
        await self.service.stop()
        waiting = asyncio.Event()

        async def stalled():
            waiting.set()
            await asyncio.Event().wait()

        with patch.object(self.service, "start_quick", new=stalled):
            starting = asyncio.create_task(self.service.start())
            await asyncio.wait_for(waiting.wait(), 2)
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        self.assertTrue(starting.cancelled())
        self.assertFalse(self.service.running)
        self.assertFalse(self.service.starting)
        self.assertIsNone(self.service.client)
        self.assertIsNone(self.service.local_runner)
        self.assertTrue(all(row["state"] == "stopped" for row in self.service.channels.values()))

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

    async def test_cancel_quickshare_publication_removes_only_its_partial_output(self):
        self.directory.mkdir()
        self.service.quick_staging = Path(self.temp.name) / "quick-staging"
        self.service.quick_staging.mkdir()
        key = "quickshare:cancel-mid-copy"
        filename = "large.bin"
        source = self.service.quick_staging / filename
        source.write_bytes(b"x" * (1024 * 1024))
        unrelated = self.service.quick_staging / "other-transfer.bin"
        unrelated.write_bytes(b"keep")
        metadata = {
            "source": {"name": "Test phone"},
            "files": [filename],
            "total_bytes": source.stat().st_size,
        }
        self.service.quick_transfer({
            "id": "cancel-mid-copy", "rtype": "Inbound",
            "state": "WaitingForUserConsent", "meta": metadata,
        })
        entered_copy = asyncio.Event()

        async def block_copy():
            entered_copy.set()
            await asyncio.Event().wait()

        with patch.object(self.service, "quick_write", new=AsyncMock()) as quick_write, patch.object(
            self.service, "quick_publication_checkpoint", new=block_copy
        ):
            response = await self.control.post("/", json={"command": "accept", "id": key})
            self.assertEqual(response.status, 200)
            self.service.quick_transfer({
                "id": "cancel-mid-copy", "rtype": "Inbound",
                "state": "Finished", "meta": metadata,
            })
            await asyncio.wait_for(entered_copy.wait(), 2)
            partials = list(self.directory.glob(".omadrop-*.part"))
            self.assertEqual(len(partials), 1)
            self.assertGreater(partials[0].stat().st_size, 0)

            self.service.quick_transfer({
                "id": "cancel-mid-copy", "rtype": "Inbound",
                "state": "Disconnected", "meta": metadata,
            })
            self.assertEqual(self.service.transfers[key]["state"], "transferring")
            self.assertEqual(unrelated.read_bytes(), b"keep")

            response = await self.control.post("/", json={"command": "cancel", "id": key})
            self.assertEqual(response.status, 200)
            self.assertEqual(self.service.transfers[key]["state"], "cancelled")
            self.assertIsNone(self.service.quick_active)
            self.assertNotIn(key, self.service.quick_publications)
            self.assertFalse(source.exists())
            self.assertFalse(list(self.directory.glob(".omadrop-*.part")))
            self.assertFalse((self.directory / filename).exists())
            self.assertEqual(unrelated.read_bytes(), b"keep")
            quick_write.assert_awaited_with({"action": "CancelTransfer", "id": "cancel-mid-copy"})

        self.service.quick_transfer({
            "id": "cancel-mid-copy", "rtype": "Inbound",
            "state": "Disconnected", "meta": metadata,
        })
        self.assertEqual(self.service.transfers[key]["state"], "cancelled")
        self.assertEqual(unrelated.read_bytes(), b"keep")

    async def test_quickshare_dispatch_failure_cleans_only_staged_inputs(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        clipboard = runtime / "clipboard"
        failed_dir = clipboard / "clipboard-failed"
        unrelated_dir = clipboard / "clipboard-other"
        failed_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        original = Path(self.temp.name) / "original.jpg"
        staged = failed_dir / "clipboard.jpg"
        unrelated = unrelated_dir / "other.jpg"
        original.write_bytes(b"original")
        staged.write_bytes(b"staged clipboard copy")
        unrelated.write_bytes(b"unrelated staged copy")
        key = "quickshare:dispatch-failed"
        peer = {
            "protocol": "quickshare", "name": "Test phone",
            "host": "127.0.0.1", "port": 12345,
        }

        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}), patch.object(
            self.service, "quick_write", new=AsyncMock(side_effect=RuntimeError("bridge stopped"))
        ):
            await self.service.send_files(peer, [str(original), str(staged)], key)

        self.assertEqual(self.service.transfers[key]["state"], "failed")
        self.assertNotIn(key, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated staged copy")

    async def test_quickshare_success_keeps_staged_inputs_until_engine_event(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        stage_dir = runtime / "clipboard" / "clipboard-success"
        stage_dir.mkdir(parents=True)
        original = Path(self.temp.name) / "original.txt"
        staged = stage_dir / "clipboard.txt"
        original.write_text("user original")
        staged.write_text("staged copy")
        key = "quickshare:dispatch-success"
        peer = {
            "protocol": "quickshare", "name": "Test phone",
            "host": "127.0.0.1", "port": 12345,
        }

        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}), patch.object(
            self.service, "quick_write", new=AsyncMock()
        ):
            await self.service.send_files(peer, [str(original), str(staged)], key)
            self.assertIn(key, self.service.outgoing_files)
            self.assertTrue(staged.exists())
            self.service.quick_transfer({
                "id": "dispatch-success", "rtype": "Outbound", "state": "Finished",
                "meta": {"source": {"name": "Test phone"}, "files": [staged.name]},
            })

        self.assertNotIn(key, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())

    async def test_quickshare_cancel_dispatch_failure_cleans_only_staged_inputs(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        failed_dir = runtime / "clipboard" / "clipboard-cancel-failed"
        unrelated_dir = runtime / "clipboard" / "clipboard-unrelated"
        failed_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        original = Path(self.temp.name) / "original.jpg"
        staged = failed_dir / "clipboard.jpg"
        unrelated = unrelated_dir / "other.jpg"
        original.write_bytes(b"user original")
        staged.write_bytes(b"staged clipboard copy")
        unrelated.write_bytes(b"unrelated staged copy")
        peer = {
            "id": "quickshare:test-phone", "name": "Test phone",
            "protocol": "quickshare", "host": "127.0.0.1", "port": 12345,
        }
        self.service.peers[peer["id"]] = peer
        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}):
            with patch.object(
                self.service, "quick_write", new=AsyncMock()
            ) as quick_write:
                response = await self.control.post("/", json={
                    "command": "send", "peer": peer["id"],
                    "files": [str(original), str(staged)],
                })
                self.assertEqual(response.status, 200)
                transfer_id = (await response.json())["transferId"]
                for _ in range(100):
                    if quick_write.await_count:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(quick_write.await_count, 1)
                await asyncio.sleep(0)
                self.assertIn(transfer_id, self.service.outgoing_files)

            self.service.quick_transfer({
                "id": transfer_id.split(":", 1)[1], "rtype": "Outbound",
                "state": "SendingFiles",
                "meta": {"source": {"name": "Test phone"}, "files": [staged.name]},
            })
            self.assertEqual(self.service.transfers[transfer_id]["state"], "transferring")
            self.service.quick = None  # The bridge exited before CancelTransfer.

            response = await self.control.post("/", json={
                "command": "cancel", "id": transfer_id,
            })
            error = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(error, {"error": "Quick Share is not running"})
        self.assertEqual(self.service.transfers[transfer_id]["state"], "failed")
        self.assertEqual(self.service.transfers[transfer_id]["error"], error["error"])
        self.assertNotIn(transfer_id, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated staged copy")

    async def test_quickshare_cancel_waits_for_terminal_event_after_dispatch(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        staged_dir = runtime / "clipboard" / "clipboard-cancel-success"
        staged_dir.mkdir(parents=True)
        original = Path(self.temp.name) / "original.txt"
        staged = staged_dir / "clipboard.txt"
        original.write_text("user original")
        staged.write_text("staged copy")
        peer = {
            "id": "quickshare:test-phone", "name": "Test phone",
            "protocol": "quickshare", "host": "127.0.0.1", "port": 12345,
        }
        self.service.peers[peer["id"]] = peer
        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}), patch.object(
            self.service, "quick_write", new=AsyncMock()
        ) as quick_write:
            response = await self.control.post("/", json={
                "command": "send", "peer": peer["id"], "files": [str(staged)],
            })
            self.assertEqual(response.status, 200)
            transfer_id = (await response.json())["transferId"]
            for _ in range(100):
                if quick_write.await_count:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(quick_write.await_count, 1)
            await asyncio.sleep(0)
            self.assertIn(transfer_id, self.service.outgoing_files)
            self.service.quick_transfer({
                "id": transfer_id.split(":", 1)[1], "rtype": "Outbound",
                "state": "SendingFiles",
                "meta": {"source": {"name": "Test phone"}, "files": [staged.name]},
            })

            response = await self.control.post("/", json={
                "command": "cancel", "id": transfer_id,
            })
            self.assertEqual(response.status, 200)
            self.assertEqual(self.service.transfers[transfer_id]["state"], "cancelled")
            self.assertIn(transfer_id, self.service.outgoing_files)
            self.assertTrue(staged.exists())
            self.service.quick_transfer({
                "id": transfer_id.split(":", 1)[1], "rtype": "Outbound",
                "state": "Cancelled",
                "meta": {"source": {"name": "Test phone"}, "files": [staged.name]},
            })

        self.assertNotIn(transfer_id, self.service.outgoing_files)
        self.assertTrue(original.exists())
        self.assertFalse(staged.exists())

    async def test_quickshare_cancel_failure_keeps_inbound_staging_while_bridge_runs(self):
        self.service.quick_staging = Path(self.temp.name) / "quick-staging"
        self.service.quick_staging.mkdir()
        key = "quickshare:inbound-cancel"
        filename = "incoming.bin"
        staged = self.service.quick_staging / filename
        staged.write_bytes(b"engine-owned bytes")
        self.service.quick_files[key] = [filename]
        self.service.quick_active = key
        self.service.quick_transfer({
            "id": "inbound-cancel", "rtype": "Inbound", "state": "ReceivingFiles",
            "meta": {"source": {"name": "Test phone"}, "files": [filename]},
        })

        class RunningBridge:
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()

        self.service.quick = RunningBridge()
        try:
            with patch.object(
                self.service, "quick_write",
                new=AsyncMock(side_effect=RuntimeError("bridge input failed")),
            ):
                response = await self.control.post("/", json={
                    "command": "cancel", "id": key,
                })
                error = await response.json()
        finally:
            self.service.quick = None

        self.assertEqual(response.status, 400)
        self.assertEqual(error, {"error": "bridge input failed"})
        self.assertEqual(self.service.transfers[key]["state"], "transferring")
        self.assertEqual(self.service.quick_files[key], [filename])
        self.assertEqual(self.service.quick_active, key)
        self.assertEqual(staged.read_bytes(), b"engine-owned bytes")

    async def test_quickshare_cancel_failure_cleans_inbound_staging_after_bridge_stops(self):
        self.service.quick_staging = Path(self.temp.name) / "quick-staging"
        self.service.quick_staging.mkdir()
        key = "quickshare:inbound-stopped"
        filename = "incoming.bin"
        staged = self.service.quick_staging / filename
        staged.write_bytes(b"engine-owned bytes")
        self.service.quick_files[key] = [filename]
        self.service.quick_active = key
        self.service.quick_transfer({
            "id": "inbound-stopped", "rtype": "Inbound", "state": "ReceivingFiles",
            "meta": {"source": {"name": "Test phone"}, "files": [filename]},
        })

        response = await self.control.post("/", json={
            "command": "cancel", "id": key,
        })
        error = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(error, {"error": "Quick Share is not running"})
        self.assertEqual(self.service.transfers[key]["state"], "failed")
        self.assertEqual(self.service.transfers[key]["error"], error["error"])
        self.assertNotIn(key, self.service.quick_files)
        self.assertIsNone(self.service.quick_active)
        self.assertFalse(staged.exists())

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

    async def test_airdrop_zero_byte_heartbeats_reset_idle_watchdog_without_fake_progress(self):
        task = asyncio.create_task(self.control.post("/", json={
            "command": "receive-request", "peer": "Test Mac",
            "files": [{"name": "large.zip", "size": 10}],
        }))
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        key = next(iter(self.service.incoming))
        await self.control.post("/", json={"command": "accept", "id": key})
        approval = await (await task).json()
        heartbeat = {
            "command": "receive-progress", "id": key,
            "token": approval["token"], "bytes": 0, "state": "transferring",
        }

        with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.12):
            await self.service.expire_stalled_transfers()
            for _ in range(5):
                await asyncio.sleep(0.04)
                response = await self.control.post("/", json=heartbeat)
                self.assertEqual(response.status, 200)
                await self.service.expire_stalled_transfers()
                self.assertEqual(self.service.transfers[key]["state"], "transferring")
                self.assertEqual(self.service.transfers[key]["bytes"], 0)

            await asyncio.sleep(0.14)
            await self.service.expire_stalled_transfers()

        self.assertEqual(self.service.transfers[key]["state"], "failed")
        self.assertNotIn(key, self.service.airdrop_progress)

    async def test_invalid_airdrop_progress_does_not_reset_idle_watchdog(self):
        for invalid in ("wrong-token", "malformed"):
            with self.subTest(invalid=invalid):
                task = asyncio.create_task(self.control.post("/", json={
                    "command": "receive-request", "peer": "Test Mac",
                    "files": [{"name": "large.zip", "size": 10}],
                }))
                for _ in range(100):
                    if self.service.incoming:
                        break
                    await asyncio.sleep(0.01)
                key = next(iter(self.service.incoming))
                await self.control.post("/", json={"command": "accept", "id": key})
                approval = await (await task).json()
                packet = {
                    "command": "receive-progress", "id": key,
                    "token": approval["token"], "bytes": 0,
                    "state": "transferring",
                }
                if invalid == "wrong-token":
                    packet["token"] = "invalid"
                else:
                    packet["state"] = "extracting"

                with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.2):
                    await self.service.expire_stalled_transfers()
                    await asyncio.sleep(0.12)
                    response = await self.control.post("/", json=packet)
                    self.assertEqual(response.status, 400)
                    await asyncio.sleep(0.1)
                    await self.service.expire_stalled_transfers()

                self.assertEqual(self.service.transfers[key]["state"], "failed")
                self.assertNotIn(key, self.service.airdrop_progress)

    async def test_cancel_airdrop_revokes_progress_and_receiver_poll_is_rejected(self):
        request = asyncio.create_task(self.control.post("/", json={
            "command": "receive-request", "peer": "Test Mac",
            "files": [{"name": "test.txt", "size": 4}],
        }))
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        key = next(iter(self.service.incoming))
        await self.control.post("/", json={"command": "accept", "id": key})
        approval = await (await request).json()
        self.assertIn(key, self.service.airdrop_progress)

        response = await self.control.post("/", json={"command": "cancel", "id": key})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.service.transfers[key]["state"], "cancelled")
        self.assertNotIn(key, self.service.airdrop_progress)

        response = await self.control.post("/", json={
            "command": "receive-progress", "id": key, "token": approval["token"],
            "bytes": 1, "state": "transferring",
        })
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "Receive session cancelled")

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
        self.service.menu_leases[omadrop.LEGACY_MENU_LEASE_ID] = time.time() - 1
        await self.service.expire_window()
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.snapshot()["mode"], "off")

    async def test_menu_leases_release_only_the_calling_panel(self):
        for lease_id in ("panel-one", "panel-two"):
            response = await self.control.post("/", json={
                "command": "lease", "durationSeconds": 30, "leaseId": lease_id,
            })
            self.assertEqual(response.status, 200)
        response = await self.control.post("/", json={
            "command": "release", "leaseId": "panel-one",
        })
        self.assertEqual(response.status, 200)
        self.assertTrue(self.service.running)
        self.assertEqual(set(self.service.menu_leases), {"panel-two"})
        self.assertEqual(self.service.snapshot()["mode"], "menu")

        response = await self.control.post("/", json={"command": "release"})
        self.assertEqual(response.status, 200)
        self.assertTrue(self.service.running)
        self.assertEqual(set(self.service.menu_leases), {"panel-two"})
        response = await self.control.post("/", json={
            "command": "release", "leaseId": "panel-two",
        })
        self.assertEqual(response.status, 200)
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.menu_leases, {})

    async def test_menu_lease_ids_are_validated_and_the_lease_map_is_bounded(self):
        for lease_id in ("legacy", "../bad", "x" * 65, None):
            response = await self.control.post("/", json={
                "command": "lease", "durationSeconds": 30, "leaseId": lease_id,
            })
            self.assertEqual(response.status, 400)
        with patch.object(omadrop, "MAX_MENU_LEASES", 2):
            for lease_id in ("first", "second"):
                response = await self.control.post("/", json={
                    "command": "lease", "durationSeconds": 30,
                    "leaseId": lease_id,
                })
                self.assertEqual(response.status, 200)
            response = await self.control.post("/", json={
                "command": "lease", "durationSeconds": 30,
                "leaseId": "third",
            })
        self.assertEqual(response.status, 400)
        self.assertEqual(set(self.service.menu_leases), {"first", "second"})

    async def test_expired_last_menu_lease_stops_and_does_not_survive_daemon_restart(self):
        response = await self.control.post("/", json={
            "command": "lease", "durationSeconds": 30, "leaseId": "panel-one",
        })
        self.assertEqual(response.status, 200)
        timer = self.service.window_timer
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        self.service.menu_leases["panel-one"] = time.time() - 1
        await self.service.expire_window()
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.snapshot()["mode"], "off")
        restarted = omadrop.Coordinator()
        self.assertEqual(restarted.menu_leases, {})

    async def test_dismiss_does_not_remove_active_transfers(self):
        self.service.transfers.update({
            "done": omadrop.Transfer(id="done", state="complete"),
            "active": omadrop.Transfer(id="active", state="waiting"),
        })
        await self.control.post("/", json={"command": "dismiss", "id": "all"})
        self.assertNotIn("done", self.service.transfers)
        self.assertIn("active", self.service.transfers)
        self.assertEqual(omadrop.Coordinator().transfers, {})

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

    async def test_cancel_outgoing_airdrop_stops_send_and_cleans_staged_files(self):
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        clipboard = runtime / "clipboard"
        staged_dir = clipboard / "clipboard-airdrop"
        unrelated_dir = clipboard / "clipboard-unrelated"
        staged_dir.mkdir(parents=True)
        unrelated_dir.mkdir()
        staged_file = staged_dir / "clipboard.txt"
        unrelated_file = unrelated_dir / "keep.txt"
        staged_file.write_text("staged content")
        unrelated_file.write_text("keep this")
        peer = {
            "id": "airdrop:test", "name": "Test Mac", "protocol": "airdrop",
            "mac": "test",
        }
        self.service.peers[peer["id"]] = peer
        send_started = asyncio.Event()
        send_cancelled = asyncio.Event()

        async def blocked_send(*args, **kwargs):
            send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                send_cancelled.set()
                raise

        with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": str(runtime)}), patch.object(
            omadrop, "run_command", new=blocked_send
        ):
            response = await self.control.post("/", json={
                "command": "send", "peer": peer["id"], "files": [str(staged_file)],
            })
            data = await response.json()
            self.assertEqual(response.status, 200)
            transfer_id = data["transferId"]
            task = next(task for task in self.service.tasks if task.get_name() == transfer_id)
            await asyncio.wait_for(send_started.wait(), 2)
            self.assertEqual(self.service.transfers[transfer_id]["state"], "transferring")

            response = await self.control.post("/", json={
                "command": "cancel", "id": transfer_id,
            })
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(send_cancelled.wait(), 2)
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(task.cancelled())
        self.assertEqual(self.service.transfers[transfer_id]["state"], "cancelled")
        self.assertNotIn(transfer_id, self.service.outgoing_files)
        self.assertFalse(staged_file.exists())
        self.assertTrue(unrelated_file.exists())

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

    async def upload(self, session, content, token=None, file_id="file"):
        return await self.client.post(
            self.url + "/upload",
            params={
                "sessionId": session["sessionId"],
                "fileId": file_id,
                "token": token or session["files"][file_id],
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

    async def test_progress_refreshes_upload_and_session_idle_timeouts(self):
        content = b"0123456789"
        task, request_id = await self.prepare(content=content)
        session = await self.approve(task, request_id)
        first_chunk = asyncio.Event()

        async def slow_chunks():
            for index, byte in enumerate(content):
                await asyncio.sleep(0.02)
                if index == 0:
                    first_chunk.set()
                yield bytes((byte,))

        async def expire_sessions_while_uploading():
            while not upload.done():
                await asyncio.sleep(0.02)
                self.service.expire_localsend_sessions()

        started = time.monotonic()
        with patch.object(omadrop, "LOCALSEND_UPLOAD_IDLE_TIMEOUT", 0.06), patch.object(
            omadrop, "LOCALSEND_SESSION_IDLE_TIMEOUT", 0.07
        ):
            upload = asyncio.create_task(self.upload(session, slow_chunks()))
            expirer = asyncio.create_task(expire_sessions_while_uploading())
            try:
                await asyncio.wait_for(first_chunk.wait(), 1)
                response = await asyncio.wait_for(upload, 2)
            finally:
                expirer.cancel()
                await asyncio.gather(expirer, return_exceptions=True)

        self.assertGreater(time.monotonic() - started, 0.07)
        self.assertEqual(response.status, 200)
        self.assertEqual((self.directory / "test.txt").read_bytes(), content)
        self.assertEqual(self.service.transfers[request_id]["state"], "complete")
        self.assertNotIn(request_id, self.service.sessions)
        self.assertFalse(list(self.directory.glob(".omadrop-*.part")))

    async def test_stalled_upload_hits_idle_timeout_and_removes_partial_file(self):
        task, request_id = await self.prepare(content=b"hello")
        session = await self.approve(task, request_id)
        first_chunk = asyncio.Event()
        continue_upload = asyncio.Event()

        async def stalled_chunks():
            yield b"he"
            first_chunk.set()
            await continue_upload.wait()
            yield b"llo"

        with patch.object(omadrop, "LOCALSEND_UPLOAD_IDLE_TIMEOUT", 0.04):
            upload = asyncio.create_task(self.upload(session, stalled_chunks()))
            try:
                await asyncio.wait_for(first_chunk.wait(), 1)
                for _ in range(100):
                    if list(self.directory.glob(".omadrop-*.part")):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(list(self.directory.glob(".omadrop-*.part")))
                await asyncio.sleep(0.06)
                continue_upload.set()
                response = await asyncio.wait_for(upload, 2)
            finally:
                continue_upload.set()

        self.assertEqual(response.status, 408)
        self.assertEqual(self.service.transfers[request_id]["state"], "failed")
        self.assertTrue(self.service.transfers[request_id]["error"])
        self.assertFalse(list(self.directory.glob(".omadrop-*.part")))
        self.assertFalse((self.directory / "test.txt").exists())

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
        self.assertNotIn(request_id, self.service.sessions)

    async def test_failed_localsend_uploads_release_slots_and_partial_files(self):
        self.directory.mkdir(exist_ok=True)
        unrelated = self.directory / "unrelated.txt"
        unrelated.write_bytes(b"keep")
        failure_kinds = ("checksum", "truncated", "oversized", "idle")

        for index in range(20):
            kind = failure_kinds[index % len(failure_kinds)]
            checksum = "0" * 64 if kind == "checksum" else None
            task, request_id = await self.prepare(content=b"hello", checksum=checksum)
            session = await self.approve(task, request_id)

            if kind == "checksum":
                response = await self.upload(session, b"hello")
                expected_status = 422
            elif kind == "truncated":
                response = await self.upload(session, b"he")
                expected_status = 422
            elif kind == "oversized":
                response = await self.upload(session, b"hello!")
                expected_status = 400
            else:
                async def stalled_chunks():
                    yield b"he"
                    await asyncio.sleep(0.04)
                    yield b"llo"

                with patch.object(omadrop, "LOCALSEND_UPLOAD_IDLE_TIMEOUT", 0.01):
                    response = await self.upload(session, stalled_chunks())
                expected_status = 408

            self.assertEqual(response.status, expected_status, kind)
            self.assertNotIn(request_id, self.service.sessions, kind)
            self.assertEqual(self.service.transfers[request_id]["state"], "failed", kind)
            self.assertFalse(list(self.directory.glob(".omadrop-*.part")), kind)

        task, request_id = await self.prepare()
        session = await self.approve(task, request_id)
        response = await self.upload(session, b"hello")
        self.assertEqual(response.status, 200)
        self.assertEqual((self.directory / "test.txt").read_bytes(), b"hello")
        self.assertEqual(unrelated.read_bytes(), b"keep")

    async def test_localsend_failure_cancels_concurrent_files_without_overwriting_failure(self):
        metadata = {
            "bad": {
                "id": "bad", "fileName": "bad.txt", "size": 4,
                "fileType": "text/plain", "sha256": "0" * 64,
            },
            "good": {
                "id": "good", "fileName": "good.txt", "size": 5,
                "fileType": "text/plain", "sha256": hashlib.sha256(b"hello").hexdigest(),
            },
        }
        task = asyncio.create_task(self.client.post(
            self.url + "/prepare-upload",
            json={"info": {"alias": "Test sender"}, "files": metadata},
        ))
        for _ in range(100):
            if self.service.incoming:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.service.incoming)
        request_id = next(iter(self.service.incoming))
        session = await self.approve(task, request_id)
        first_chunk = asyncio.Event()
        continue_upload = asyncio.Event()

        async def slow_good_file():
            yield b"he"
            first_chunk.set()
            await continue_upload.wait()
            yield b"llo"

        good_upload = asyncio.create_task(
            self.upload(session, slow_good_file(), file_id="good")
        )
        try:
            await asyncio.wait_for(first_chunk.wait(), 2)
            for _ in range(100):
                if list(self.directory.glob(".omadrop-*.part")):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(list(self.directory.glob(".omadrop-*.part"))), 1)
            bad_response = await self.upload(session, b"bad!", file_id="bad")
            self.assertEqual(bad_response.status, 422)
            self.assertNotIn(request_id, self.service.sessions)
            self.assertEqual(self.service.transfers[request_id]["state"], "failed")
            first_error = self.service.transfers[request_id]["error"]
            self.assertTrue(first_error)
        finally:
            continue_upload.set()

        good_response = await asyncio.wait_for(good_upload, 3)
        self.assertEqual(good_response.status, 403)
        self.assertEqual(self.service.transfers[request_id]["state"], "failed")
        self.assertEqual(self.service.transfers[request_id]["error"], first_error)
        self.assertFalse((self.directory / "bad.txt").exists())
        self.assertFalse((self.directory / "good.txt").exists())
        self.assertFalse(list(self.directory.glob(".omadrop-*.part")))

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

    async def test_cancelled_localsend_sessions_do_not_consume_the_session_limit(self):
        for _ in range(20):
            task, request_id = await self.prepare()
            await self.approve(task, request_id)
            self.assertIn(request_id, self.service.sessions)
            response = await self.control.post(
                "/", json={"command": "cancel", "id": request_id}
            )
            self.assertEqual(response.status, 200)
            self.assertNotIn(request_id, self.service.sessions)
            self.assertEqual(self.service.transfers[request_id]["state"], "cancelled")

    async def test_localsend_cancel_during_upload_removes_only_its_partial_file(self):
        task, request_id = await self.prepare()
        session = await self.approve(task, request_id)
        first_chunk_sent = asyncio.Event()
        send_rest = asyncio.Event()

        async def chunks():
            yield b"he"
            first_chunk_sent.set()
            await send_rest.wait()
            yield b"llo"

        upload = asyncio.create_task(self.upload(session, chunks()))
        try:
            await asyncio.wait_for(first_chunk_sent.wait(), 2)
            for _ in range(100):
                partials = list(self.directory.glob(".omadrop-*.part"))
                if partials:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(partials), 1)

            response = await self.control.post(
                "/", json={"command": "cancel", "id": request_id}
            )
            self.assertEqual(response.status, 200)
            self.assertNotIn(request_id, self.service.sessions)
        finally:
            send_rest.set()

        response = await asyncio.wait_for(upload, 3)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.service.transfers[request_id]["state"], "cancelled")
        self.assertFalse(list(self.directory.glob(".omadrop-*.part")))
        self.assertFalse((self.directory / "test.txt").exists())

    async def test_stop_with_pending_quickshare_consent(self):
        self.service.incoming["quickshare:test"] = {"decision": None, "public": {}}
        await self.service.stop()
        self.assertFalse(self.service.running)

    async def test_stop_reports_airdrop_shutdown_failure_and_preserves_ownership(self):
        self.service.airdrop_owned = True
        with patch.object(
            omadrop,
            "run_command",
            new=AsyncMock(side_effect=RuntimeError("radio restoration failed")),
        ):
            response = await self.control.post("/", json={"command": "stop"})

        self.assertEqual(response.status, 400)
        self.assertIn("Could not stop AirDrop", (await response.json())["error"])
        self.assertTrue(self.service.airdrop_owned)
        self.assertEqual(self.service.channels["airdrop"]["state"], "unavailable")
        self.assertIn("radio restoration failed", self.service.channels["airdrop"]["reason"])

        with patch.object(omadrop, "run_command", new=AsyncMock(return_value="")):
            await self.service.stop()
        self.assertFalse(self.service.airdrop_owned)
        self.assertEqual(self.service.channels["airdrop"]["state"], "stopped")

    async def send_upload_token_map(self, session, status=200):
        sources = [Path(self.temp.name) / name for name in ("first.txt", "second.txt")]
        for path, content in zip(sources, (b"first", b"second")):
            path.write_bytes(content)
        uploads = []

        async def prepare(request):
            offered = await request.json()
            self.assertEqual(set(offered["files"]), {"0", "1"})
            if status == 204:
                return web.Response(status=204)
            return web.json_response(session, status=status)

        async def upload(request):
            uploads.append((dict(request.query), await request.read()))
            return web.Response()

        app = web.Application()
        app.router.add_post(omadrop.API + "/prepare-upload", prepare)
        app.router.add_post(omadrop.API + "/upload", upload)
        server = TestServer(app)
        await server.start_server()
        try:
            peer = {"name": "Test receiver", "protocol": "localsend", "scheme": "http",
                    "host": "127.0.0.1", "port": server.port}
            key = f"token-map-{len(self.service.transfers)}"
            await self.service.send_files(peer, [str(path) for path in sources], key)
            return self.service.transfers[key], uploads
        finally:
            await server.close()

    async def test_invalid_upload_token_map_fails_before_sending_any_bytes(self):
        valid_tokens = {"0": "first-token", "1": "second-token"}
        sessions = [None, [], {},
            *({"sessionId": value, "files": valid_tokens} for value in (None, "", 1, [])),
            *({"sessionId": "test-session", "files": value} for value in (
                None, [], {}, {"0": "first-token", "00": "second-token"},
                {"0": "first-token", "2": "second-token"},
                {"-1": "first-token", "1": "second-token"},
                {"0": None, "1": "second-token"},
                {"0": "", "1": "second-token"},
                {"0": 7, "1": "second-token"},
            )),
        ]
        for session in sessions:
            with self.subTest(session=session):
                transfer, uploads = await self.send_upload_token_map(session)
                self.assertEqual(transfer["state"], "failed")
                self.assertEqual(transfer["error"], "Recipient returned an invalid LocalSend upload session")
                self.assertEqual(transfer["bytes"], 0)
                self.assertEqual(uploads, [])

    async def test_partial_upload_token_map_reports_only_accepted_files(self):
        transfer, uploads = await self.send_upload_token_map({
            "sessionId": "test-session", "files": {"1": "second-token"},
        })
        self.assertEqual(transfer["state"], "complete")
        self.assertEqual((transfer["files"], transfer["fileName"], transfer["name"]),
                         (1, "second.txt", "second.txt"))
        self.assertEqual((transfer["bytes"], transfer["total"]), (6, 6))
        self.assertEqual(uploads, [({"sessionId": "test-session", "fileId": "1",
                                    "token": "second-token"}, b"second")])

    async def test_upload_token_map_http_204_completes_without_upload(self):
        transfer, uploads = await self.send_upload_token_map(None, status=204)
        self.assertEqual(transfer["state"], "complete")
        self.assertEqual((transfer["files"], transfer["bytes"], transfer["total"]), (0, 0, 0))
        self.assertEqual((transfer["fileName"], transfer["name"], transfer["error"]),
                         ("No upload needed", "No upload needed", ""))
        self.assertEqual(uploads, [])

    async def test_reordered_upload_token_map_sends_each_offered_file_once(self):
        transfer, uploads = await self.send_upload_token_map({
            "sessionId": "test-session", "files": {"1": "second-token", "0": "first-token"},
        })
        self.assertEqual(transfer["state"], "complete")
        self.assertEqual((transfer["files"], transfer["bytes"], transfer["total"]), (2, 11, 11))
        self.assertCountEqual(uploads, [
            ({"sessionId": "test-session", "fileId": "0", "token": "first-token"}, b"first"),
            ({"sessionId": "test-session", "fileId": "1", "token": "second-token"}, b"second"),
        ])

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

    async def test_slow_healthy_outbound_localsend_body_exceeds_idle_deadlines_over_https(self):
        receiver = LocalOnly()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            receiver.port = sock.getsockname()[1]
        receiver.settings["directory"] = str(Path(self.temp.name) / "slow-receiver")
        await receiver.start()
        source = Path(self.temp.name) / "slow-outbound.bin"
        content = b"x" * (16 * 1024)
        source.write_bytes(content)
        peer = {
            "id": "localsend:slow-receiver", "name": "Slow receiver",
            "protocol": "localsend", "host": "127.0.0.1",
            "port": receiver.port, "scheme": "https",
            "fingerprint": receiver.fingerprint,
        }
        self.service.peers[peer["id"]] = dict(peer, seen=0, deviceType="desktop")
        first_chunk = asyncio.Event()

        async def paced_chunks(path, transfer):
            with path.open("rb") as source_file:
                while data := source_file.read(1024):
                    yield data
                    transfer["bytes"] += len(data)
                    first_chunk.set()
                    await asyncio.sleep(0.02)

        async def monitor_progress():
            while not sending.done():
                await asyncio.sleep(0.01)
                await self.service.expire_stalled_transfers()

        self.assertIsNone(self.service.local_send_timeout().total)
        with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.05), patch.object(
            omadrop, "LOCALSEND_RESPONSE_IDLE_TIMEOUT", 0.08
        ), patch.object(self.service, "local_send_chunks", paced_chunks):
            started = time.monotonic()
            sending = self.service.spawn(
                self.service.send_files(peer, [str(source)], "slow-outbound")
            )
            sending.set_name("slow-outbound")
            monitor = None
            try:
                for _ in range(100):
                    if receiver.incoming:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(receiver.incoming)
                next(iter(receiver.incoming.values()))["decision"].set_result(True)
                await asyncio.wait_for(first_chunk.wait(), 2)
                monitor = asyncio.create_task(monitor_progress())
                await asyncio.wait_for(sending, 5)
            finally:
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                await receiver.stop()

        self.assertGreater(time.monotonic() - started, 0.08)
        self.assertEqual(self.service.transfers["slow-outbound"]["state"], "complete")
        self.assertEqual(self.service.transfers["slow-outbound"]["bytes"], len(content))
        self.assertEqual(
            (Path(receiver.settings["directory"]) / source.name).read_bytes(), content
        )

    async def test_outbound_localsend_stall_is_cancelled_by_progress_watchdog(self):
        receiver = LocalOnly()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            receiver.port = sock.getsockname()[1]
        receiver.settings["directory"] = str(Path(self.temp.name) / "stalled-receiver")
        await receiver.start()
        source = Path(self.temp.name) / "stalled-outbound.bin"
        source.write_bytes(b"x" * (16 * 1024))
        peer = {
            "id": "localsend:stalled-receiver", "name": "Stalled receiver",
            "protocol": "localsend", "host": "127.0.0.1",
            "port": receiver.port, "scheme": "https",
            "fingerprint": receiver.fingerprint,
        }
        self.service.peers[peer["id"]] = dict(peer, seen=0, deviceType="desktop")
        first_chunk = asyncio.Event()
        continue_upload = asyncio.Event()

        async def stalled_chunks(path, transfer):
            yield b"x" * 1024
            transfer["bytes"] += 1024
            first_chunk.set()
            await continue_upload.wait()
            yield path.read_bytes()[1024:]
            transfer["bytes"] += path.stat().st_size - 1024

        async def monitor_progress():
            while not sending.done():
                await asyncio.sleep(0.01)
                await self.service.expire_stalled_transfers()

        with patch.object(omadrop, "TRANSFER_IDLE_TIMEOUT", 0.05), patch.object(
            self.service, "local_send_chunks", stalled_chunks
        ):
            sending = self.service.spawn(
                self.service.send_files(peer, [str(source)], "stalled-outbound")
            )
            sending.set_name("stalled-outbound")
            monitor = None
            try:
                for _ in range(100):
                    if receiver.incoming:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(receiver.incoming)
                next(iter(receiver.incoming.values()))["decision"].set_result(True)
                await asyncio.wait_for(first_chunk.wait(), 2)
                monitor = asyncio.create_task(monitor_progress())
                for _ in range(100):
                    if list(Path(receiver.settings["directory"]).glob(".omadrop-*.part")):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(
                    list(Path(receiver.settings["directory"]).glob(".omadrop-*.part"))
                )
                await asyncio.wait_for(monitor, 2)
                continue_upload.set()
                await asyncio.gather(sending, return_exceptions=True)
                await asyncio.sleep(0.05)
                self.assertFalse(receiver.sessions)
                self.assertFalse(
                    list(Path(receiver.settings["directory"]).glob(".omadrop-*.part"))
                )
            finally:
                continue_upload.set()
                if monitor is not None and not monitor.done():
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                await receiver.stop()

        transfer = self.service.transfers["stalled-outbound"]
        self.assertEqual(transfer["state"], "failed")
        self.assertIn("without progress", transfer["error"])
        self.assertFalse(receiver.sessions)
        self.assertFalse(
            list(Path(receiver.settings["directory"]).glob(".omadrop-*.part"))
        )

    async def test_localsend_watchdog_releases_all_abandoned_accepted_sessions(self):
        now = time.monotonic()
        for index in range(16):
            key = f"abandoned-session-{index}"
            self.service.sessions[key] = {
                "files": {},
                "host": "127.0.0.1",
                "directory": self.directory,
                "expires": now + omadrop.LOCALSEND_SESSION_IDLE_TIMEOUT,
                "cancelled": False,
            }
            self.service.transfers[key] = omadrop.Transfer({
                "id": key,
                "name": "abandoned.txt",
                "peer": "Abandoned sender",
                "protocol": "localsend",
                "direction": "in",
                "bytes": 0,
                "total": 100,
                "state": "waiting",
                "error": "",
                "files": 1,
                "fileName": "abandoned.txt",
            })
            self.service.transfer_watch[key] = (
                ("waiting", 0), now - omadrop.TRANSFER_IDLE_TIMEOUT - 1
            )

        await self.service.expire_stalled_transfers()

        self.assertFalse(self.service.sessions)
        for index in range(16):
            key = f"abandoned-session-{index}"
            self.assertEqual(self.service.transfers[key]["state"], "failed")

        task, request_id = await self.prepare(name="after-timeout.txt")
        self.assertFalse(task.done())
        session = await self.approve(task, request_id)
        self.assertIn(session["sessionId"], self.service.sessions)

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
