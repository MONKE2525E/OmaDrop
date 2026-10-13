"""Discovery wire compatibility, bounded LAN scans, and durable history expiry."""

import importlib.util
import ipaddress
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "omadrop_history", Path(__file__).parents[1] / "scripts/omadrop.py"
)
omadrop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadrop)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omadrop-history-")
        self.environment = patch.dict(os.environ, {"OMADROP_CONFIG_DIR": self.temp.name})
        self.environment.start()
        self.service = omadrop.Coordinator()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_terminal_transition_persists_immediately_without_status_poll(self):
        transfer = omadrop.Transfer(id="send", state="transferring", bytes=1)
        self.service.transfers["send"] = transfer
        transfer.update(state="complete", bytes=2)
        restored = omadrop.Coordinator().transfers["send"]
        self.assertEqual(restored["state"], "complete")
        self.assertEqual(restored["completedAt"], transfer["completedAt"])
        self.assertEqual(restored["bytes"], 2)
        self.assertEqual((Path(self.temp.name) / "transfers.json").stat().st_mode & 0o777, 0o600)

    def test_late_terminal_updates_do_not_extend_three_hour_retention(self):
        with patch.object(omadrop.time, "time", return_value=10000):
            transfer = omadrop.Transfer(id="failed", state="transferring")
            self.service.transfers["failed"] = transfer
            transfer["state"] = "failed"
        with patch.object(omadrop.time, "time", return_value=10000 + omadrop.TRANSFER_HISTORY_TTL - 1):
            transfer.update(state="failed", error="late disconnect")
            self.service.expire_finished_transfers()
        self.assertIn("failed", self.service.transfers)
        self.assertEqual(transfer["completedAt"], 10000)
        self.service.expire_finished_transfers(10000 + omadrop.TRANSFER_HISTORY_TTL)
        self.assertNotIn("failed", self.service.transfers)
        self.assertEqual(json.loads((Path(self.temp.name) / "transfers.json").read_text()), [])

    def test_restart_prunes_expired_and_legacy_history_but_preserves_recent(self):
        now = time.time()
        rows = [
            {"id": "old", "state": "complete", "completedAt": now - 10801},
            {"id": "old-legacy", "state": "failed", "updated": now - 10801},
            {"id": "recent", "state": "cancelled", "completedAt": now - 100},
            {"id": "recent-legacy", "state": "rejected", "updated": now - 100},
            {"id": "unfinished", "state": "waiting", "updated": now},
        ]
        (Path(self.temp.name) / "transfers.json").write_text(json.dumps(rows))
        restored = omadrop.Coordinator()
        self.assertEqual(set(restored.transfers), {"recent", "recent-legacy"})
        self.assertEqual({row["id"] for row in json.loads((Path(self.temp.name) / "transfers.json").read_text())}, set(restored.transfers))

    def test_expiry_keeps_active_transfers_even_with_old_timestamp(self):
        self.service.transfers["active"] = omadrop.Transfer(id="active", state="waiting", updated=1)
        self.service.expire_finished_transfers()
        self.assertIn("active", self.service.transfers)

    def test_subnet_scan_obeys_real_prefix_and_rotates_large_lan(self):
        self.service.local_interfaces = [{"address": "192.168.7.9", "prefixlen": 28, "name": "eth0"}]
        self.service.local_addresses = ["192.168.7.9"]
        hosts = self.service.discovery_hosts()
        self.assertEqual(hosts, {str(ip) for ip in ipaddress.ip_network("192.168.7.0/28").hosts()} - {"192.168.7.9"})
        self.service.local_interfaces = [{"address": "192.168.7.9", "prefixlen": 16, "name": "eth0"}]
        first, second = self.service.discovery_hosts(), self.service.discovery_hosts()
        self.assertLessEqual(len(first), omadrop.DISCOVERY_SCAN_LIMIT)
        self.assertFalse(first & second)
        self.assertIn("192.168.7.10", first)
        self.assertTrue(all(ipaddress.ip_address(host) in ipaddress.ip_network("192.168.0.0/16") for host in first | second))

    def test_current_multicast_without_announce_triggers_register_but_legacy_response_does_not(self):
        owner = Mock()
        owner.register_peer.return_value = {"id": "localsend:test"}
        discovery = omadrop.Discovery(owner)
        packet = {"alias": "Test", "fingerprint": "peer", "version": "2.2", "protocol": "http", "port": 53317}
        discovery.datagram_received(json.dumps(packet).encode(), ("192.168.7.10", 53317))
        owner.reply_to_announcement.assert_called_once()
        owner.reply_to_announcement.reset_mock()
        discovery.datagram_received(json.dumps(dict(packet, announce=False)).encode(), ("192.168.7.10", 53317))
        owner.reply_to_announcement.assert_not_called()

    def test_invalid_multicast_packet_cannot_crash_callback(self):
        discovery = omadrop.Discovery(self.service)
        for packet in ([], None, 123, "text", {"protocol": "ftp"}):
            discovery.datagram_received(json.dumps(packet).encode(), ("192.168.7.10", 53317))
        self.assertEqual(self.service.peers, {})


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_interfaces_keep_prefixes_and_exclude_vpn_docker_and_loopback(self):
        def interface(name, address, prefix=24, flags=None):
            return {"ifname": name, "flags": flags or ["UP", "MULTICAST"],
                    "addr_info": [{"local": address, "prefixlen": prefix}]}
        rows = [interface("eno1", "192.168.7.9", 28), interface("wlan0", "10.0.1.5", 23),
                interface("tailscale0", "100.64.0.1", 32), interface("docker0", "172.17.0.1"),
                interface("veth123", "172.17.0.2"), interface("vpn0", "10.0.0.1", flags=["UP", "POINTOPOINT"]),
                interface("lo", "127.0.0.1", 8)]
        with patch.object(omadrop, "run_command", new=AsyncMock(return_value=json.dumps(rows))):
            result = await omadrop.lan_interfaces()
        self.assertEqual([(row["name"], row["prefixlen"]) for row in result], [("eno1", 28), ("wlan0", 23)])


if __name__ == "__main__":
    unittest.main()
