"""Clipboard staging preserves bytes and only cleans its own temporary copies."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SPEC = importlib.util.spec_from_file_location("omadrop_content", Path(__file__).parents[1] / "scripts/omadrop.py")
omadrop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omadrop)


class ContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_dropped_text_preserves_utf8_and_rejects_invalid_or_large_input(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-content-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": temp}):
                data = "Dropped text: café 🐒\n".encode()
                result = omadrop.stage_text(data)
                path = Path(result["files"][0]["path"])
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(result["files"][0]["kind"], "text")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                omadrop.cleanup_sent_files([path])
                self.assertEqual(omadrop.stage_text(b""), {"files": [], "error": "Nothing to share"})
                for invalid in (b"\xff", b"x" * (1024 * 1024 + 1)):
                    with self.assertRaises(ValueError):
                        omadrop.stage_text(invalid)

    async def test_text_and_image_staging_are_private_and_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-content-test-") as temp:
            with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": temp}):
                for mime, data, kind in (("text/plain", b"Hello\n", "text"),
                                         ("image/png", b"\x89PNG test", "image")):
                    with patch.object(omadrop, "clipboard_read", new=AsyncMock(side_effect=[mime.encode(), data])):
                        result = await omadrop.stage_clipboard()
                    metadata = result["files"][0]
                    path = Path(metadata["path"])
                    self.assertEqual(path.read_bytes(), data)
                    self.assertEqual(metadata["kind"], kind)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                    omadrop.cleanup_sent_files([path])
                    self.assertFalse(path.exists())

    async def test_file_urls_preserve_original_and_ignore_remote_urls(self):
        with tempfile.TemporaryDirectory(prefix="omadrop-content-test-") as temp:
            original = Path(temp) / "image with space.png"
            original.write_bytes(b"test")
            uris = original.as_uri() + "\nhttps://example.com/private\n"
            with patch.dict(os.environ, {"OMADROP_RUNTIME_DIR": temp}), patch.object(
                omadrop, "clipboard_read", new=AsyncMock(side_effect=[b"text/uri-list", uris.encode()])
            ):
                result = await omadrop.stage_clipboard()
                self.assertEqual(result["files"][0]["path"], str(original))
                omadrop.cleanup_sent_files([original])
                self.assertTrue(original.exists())

    async def test_empty_clipboard_does_not_create_file(self):
        with patch.object(omadrop, "clipboard_read", new=AsyncMock(return_value=b"")):
            self.assertEqual(await omadrop.stage_clipboard(), {"files": [], "error": "Clipboard is empty"})
