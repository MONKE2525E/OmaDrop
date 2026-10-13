"""Check merged radio lifecycles using command doubles, without radio access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "userspace/omdrop-discoverable").read_text()
FUNCTIONS = SOURCE[SOURCE.index("log() {"):SOURCE.rindex('\ncase "${1:-}"')]


class RadioModeTests(unittest.TestCase):
    def run_case(self, mode="shared", extra="cmd_start 30", *, freq="2437", foreign="", legacy=False, ipv6=True,
                 fail_type_restore=False, fail_ipv6_restore=False, fail_link_restore=False,
                 fail_mon_delete=False, fail_go_delete=False, fail_ibss_delete=False,
                 fail_idle_managed_restore=False, fail_idle_link_restore=False, fail_pm_restore=False,
                 fail_reconnect=False):
        with tempfile.TemporaryDirectory(prefix="omadrop-radio-test-") as tmp:
            root = Path(tmp)
            for name in ("owl", "hostapd-go"):
                binary = root / name
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o755)
            env = dict(os.environ, RUN=tmp, LIB=tmp, MODE=mode, FREQ=freq,
                       FOREIGN=foreign, LEGACY=str(int(legacy)), IPV6=str(int(ipv6)))
            env["FAIL_TYPE_RESTORE"] = str(int(fail_type_restore))
            env["FAIL_IPV6_RESTORE"] = str(int(fail_ipv6_restore))
            env["FAIL_LINK_RESTORE"] = str(int(fail_link_restore))
            env["FAIL_MON_DELETE"] = str(int(fail_mon_delete))
            env["FAIL_GO_DELETE"] = str(int(fail_go_delete))
            env["FAIL_IBSS_DELETE"] = str(int(fail_ibss_delete))
            env["FAIL_IDLE_MANAGED_RESTORE"] = str(int(fail_idle_managed_restore))
            env["FAIL_IDLE_LINK_RESTORE"] = str(int(fail_idle_link_restore))
            env["FAIL_PM_RESTORE"] = str(int(fail_pm_restore))
            env["FAIL_RECONNECT"] = str(int(fail_reconnect))
            script = r'''
set -uo pipefail
IF=awdl0
MON=mon0
GO=omgo0
IBSS=awdlibss0
AWDL_BSSID=00:25:00:ff:94:73
IPV6_RESTORE_ATTEMPTS=0
LINK_RESTORE_ATTEMPTS=0
IDLE_LINK_RESTORE_ATTEMPTS=0
IDLE_MANAGED_RESTORE_ATTEMPTS=0
RECONNECT_ATTEMPTS=0
MON_DELETE_ATTEMPTS=0
GO_DELETE_ATTEMPTS=0
IBSS_DELETE_ATTEMPTS=0
MON_PRESENT=$LEGACY
GO_PRESENT=0
IBSS_PRESENT=$LEGACY
GO_BIN=$LIB/hostapd-go
OWL_BIN=$LIB/owl
OWL_LOG=$RUN/owl.log
SELF=$LIB/omdrop-discoverable
CONF=$RUN
PROFILES=$LIB/owl-profiles
ADV_FLAGS=137
ADV_INTERVAL=3
ADV_BURST="--count 3 --interval 0.05"
ADV_BURST_EXCLUSIVE="--count 64 --interval 0.0164"
VERSION=0.1.0
CONTRACT=0.8.1
''' + FUNCTIONS + r'''
record() { printf '%s\n' "$*" >> "$RUN/commands"; }
log() { record "log $*"; }
need_root() { :; }
station_iface() { echo wlan0; }
driver_of() { echo fixture; }
load_profile() {
  PROF_STATUS=tested
  PROF_MODE=$MODE
  PROF_MONMAC=station
  PROF_RECONNECT=${RECONNECT:-no}
  PROF_PM=no
}
knob() { :; }
alive() { [[ "$LEGACY" == 1 && "${2:-}" == "$OWL_BIN"* ]]; }
flock() { :; }
ping() { :; }
sleep() { :; }
kill() { record "kill $*"; }
pkill() { record "pkill $*"; }
mountpoint() { return 0; }
readlink() { echo /fixture/phy0; }
mt76_dir() {
  if [[ "$FAIL_PM_RESTORE" == 1 ]]; then
    calls=$(cat "$RUN/pm-restore-attempts" 2>/dev/null || echo 0)
    calls=$((calls + 1))
    echo "$calls" > "$RUN/pm-restore-attempts"
    if [[ "$calls" -eq 1 ]]; then echo "$RUN/missing-mt76"; return; fi
  fi
  mkdir -p "$RUN/mt76"
  touch "$RUN/mt76/runtime-pm" "$RUN/mt76/deep-sleep"
  echo "$RUN/mt76"
}
tx_channels() { echo 2437; }
exclusive_up() { record "exclusive_up $*"; echo wlan0 > "$RUN/exclusive"; echo 2437; }
cat() {
  if [[ "${1:-}" == /sys/class/net/wlan0/address ]]; then
    echo 02:00:00:00:00:01
  else
    command cat "$@"
  fi
}
iw() {
  record "iw $*"
  if [[ "$*" == 'dev wlan0 set type managed' && "$FAIL_TYPE_RESTORE" == 1 ]]; then return 1; fi
  case "$*" in
    'dev mon0 info') [[ "$FOREIGN" == mon0 || "$MON_PRESENT" == 1 ]]; return ;;
    'dev omgo0 info') [[ "$FOREIGN" == omgo0 || "$GO_PRESENT" == 1 ]]; return ;;
    'dev awdlibss0 info') [[ "$FOREIGN" == awdlibss0 || "$IBSS_PRESENT" == 1 ]]; return ;;
    'dev mon0 del')
      MON_DELETE_ATTEMPTS=$((MON_DELETE_ATTEMPTS + 1))
      [[ "$FAIL_MON_DELETE" != 1 || "$MON_DELETE_ATTEMPTS" -ne 1 ]] || return 1
      MON_PRESENT=0
      return 0
      ;;
    'dev omgo0 del')
      GO_DELETE_ATTEMPTS=$((GO_DELETE_ATTEMPTS + 1))
      [[ "$FAIL_GO_DELETE" != 1 || "$GO_DELETE_ATTEMPTS" -ne 1 ]] || return 1
      GO_PRESENT=0
      return 0
      ;;
    'dev awdlibss0 del')
      IBSS_DELETE_ATTEMPTS=$((IBSS_DELETE_ATTEMPTS + 1))
      [[ "$FAIL_IBSS_DELETE" != 1 || "$IBSS_DELETE_ATTEMPTS" -ne 1 ]] || return 1
      IBSS_PRESENT=0
      return 0
      ;;
    'phy '*" interface add omgo0 type __p2pgo") GO_PRESENT=1 ;;
    'phy '*" interface add mon0 type monitor") MON_PRESENT=1 ;;
  esac
  if [[ "$*" == 'dev wlan0 link' ]]; then [[ -z "$FREQ" ]] || echo "freq: $FREQ"; fi
  return 0
}
ip() {
  record "ip $*"
  if [[ "$*" == '-4 route show default dev wlan0' ]]; then echo 'default via 192.0.2.1 dev wlan0'; fi
  if [[ "$*" == '-6 addr show dev awdl0 scope link' && "$IPV6" == 1 ]]; then echo 'inet6 fe80::1/64 scope link'; fi
  if [[ "$*" == 'link set wlan0 up' && "$FAIL_LINK_RESTORE" == 1 ]]; then
    LINK_RESTORE_ATTEMPTS=$((LINK_RESTORE_ATTEMPTS + 1))
    [[ "$LINK_RESTORE_ATTEMPTS" -ne 1 ]] || return 1
  fi
  if [[ "$*" == 'link set wlan0 up' && "$FAIL_IDLE_LINK_RESTORE" == 1 ]]; then
    IDLE_LINK_RESTORE_ATTEMPTS=$((IDLE_LINK_RESTORE_ATTEMPTS + 1))
    [[ "$IDLE_LINK_RESTORE_ATTEMPTS" -ne 1 ]] || return 1
  fi
  return 0
}
nmcli() {
  record "nmcli $*"
  if [[ "$*" == '-g GENERAL.NM-MANAGED device show wlan0' ]]; then echo yes; fi
  if [[ "$*" == '-w 20 device connect wlan0' && "$FAIL_RECONNECT" == 1 ]]; then
    RECONNECT_ATTEMPTS=$((RECONNECT_ATTEMPTS + 1))
    [[ "$RECONNECT_ATTEMPTS" -ge 5 ]] || return 1
  fi
  if [[ "$*" == 'device set wlan0 managed yes' && "$FAIL_IDLE_MANAGED_RESTORE" == 1 ]]; then
    IDLE_MANAGED_RESTORE_ATTEMPTS=$((IDLE_MANAGED_RESTORE_ATTEMPTS + 1))
    [[ "$IDLE_MANAGED_RESTORE_ATTEMPTS" -ne 1 ]] || return 1
  fi
  return 0
}
sysctl() {
  record "sysctl $*"
  if [[ "$*" == '-qw net.ipv6.conf.wlan0.disable_ipv6=0' && "$FAIL_IPV6_RESTORE" == 1 ]]; then
    IPV6_RESTORE_ATTEMPTS=$((IPV6_RESTORE_ATTEMPTS + 1))
    [[ "$IPV6_RESTORE_ATTEMPTS" -ne 1 ]] || return 1
  fi
}
setsid() { record "setsid $*"; echo AP-ENABLED; }
''' + "\n" + extra + "\nwait\n"
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                                    text=True, timeout=10)
            commands = (root / "commands").read_text() if (root / "commands").exists() else ""
            return result, commands

    def test_shared_uses_ap_channel_and_intersection(self):
        result, commands = self.run_case(freq="5220")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-c 44 -N -S intersect -v", commands)
        self.assertNotIn("exclusive_up", commands)
        self.assertNotIn("device set wlan0 managed no", commands)

    def test_standalone_uses_configured_channel_without_gateway_or_ap(self):
        result, commands = self.run_case("standalone", freq="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iw dev mon0 set channel 6", commands)
        self.assertIn("-c 6 -N -S intersect -v", commands)
        self.assertIn("nmcli device set wlan0 managed no", commands)
        self.assertNotIn("route show default", commands)

    def test_standalone_refuses_associated_station(self):
        result, commands = self.run_case("standalone", freq="5180")
        self.assertEqual(result.returncode, 3)
        self.assertIn("needs an idle Wi-Fi card", result.stderr)
        self.assertNotIn("device set wlan0 managed no", commands)

    def test_dualchan_uses_independent_channel_and_wifi_direct(self):
        result, commands = self.run_case("dualchan", freq="5180")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iw phy phy0 interface add omgo0 type __p2pgo", commands)
        self.assertIn("HOSTAPD_P2P_GO=1", commands)
        self.assertIn("-c 149 -N -S intersect -v", commands)
        self.assertNotIn("route show default", commands)
        self.assertNotIn("device set wlan0 managed no", commands)

    def test_exclusive_uses_fixed_radio_pin_strategy(self):
        result, commands = self.run_case("exclusive", freq="5180")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exclusive_up wlan0 phy0 2437", commands)
        self.assertIn("-c 6 -N -S pin -F -E -v", commands)
        self.assertNotIn("route show default", commands)

    def test_foreign_interface_refusal_precedes_radio_changes(self):
        for mode, interface in (("exclusive", "mon0"), ("exclusive", "awdlibss0"),
                                ("dualchan", "omgo0"), ("standalone", "mon0")):
            with self.subTest(mode=mode, interface=interface):
                result, commands = self.run_case(mode, foreign=interface)
                self.assertEqual(result.returncode, 4)
                self.assertIn("refusing to replace", result.stderr)
                self.assertNotIn("exclusive_up", commands)
                self.assertNotIn("managed no", commands)
                self.assertNotIn("ip link set", commands)
                self.assertNotIn(" del", commands)

    def test_missing_link_local_address_fails_start_and_cleans_owned_monitor(self):
        result, commands = self.run_case(ipv6=False)
        self.assertEqual(result.returncode, 4)
        self.assertIn("no usable IPv6 link-local address", result.stderr)
        self.assertIn("iw dev mon0 del", commands)
        self.assertNotIn("__supervise", commands)

    def test_idle_stop_does_not_delete_foreign_radio_interfaces(self):
        result, commands = self.run_case(extra="cmd_stop stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("iw dev mon0 del", commands)
        self.assertNotIn("iw dev awdlibss0 del", commands)
        self.assertNotIn("iw dev omgo0 del", commands)

    def test_stop_reports_radio_restore_failure_and_keeps_retry_state(self):
        extra = '''
echo wlan0 > "$RUN/exclusive"
echo 0 > "$RUN/converted"
echo yes > "$RUN/managed"
echo 12345678-1234-1234-1234-123456789abc > "$RUN/connection"
echo wlan0 > "$RUN/station"
echo "fixture exclusive station no no" > "$RUN/profile"
echo "0 0" > "$RUN/pm"
if cmd_stop stopped; then exit 10; fi
[[ -e "$RUN/exclusive" && -e "$RUN/converted" && -e "$RUN/managed" && -e "$RUN/connection" && -e "$RUN/station" && -e "$RUN/profile" && -e "$RUN/pm" ]]
grep -q 'could not be restored; run stop again to retry' "$RUN/reason"
'''
        result, commands = self.run_case(extra=extra, fail_type_restore=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iw dev wlan0 set type managed", commands)
        self.assertIn("exclusive radio state could not be restored", result.stderr)
        self.assertIn("could not be restored; run stop again to retry", result.stderr)

    def test_stop_retries_failed_ipv6_and_link_restoration(self):
        extra = '''
echo wlan0 > "$RUN/exclusive"
echo 0 > "$RUN/converted"
echo yes > "$RUN/managed"
echo 12345678-1234-1234-1234-123456789abc > "$RUN/connection"
echo wlan0 > "$RUN/station"
echo "fixture exclusive station no no" > "$RUN/profile"
echo "0 0" > "$RUN/pm"
if cmd_stop stopped; then exit 10; fi
[[ -e "$RUN/exclusive" && -e "$RUN/converted" && -e "$RUN/connection" && -e "$RUN/type-restored" && -e "$RUN/station" && -e "$RUN/profile" && -e "$RUN/pm" ]] || exit 11
grep -q 'could not be restored; run stop again to retry' "$RUN/reason" || exit 12
cmd_stop retry || exit 13
[[ ! -e "$RUN/exclusive" && ! -e "$RUN/converted" && ! -e "$RUN/connection" && ! -e "$RUN/station" && ! -e "$RUN/profile" && ! -e "$RUN/pm" ]]
'''
        for failure in ("ipv6", "link"):
            with self.subTest(failure=failure):
                result, commands = self.run_case(
                    extra=extra,
                    fail_ipv6_restore=failure == "ipv6",
                    fail_link_restore=failure == "link",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if failure == "ipv6":
                    self.assertEqual(commands.count("sysctl -qw net.ipv6.conf.wlan0.disable_ipv6=0"), 2)
                else:
                    self.assertEqual(commands.count("ip link set wlan0 up"), 2)

    def test_stop_retries_owned_interfaces_station_and_power_restoration(self):
        extra = '''
MON_PRESENT=1
GO_PRESENT=1
IBSS_PRESENT=1
echo 1 > "$RUN/mon-owned"
echo 1 > "$RUN/go-owned"
echo 1 > "$RUN/ibss-owned"
echo 123 > "$RUN/go.pid"
echo config > "$RUN/hostapd.conf"
echo "wlan0 yes yes" > "$RUN/idle-station"
echo wlan0 > "$RUN/station"
echo "fixture standalone station no yes" > "$RUN/profile"
echo "0 0" > "$RUN/pm"
if cmd_stop stopped; then exit 10; fi
[[ -e "$RUN/mon-owned" && -e "$RUN/go-owned" && -e "$RUN/ibss-owned" && -e "$RUN/idle-station" && -e "$RUN/station" && -e "$RUN/profile" && -e "$RUN/pm" && -e "$RUN/hostapd.conf" ]] || exit 11
grep -q 'Radio state could not be restored' "$RUN/reason" || exit 12
cmd_stop retry || exit 13
[[ ! -e "$RUN/mon-owned" && ! -e "$RUN/go-owned" && ! -e "$RUN/ibss-owned" && ! -e "$RUN/idle-station" && ! -e "$RUN/station" && ! -e "$RUN/profile" && ! -e "$RUN/pm" && ! -e "$RUN/hostapd.conf" ]]
'''
        result, commands = self.run_case(
            extra=extra,
            fail_mon_delete=True,
            fail_go_delete=True,
            fail_ibss_delete=True,
            fail_idle_managed_restore=True,
            fail_idle_link_restore=True,
            fail_pm_restore=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("iw dev mon0 del", "iw dev omgo0 del", "iw dev awdlibss0 del",
                        "nmcli device set wlan0 managed yes", "ip link set wlan0 up"):
            self.assertEqual(commands.count(command), 2, command)

    def test_start_refuses_to_reuse_exclusive_state_when_restore_retry_fails(self):
        extra = '''
echo wlan0 > "$RUN/exclusive"
echo 0 > "$RUN/converted"
echo yes > "$RUN/managed"
echo 12345678-1234-1234-1234-123456789abc > "$RUN/connection"
echo "fixture exclusive station no no" > "$RUN/profile"
cmd_start 30
'''
        result, commands = self.run_case(extra=extra, fail_type_restore=True)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("Radio state could not be restored", result.stderr)
        self.assertNotIn("exclusive_up", commands)

    def test_start_retries_power_restore_before_opening_a_new_window(self):
        extra = '''
echo wlan0 > "$RUN/station"
echo "fixture shared station no yes" > "$RUN/profile"
echo "1 1" > "$RUN/pm"
cmd_start 30
[[ ! -e "$RUN/pm" ]]
[[ $(cat "$RUN/mt76/runtime-pm") == 1 && $(cat "$RUN/mt76/deep-sleep") == 1 ]]
'''
        result, commands = self.run_case(extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ip link set mon0 up", commands)

    def test_stop_retries_shared_reconnect_failure(self):
        extra = '''
echo wlan0 > "$RUN/station"
echo "fixture shared station yes no" > "$RUN/profile"
if cmd_stop stopped; then exit 10; fi
[[ -e "$RUN/station" && -e "$RUN/profile" ]] || exit 11
grep -q 'wlan0 did not reconnect after stop' "$RUN/reason" || exit 12
cmd_stop retry || exit 13
[[ ! -e "$RUN/station" && ! -e "$RUN/profile" ]]
'''
        result, commands = self.run_case(extra=extra, fail_reconnect=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands.count("nmcli -w 20 device connect wlan0"), 5)
        self.assertEqual(commands.count("iw dev wlan0 disconnect"), 1)

    def test_legacy_live_window_migrates_ownership_before_stopping(self):
        result, commands = self.run_case(extra='printf "fixture exclusive station no no\\n" > "$RUN/profile"\n'
                                               'cmd_stop stopped', legacy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("iw dev mon0 del", commands)
        self.assertIn("iw dev awdlibss0 del", commands)

    def test_stale_legacy_state_does_not_authorize_deletion(self):
        result, commands = self.run_case(extra='printf "fixture exclusive station no no\\n" > "$RUN/profile"\n'
                                               'cmd_stop stopped')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("iw dev mon0 del", commands)
        self.assertNotIn("iw dev awdlibss0 del", commands)


if __name__ == "__main__":
    unittest.main()
