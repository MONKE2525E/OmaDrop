"""Exercise Owly installation without touching system files or services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AirDropInstallTests(unittest.TestCase):
    def run_install(self, mode=None, receiver=False, external=False):
        with tempfile.TemporaryDirectory(prefix="omadrop-install-") as temp:
            base = Path(temp)
            project = base / "project"
            (project / "tools").mkdir(parents=True)
            (project / "scripts").mkdir()
            (project / "patches").mkdir()
            for name in ("consent", "progress", "clipboard", "links"):
                (project / f"patches/receiver-{name}.patch").touch()
            (project / "pins").write_text("OMDROP_RECEIVER_REPO=test\nOMDROP_RECEIVER_COMMIT=test\n")
            (project / "scripts/airdrop_consent.py").write_text("# fixture\n")
            executable = project / "tools/install-airdrop.sh"
            shutil.copy2(ROOT / "tools/install-airdrop.sh", executable)
            tools = base / "bin"
            tools.mkdir()
            for name in ("bash", "dirname", "readlink", "mkdir", "mktemp", "cp", "install", "rm"):
                (tools / name).symlink_to(shutil.which(name))
            log = base / "commands.jsonl"
            mock = tools / "mock"
            mock.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\n')
if name == 'git' and args[0] == 'clone':
    destination = pathlib.Path(args[-1])
    (destination / 'bin').mkdir(parents=True)
    (destination / 'share').mkdir()
    receiver = destination / 'bin/omdrop'
    receiver.write_text('#!/usr/bin/bash\nexit 0\n')
    receiver.chmod(0o755)
    for filename in ('manifest.json', 'LICENSE', 'README.md'):
        (destination / filename).write_text('fixture\n')
''')
            mock.chmod(0o755)
            for name in ("sudo", "omarchy", "cargo", "git", "patch"):
                (tools / name).symlink_to(mock)
            if external:
                (tools / "omdrop").symlink_to(mock)
            if receiver:
                (base / "data/omadrop/airdrop").mkdir(parents=True)
            env = dict(os.environ, PATH=str(tools), XDG_DATA_HOME=str(base / "data"), TMPDIR=str(base), TEST_LOG=str(log))
            result = subprocess.run(["/usr/bin/bash", str(executable)] + ([mode] if mode else []),
                                    env=env, text=True, capture_output=True, timeout=10)
            commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            consent = (base / "data/omadrop/airdrop/bin/airdrop_consent.py").is_file()
            return result, commands, consent

    def test_installs_both_owly_packages_and_preserves_consent(self):
        result, commands, consent = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(consent)
        installs = [c for c in commands if c[0] == "sudo" and c[1].endswith("packaging/install.sh")]
        self.assertEqual([c[2] for c in installs], ["owly", "omdrop-owl"])
        self.assertEqual(len([c for c in commands if c[0] == "patch"]), 4)
        self.assertFalse(any("/etc/omdrop" in " ".join(c) or "ufw" in c for c in commands))

    def test_refuses_legacy_mode_before_any_changes(self):
        for mode in ("shared", "exclusive", "standalone", "dualchan", "auto"):
            with self.subTest(mode=mode):
                result, commands, _ = self.run_install(mode=mode)
                self.assertEqual(result.returncode, 2)
                self.assertIn("omadrop triage", result.stderr)
                self.assertEqual(commands, [])

    def test_preserves_existing_receivers(self):
        for flags in ({"receiver": True}, {"external": True}):
            with self.subTest(flags=flags):
                result, commands, _ = self.run_install(**flags)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(commands, [])
