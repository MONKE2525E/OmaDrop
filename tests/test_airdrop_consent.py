"""Approval grants are bound to the sender, request and exact file manifest."""
import importlib.util
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "airdrop_consent", Path(__file__).parents[1] / "scripts/airdrop_consent.py"
)
consent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consent)


class ConsentTests(unittest.TestCase):
    def setUp(self):
        consent._grants.clear()
        self.handler = types.SimpleNamespace(
            client_address=("fe80::123", 10), headers={"TransferID": "test-transfer-123"},
            connection=types.SimpleNamespace(getpeercert=lambda **kwargs: b"peer-cert"),
        )
        self.ask = {"TransferID": "test-transfer-123", "SenderComputerName": "Test Mac",
                    "Files": [{"FileName": "photo.png", "FileSize": 12}]}

    def test_decline_and_failed_bridge_never_grant_upload(self):
        for result in ({"accepted": False}, {}):
            with patch.object(consent, "request_approval", return_value=result):
                self.assertFalse(consent.approve(self.handler, self.ask))
            self.assertIsNone(consent.consume(self.handler))

    def test_explicit_approval_is_single_use_and_bound_to_peer(self):
        with patch.object(consent, "request_approval", return_value={"accepted": True}):
            self.assertTrue(consent.approve(self.handler, self.ask))
        self.handler.client_address = ("fe80::456", 10)
        self.assertIsNone(consent.consume(self.handler))
        self.handler.client_address = ("fe80::123", 11)
        grant = consent.consume(self.handler)
        self.assertIsNotNone(grant)
        self.assertIsNone(consent.consume(self.handler))
        consent.check_file(grant, "photo.png", 12)
        with self.assertRaises(ValueError):
            consent.check_file(grant, "extra.txt", 3)

    def test_expired_grant_and_wrong_file_size_are_rejected(self):
        with patch.object(consent, "request_approval", return_value={"accepted": True}):
            consent.approve(self.handler, self.ask)
        with patch.object(consent.time, "monotonic", return_value=10**20):
            self.assertIsNone(consent.consume(self.handler))
        with self.assertRaises(ValueError):
            consent.check_file({"expected": {"photo.png": 12}}, "photo.png", 13)

    def test_links_cannot_be_hidden_in_an_approved_file_request(self):
        self.ask["Items"] = ["https://example.com"]
        with patch.object(consent, "request_approval") as bridge:
            self.assertFalse(consent.approve(self.handler, self.ask))
            bridge.assert_not_called()

    def test_apple_transfer_id_dictionary_matches_upload_header(self):
        self.ask["TransferID"] = {"id": "test-transfer-123"}
        with patch.object(consent, "request_approval", return_value={"accepted": True}):
            self.assertTrue(consent.approve(self.handler, self.ask))
        self.assertIsNotNone(consent.consume(self.handler))

    def test_links_require_consent_open_once_and_cannot_authorize_upload(self):
        ask = {"TransferID": "test-transfer-123", "TransferType": {"links": True},
               "Items": ["https://example.test/path"]}
        with patch.object(consent, "request_approval", return_value={"accepted": False}):
            self.assertFalse(consent.approve(self.handler, ask))
        with patch.object(consent, "request_approval", return_value={"accepted": True}) as approval:
            self.assertTrue(consent.approve(self.handler, ask))
        self.assertEqual(approval.call_args.args[1]["kind"], "text")
        self.assertEqual(approval.call_args.args[1]["description"], ask["Items"][0])
        self.assertIsNone(consent.consume(self.handler))
        with tempfile.TemporaryDirectory() as folder, patch.object(consent, "report") as report:
            contents = consent.store_links(self.handler, folder)
            self.assertEqual(contents, [b"https://example.test/path\n"])
            self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertEqual(approval.call_args.args[1]["urls"], ask["Items"])
            self.assertEqual(report.call_args.args[1], "complete")
            with self.assertRaises(ValueError):
                consent.store_links(self.handler, folder)

    def test_invalid_or_mixed_links_never_request_approval(self):
        for items in (["file:///etc/passwd"], ["https://example.test/\nInjected=value"],
                      ["https://"], ["https://example.test/" + "x" * 1048576]):
            ask = {"TransferID": "test-transfer-123", "TransferType": {"links": True}, "Items": items}
            with patch.object(consent, "request_approval") as approval:
                self.assertFalse(consent.approve(self.handler, ask))
                approval.assert_not_called()

    def test_failed_progress_reporting_always_releases_upload_slot(self):
        slots = threading.BoundedSemaphore(1)
        grant = {"id": "expired", "token": "token", "bytes": 0}
        for _ in range(3):
            self.assertTrue(slots.acquire(blocking=False))
            with patch.object(consent, "report", side_effect=ValueError("expired")) as report:
                with self.assertRaisesRegex(ValueError, "expired"):
                    try:
                        # The initial report is inside the receiver's slot-owned try.
                        report(grant)
                    finally:
                        consent.finish_upload(grant, "failed", slots.release)
                self.assertEqual(report.call_count, 2)
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
