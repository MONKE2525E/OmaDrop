"""AirDrop CLI compatibility: no peers is a valid advertising state."""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SPEC = importlib.util.spec_from_file_location(
    "omadrop", Path(__file__).parents[1] / "scripts/omadrop.py"
)
omadrop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadrop)


class AirDropTests(unittest.IsolatedAsyncioTestCase):
    async def test_unnamed_peers_are_hidden_until_name_resolution(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-peer-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
            peer = {"id": "airdrop:test", "protocol": "airdrop", "name": "Unnamed Apple device"}
            service.peers[peer["id"]] = peer
            self.assertEqual(service.snapshot()["peers"], [])
            self.assertIn(peer["id"], service.peers)
            peer["name"] = "Test Mac"
            self.assertEqual(service.snapshot()["peers"][0]["name"], "Test Mac")

    async def test_control_client_does_not_write_to_receiver_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-runtime-test-") as temp:
            Path(temp).chmod(0o700)
            with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": temp}), patch.object(
                Path, "mkdir", side_effect=OSError("read-only")
            ), patch.object(Path, "chmod", side_effect=OSError("read-only")):
                self.assertEqual(omadrop.runtime_dir(create=False), Path(temp))

    async def test_expired_or_old_unlimited_setting_does_not_resume(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-window-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                for deadline in (0, time.time() - 1):
                    service = omadrop.Coordinator()
                    service.settings.update(enabled=True, advertiseUntil=deadline)
                    service.start = AsyncMock()
                    await service.restore_window()
                    service.start.assert_not_awaited()
                    self.assertFalse(omadrop.Coordinator().settings["enabled"])

    async def test_resume_preserves_original_deadline(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-window-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
                deadline = time.time() + 60
                service.settings.update(enabled=True, advertiseUntil=deadline)
                service.start = AsyncMock()
                await service.restore_window()
                service.start.assert_awaited_once()
                self.assertEqual(service.settings["advertiseUntil"], deadline)
                await service.stop()

    async def test_deadline_stops_advertising_and_clears_saved_preference(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-window-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
                service.running = True
                service.settings.update(enabled=True, advertiseUntil=time.time() - 1)
                await service.expire_window()
                self.assertFalse(service.running)
                self.assertFalse(omadrop.Coordinator().settings["enabled"])
                self.assertTrue(all(row["state"] == "stopped" for row in service.channels.values()))

    async def test_airdrop_preserves_existing_receiver_ownership_and_visibility(self):
        for visible in (False, True):
            with self.subTest(visible=visible), tempfile.TemporaryDirectory(
                prefix="omadrop-external-radio-"
            ) as temp:
                with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                    service = omadrop.Coordinator()
                status = (
                    '{"receiving":true,"visible":'
                    + ("true" if visible else "false")
                    + "}"
                )
                command = AsyncMock(
                    side_effect=[status, status] + (["[]"] if visible else [])
                )
                with patch.object(
                    omadrop.shutil, "which", return_value="/bin/omdrop"
                ), patch.object(omadrop, "run_command", new=command):
                    await service.start_airdrop()
                    await service.stop()
                self.assertFalse(service.airdrop_owned)
                self.assertNotIn(
                    ("omdrop", "on", "forever"),
                    [call.args[:3] for call in command.await_args_list],
                )
                self.assertNotIn(
                    ("omdrop", "off"),
                    [call.args[:2] for call in command.await_args_list],
                )
                self.assertEqual(
                    service.channels["airdrop"]["state"], "stopped"
                )
                if visible:
                    self.assertEqual(len(command.await_args_list), 3)
                else:
                    self.assertEqual(len(command.await_args_list), 2)

    async def test_airdrop_takes_ownership_only_after_successful_start(self):
        for visible in (False, True):
            with self.subTest(visible=visible), tempfile.TemporaryDirectory(
                prefix="omadrop-radio-start-"
            ) as temp:
                with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                    service = omadrop.Coordinator()
                before = (
                    '{"receiving":false,"visible":'
                    + ("true" if visible else "false")
                    + "}"
                )
                command = AsyncMock(side_effect=[
                    before, "", "", '{"receiving":true,"visible":true}',
                    "[]", "",
                ])
                with patch.object(
                    omadrop.shutil, "which", return_value="/bin/omdrop"
                ), patch.object(omadrop, "run_command", new=command):
                    await service.start_airdrop()
                    self.assertTrue(service.airdrop_owned)
                    await service.stop()
                command.assert_any_await("omdrop", "on", "forever", timeout=30)
                command.assert_any_await("omdrop", "off", timeout=20)

    async def test_failed_airdrop_start_does_not_claim_or_stop_external_receiver(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-radio-failure-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
            command = AsyncMock(side_effect=[
                '{"receiving":false,"visible":false}', "",
                RuntimeError("radio failed"),
            ])
            with patch.object(
                omadrop.shutil, "which", return_value="/bin/omdrop"
            ), patch.object(omadrop, "run_command", new=command):
                with self.assertRaisesRegex(RuntimeError, "radio failed"):
                    await service.start_airdrop()
                self.assertFalse(service.airdrop_owned)
                await service.stop()
            self.assertNotIn(
                ("omdrop", "off"),
                [call.args[:2] for call in command.await_args_list],
            )

    async def test_unready_radio_refreshes_reason_without_starting(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-retry-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
            service.channel("airdrop", "unavailable", "old error")
            service.start_airdrop = AsyncMock()
            with patch.object(omadrop.shutil, "which", return_value="/bin/omdrop"), patch.object(
                omadrop, "run_command", new=AsyncMock(return_value=
                    '{"ready":false,"missing":[{"say":"Unsupported channel 40"}]}')
            ):
                await service.refresh_airdrop()
            self.assertEqual(service.channels["airdrop"]["reason"], "Unsupported channel 40")
            service.start_airdrop.assert_not_awaited()

    async def test_radio_retries_when_network_becomes_ready(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-retry-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
            service.running = True
            service.channel("airdrop", "unavailable", "waiting for Wi-Fi")
            service.channel("localsend", "advertising")
            service.start_airdrop = AsyncMock()
            with patch.object(omadrop.shutil, "which", return_value="/bin/omdrop"), patch.object(
                omadrop, "run_command", new=AsyncMock(return_value='{"ready":true}')
            ):
                await service.start()
            service.start_airdrop.assert_awaited_once()
            self.assertEqual(service.channels["localsend"]["state"], "advertising")

    async def test_start_stop_preference_survives_coordinator_shutdown(self):
        class Request:
            def __init__(self, command):
                self.command = command

            async def json(self):
                return {"command": self.command}

        with tempfile.TemporaryDirectory(prefix="omadrop-restart-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_CONFIG_DIR": temp}):
                service = omadrop.Coordinator()
                service.start = AsyncMock()
                await service.action(Request("start"))
                await service.stop()
                self.assertTrue(omadrop.Coordinator().settings["enabled"])
                await service.action(Request("stop"))
                self.assertFalse(omadrop.Coordinator().settings["enabled"])

    async def test_empty_peer_exit_one_is_not_radio_failure(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-airdrop-test-") as temp:
            cli = Path(temp) / "omdrop"
            cli.write_text(
                '#!/bin/sh\ncase "$1" in\n'
                'status) echo \'{"receiving":true,"visible":true}\';;\n'
                'peers) echo "[]"; echo "No devices found yet" >&2; exit 1;;\n'
                'esac\n'
            )
            cli.chmod(0o700)
            with patch.dict(os.environ, {
                "PATH": temp + os.pathsep + os.environ["PATH"],
                "OMADROP_CONFIG_DIR": temp + "/config",
            }):
                service = omadrop.Coordinator()
                await service.start_airdrop()
                self.assertEqual(service.channels["airdrop"]["state"], "advertising")
                self.assertFalse(service.airdrop_owned)
                self.assertEqual(service.peers, {})

    async def test_failed_command_remains_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "radio failed"):
            await omadrop.run_command(
                "/bin/sh", "-c", "echo radio failed >&2; exit 4",
                accepted_codes=(0, 1),
            )
