"""Triage handoff, privacy, and interactive launcher behavior."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("triage", ROOT / "scripts/airdrop_triage.py")
triage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(triage)


class TriageTests(unittest.TestCase):
    def test_redacts_network_identifiers_without_losing_hardware_ids(self):
        evidence = (f"ssid My Secret Network\naddr aa:bb:cc:dd:ee:ff\n"
                    f"10.20.30.40 fe80::abcd:1234%awdl0 {Path.home()}/secret\n"
                    "rtw89_8852be 10ec:b852 channel 40\n")
        result = triage.redact(evidence)
        for private in ("My Secret Network", "aa:bb:cc:dd:ee:ff", "10.20.30.40", "fe80::abcd:1234", str(Path.home())):
            self.assertNotIn(private, result)
        self.assertIn("10ec:b852 channel 40", result)

    def test_probe_failure_timeout_and_missing_tool_are_evidence(self):
        result = triage.command(sys.executable, "-c", "print('blocked'); raise SystemExit(4)")
        self.assertEqual(result, {"exit": 4, "output": "blocked"})
        result = triage.command(sys.executable, "-c", "import time; time.sleep(10)", timeout=0.02)
        self.assertIsNone(result["exit"])
        self.assertIn("timed out", result["output"])
        self.assertEqual(triage.command("omadrop-nonexistent-tool")["output"], "not installed")

    def test_collection_bounds_helper_output(self):
        result = triage.command(sys.executable, "-c", "print('x'*40000)")
        self.assertLess(len(result["output"]), triage.LIMIT + 100)
        self.assertIn("truncated", result["output"])

    def test_configuration_does_not_copy_arbitrary_fields_or_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            etc = Path(temp)
            (etc / "omdrop").mkdir()
            (etc / "omdrop/owl-profiles").write_text("rtw89_8852be untested dualchan station no no private notes\n")
            (etc / "owly").mkdir()
            (etc / "owly/radio.toml").write_text('station="wlan0"\nallow_untested=true\npassword="private"\n')
            result = triage.configuration(etc)
            self.assertEqual(result["legacy_profiles"][0]["mode"], "dualchan")
            self.assertEqual(result["owly"]["radio"]["settings"], {"station": "wlan0", "allow_untested": True})
            self.assertNotIn("private", json.dumps(result))
            (etc / "owly/radio.toml").write_text("broken[")
            self.assertIn("error", triage.configuration(etc)["owly"]["radio"])

    def test_select_agent_does_not_choose_a_different_requested_agent(self):
        with patch.object(triage.shutil, "which", side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            self.assertEqual(triage.select_agent(None), "codex")
            with self.assertRaisesRegex(ValueError, "claude is not installed"):
                triage.select_agent("claude")
        with patch.object(triage.shutil, "which", return_value="/fixture"), patch.object(triage.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "Both agents"):
                triage.select_agent(None)

    def test_private_unique_sessions_include_the_offline_playbook(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            one = triage.write_session(base, {"wifi": []}, "dualchan unsupported")
            two = triage.write_session(base, {"wifi": []}, "")
            self.assertNotEqual(one, two)
            self.assertEqual(one.stat().st_mode & 0o777, 0o700)
            self.assertEqual((one / "context.json").stat().st_mode & 0o777, 0o600)
            prompt = (one / "prompt.md").read_text()
            self.assertIn("dualchan unsupported", prompt)
            self.assertIn("Only call the card working after a real file transfer each way", prompt)
            self.assertIn("explicit approval before posting", prompt)

    def test_collect_only_works_through_real_shell_and_npm_launchers(self):
        with tempfile.TemporaryDirectory() as temp:
            for command in ([str(ROOT / "scripts/omadrop"), "triage"], [shutil.which("node"), str(ROOT / "bin/omadrop-triage.cjs")]):
                with self.subTest(command=command):
                    result = subprocess.run(command + ["--collect-only", "--output-dir", temp],
                                            text=True, capture_output=True, timeout=90)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("context and playbook saved", result.stdout)
            sessions = list(Path(temp).glob("airdrop-*"))
            self.assertEqual(len(sessions), 2)
            facts = json.loads((sessions[0] / "context.json").read_text())
            self.assertIn("iw dev", facts["commands"])
            self.assertIn("legacy_profiles", facts["configuration"])

    def test_real_interactive_agent_gets_model_context_cwd_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            tools = base / "bin"
            tools.mkdir()
            marker = base / "agent.json"
            agent = tools / "codex"
            agent.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                             f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'cwd':os.getcwd(), 'prompt':pathlib.Path('prompt.md').exists()}}))\n"
                             "raise SystemExit(7)\n")
            agent.chmod(0o755)
            master, slave = pty.openpty()
            try:
                result = subprocess.run([sys.executable, str(ROOT / "scripts/airdrop_triage.py"),
                                         "--agent", "codex", "--model", "chosen-model", "--output-dir", str(base / "sessions")],
                                        stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                                        env={**os.environ, "PATH": str(tools)}, timeout=15)
            finally:
                os.close(slave)
                os.close(master)
            self.assertEqual(result.returncode, 7, result.stderr)
            called = json.loads(marker.read_text())
            self.assertEqual(called["argv"][:2], ["--model", "chosen-model"])
            self.assertEqual(len(called["argv"]), 3)
            self.assertTrue(called["prompt"])
            self.assertTrue(Path(called["cwd"]).is_relative_to(base / "sessions"))


if __name__ == "__main__":
    unittest.main()
