#!/usr/bin/env python3
"""Stand-in for the installed `omadrop` command. Test use only.

Reads OMADROP_FAKE_DIR for its state and logs every argv to argv.log, so the
UI's process wiring can be checked without a live service or radio.

  state.json   the fixture, edited in place (a peer file is a normal status)
  argv.log     one line per invocation, args joined by spaces
  stdin.log    what `stage-text` received

Behaviour:
  status           prints state.json with transfers advanced by wall clock
  lease/release    mode menu/off (a window stays a window)
  start/stop       window/off
  send             adds a transfer: waiting 3 s, then transferring 6 s, then
                   complete; a peer named "Reject*" is declined, "Fail*" fails
  pick             prints OMADROP_FAKE_PICK (json) or {"files": []}; sleeps
                   OMADROP_FAKE_PICK_DELAY seconds first
  stage-clipboard  prints OMADROP_FAKE_CLIP (json) or an empty-clipboard error
  stage-text       stages stdin as a text file

Switches: OMADROP_FAKE_LEASE_HANG (the first lease never answers),
OMADROP_FAKE_LEASE_FAIL (every lease fails), OMADROP_FAKE_DISMISS_FAIL (dismiss
is refused), OMADROP_FAKE_DISMISS_DELAY (seconds before a dismiss answers).
A lease that answers clears `starting` on the protocols.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

root = Path(os.environ["OMADROP_FAKE_DIR"])
state_path = root / "state.json"
args = sys.argv[1:]
with (root / "argv.log").open("a") as log:
    log.write(" ".join(args) + "\n")


def load():
    return json.loads(state_path.read_text())


def save(state):
    state_path.write_text(json.dumps(state))


def advance(state):
    now = time.time()
    for t in state.get("transfers", []):
        if t.get("direction") != "out" or "startedAt" not in t:
            continue
        age = now - t["startedAt"]
        if t["state"] in ("complete", "failed", "rejected", "cancelled"):
            continue
        if t["peer"].startswith("Reject") and age > 3:
            t.update(state="rejected", error="Declined on the other device")
        elif t["peer"].startswith("Fail") and age > 4:
            t.update(state="failed", error="Connection reset by peer")
        elif age > 9:
            t.update(state="complete", bytes=t["total"])
        elif age > 3 and not t["peer"].startswith(("Reject", "Fail")):
            t.update(state="transferring", bytes=int(t["total"] * min(1, (age - 3) / 6)))
        t["updated"] = now
    return state


def public(state):
    out = dict(state)
    out["transfers"] = [{k: v for k, v in t.items() if k != "startedAt"} for t in state.get("transfers", [])]
    # An incoming request may carry `expiresIn` (seconds); turn it into the
    # `expiresAt` epoch the backend would send, so a fixture never goes stale.
    out["incoming"] = [dict(i, expiresAt=int(time.time() + i["expiresIn"])) if "expiresIn" in i else i
                       for i in state.get("incoming", [])]
    return out


def main():
    if not args:
        return 2
    command = args[0]
    if command == "dismiss":
        # Lets a run see the row while its Clear is still in flight.
        time.sleep(float(os.environ.get("OMADROP_FAKE_DISMISS_DELAY", "0")))
    state = advance(load())
    if command == "status":
        print(json.dumps(public(state)))
    elif command == "lease" and os.environ.get("OMADROP_FAKE_LEASE_HANG") and not (root / "hung").exists():
        # The first lease never answers: the service is stuck starting its
        # radios. Later leases answer, so a Retry can succeed.
        (root / "hung").write_text("1")
        time.sleep(float(os.environ["OMADROP_FAKE_LEASE_HANG"]))
        return 1
    elif command == "lease" and os.environ.get("OMADROP_FAKE_LEASE_FAIL"):
        print(json.dumps({"error": os.environ["OMADROP_FAKE_LEASE_FAIL"]}))
        return 1
    elif command == "lease":
        if state.get("mode") != "window":
            state["mode"] = "menu"
        state["running"] = True
        # Once a lease answers the radios are up: starting protocols advertise.
        state["starting"] = False
        for p in state.get("protocols", []):
            if p.get("state") == "starting":
                p.update(state="advertising", reason="")
        save(state)
        print(json.dumps(public(state)))
    elif command == "release":
        if state.get("mode") != "window":
            state["mode"] = "off"
            state["running"] = False
        save(state)
        print(json.dumps(public(state)))
    elif command == "start":
        state.update(mode="window", running=True, remainingSeconds=int(args[args.index("--seconds") + 1]))
        save(state)
        print(json.dumps(public(state)))
    elif command == "stop":
        state.update(mode="off", running=False, remainingSeconds=None)
        save(state)
        print(json.dumps(public(state)))
    elif command == "send":
        peer_id = args[args.index("--peer") + 1]
        files = args[args.index("--") + 1:] if "--" in args else []
        peer = next((p for p in state["peers"] if p["id"] == peer_id), None)
        if not peer:
            print(json.dumps({"error": "Device is no longer nearby"}))
            return 1
        if not files:
            print(json.dumps({"error": "Choose 1 to 100 files"}))
            return 1
        sizes = [Path(f).stat().st_size for f in files if Path(f).is_file()]
        transfer = {
            "id": peer["protocol"] + ":" + str(uuid.uuid4()), "name": ", ".join(Path(f).name for f in files),
            "peer": peer["name"], "protocol": peer["protocol"], "direction": "out", "bytes": 0,
            "total": sum(sizes) or 1000, "state": "waiting", "error": "", "files": len(files),
            "fileName": Path(files[0]).name, "updated": time.time(), "startedAt": time.time(),
        }
        state.setdefault("transfers", []).append(transfer)
        save(state)
        print(json.dumps({"ok": True, "transferId": transfer["id"], "transfer": transfer, **public(state)}))
    elif command in ("accept", "reject", "cancel"):
        ident = args[1]
        if command == "cancel":
            for t in state.get("transfers", []):
                if t["id"] == ident:
                    t["state"] = "cancelled"
        else:
            state["incoming"] = [i for i in state.get("incoming", []) if i["id"] != ident]
        save(state)
        print(json.dumps({"ok": True}))
    elif command == "dismiss" and os.environ.get("OMADROP_FAKE_DISMISS_FAIL"):
        # The backend keeps the finished transfer and refuses to forget it.
        print(json.dumps({"error": os.environ["OMADROP_FAKE_DISMISS_FAIL"]}))
        return 1
    elif command == "dismiss":
        state["transfers"] = [t for t in state.get("transfers", [])
                              if t["id"] != args[1] or t["state"] in ("waiting", "transferring", "preparing")]
        save(state)
        print(json.dumps({"ok": True}))
    elif command == "pick":
        time.sleep(float(os.environ.get("OMADROP_FAKE_PICK_DELAY", "0")))
        print(os.environ.get("OMADROP_FAKE_PICK", json.dumps({"files": []})))
    elif command == "stage-clipboard":
        print(os.environ.get("OMADROP_FAKE_CLIP", json.dumps({"files": [], "error": "Clipboard is empty"})))
    elif command == "stage-text":
        text = sys.stdin.read()
        (root / "stdin.log").write_text(text)
        folder = root / "staged"
        folder.mkdir(exist_ok=True)
        path = folder / "clipboard-text.txt"
        path.write_text(text)
        print(json.dumps({"files": [{"path": str(path), "name": path.name, "size": len(text.encode()), "kind": "text"}]}))
    elif command == "directory":
        print(json.dumps({"cancelled": True}))
    else:
        print(json.dumps({"error": "unknown command " + command}))
        return 2
    return 0


sys.exit(main())
