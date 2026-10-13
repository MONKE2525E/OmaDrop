"""Exercise installer mode selection with all privileged/external work mocked."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AirDropInstallModeTests(unittest.TestCase):
    def run_install(self, mode=None, existing_profile=False, coordinator=False):
        with tempfile.TemporaryDirectory(prefix="omadrop-install-test-") as temporary:
            base = Path(temporary)
            project = base / "project"
            (project / "tools").mkdir(parents=True)
            (project / "scripts").mkdir()
            (project / "userspace").mkdir()
            (project / "patches").mkdir()
            for name in ("consent", "progress", "clipboard", "links"):
                (project / f"patches/receiver-{name}.patch").touch()
            (project / "pins").write_text("OMDROP_RECEIVER_REPO=test\nOMDROP_RECEIVER_COMMIT=test\n")
            (project / "scripts/airdrop_consent.py").write_text("# fixture\n")
            (project / "userspace/owl-profiles").write_text(
                "iwlwifi tested exclusive station no no Intel fixture\n"
                "* untested shared station yes no Generic fixture\n"
            )
            config = base / "etc/omdrop"
            config.mkdir(parents=True)
            if existing_profile:
                (config / "owl-profiles").write_text("preserve me\n")
            sysfs = base / "sys/class/net/wlan0/device"
            sysfs.mkdir(parents=True)
            (sysfs / "driver").symlink_to(base / "iwlwifi")
            script = (ROOT / "tools/install-airdrop.sh").read_text()
            script = script.replace("/etc/omdrop", str(config)).replace("/sys/class/net", str(base / "sys/class/net"))
            executable = project / "tools/install-airdrop.sh"
            executable.write_text(script)
            tools = base / "bin"
            tools.mkdir()
            for name in ("bash", "dirname", "basename", "readlink", "awk", "mkdir", "mktemp", "cp", "install", "rm", "rg"):
                (tools / name).symlink_to(shutil.which(name))
            if coordinator:
                coordinator_cli = tools / "omadrop"
                coordinator_cli.write_text(f"#!{sys.executable}\nraise SystemExit('Wrong CLI invoked')\n")
                coordinator_cli.chmod(0o755)
            mock = tools / "mock"
            mock.write_text(f"#!{sys.executable}\n" + r'''
import os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'sudo':
    with open(os.environ['TEST_PRIVILEGED_LOG'], 'a') as log:
        log.write(' '.join(args) + '\n')
    raise SystemExit(subprocess.run(args).returncode)
if name == 'iw' and args == ['dev']:
    print('Interface wlan0\n\ttype managed')
if name == 'makepkg' and args == ['--packagelist']:
    print(os.environ['TEST_PACKAGE'])
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
            for name in ("sudo", "omarchy", "makepkg", "pacman", "git", "patch", "iw"):
                (tools / name).symlink_to(mock)
            log = base / "privileged.log"
            environment = dict(os.environ, PATH=str(tools), XDG_DATA_HOME=str(base / "data"),
                               TMPDIR=str(base), TEST_PRIVILEGED_LOG=str(log), TEST_PACKAGE=str(base / "package.pkg.tar.zst"))
            args = ["/usr/bin/bash", str(executable)] + ([mode] if mode else [])
            result = subprocess.run(args, env=environment, text=True, capture_output=True, timeout=10)
            profile = (config / "owl-profiles").read_text() if (config / "owl-profiles").exists() else None
            return result, profile, log.read_text() if log.exists() else ""

    def test_default_uses_automatic_profile_without_override(self):
        result, profile, _ = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(profile)

    def test_existing_omadrop_coordinator_does_not_block_receiver_install(self):
        result, profile, privileged = self.run_install(coordinator=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[SUCCESS] AirDrop installed", result.stdout)
        self.assertIn("pacman -U --noconfirm", privileged)
        self.assertIsNone(profile)

    def test_each_explicit_mode_is_written_and_firewall_is_untouched(self):
        for mode in ("shared", "exclusive", "standalone", "dualchan"):
            with self.subTest(mode=mode):
                result, profile, privileged = self.run_install(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(profile.split()[:3], ["iwlwifi", "untested", mode])
                self.assertNotIn("ufw", privileged)
                self.assertIn("Firewall rules were not changed", result.stdout)

    def test_explicit_mode_preserves_existing_override(self):
        result, profile, privileged = self.run_install("shared", existing_profile=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(profile, "preserve me\n")
        self.assertEqual(privileged, "")


if __name__ == "__main__":
    unittest.main()
