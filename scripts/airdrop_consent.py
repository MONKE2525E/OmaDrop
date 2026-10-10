"""Fail-closed approval bridge for the pinned AirDrop receiver."""
import hashlib
import json
import logging
import re
import select
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_grants = {}
_lock = threading.Lock()


def request_approval(handler, request):
    poll = select.poll()
    poll.register(handler.connection.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLRDHUP)
    process = subprocess.Popen(
        [str(Path.home() / ".local/bin/omadrop"), "receive-request"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 35
    content = json.dumps(request).encode()
    try:
        while time.monotonic() < deadline:
            events = poll.poll(0)
            if events:
                if events[0][1] & (select.POLLHUP | select.POLLERR | select.POLLRDHUP):
                    return {"accepted": False}
                # TLS close_notify arrives as readable encrypted data before
                # the TCP socket closes. Process it so an aborted Ask cancels
                # immediately. No next HTTP request is valid before our reply.
                previous_timeout = handler.connection.gettimeout()
                try:
                    handler.connection.setblocking(False)
                    handler.connection.recv(1)
                    return {"accepted": False}
                except (ssl.SSLWantReadError, BlockingIOError):
                    pass
                finally:
                    handler.connection.settimeout(previous_timeout)
            try:
                output, errors = process.communicate(input=content, timeout=0.15)
                if process.returncode:
                    logging.warning("AirDrop approval bridge failed: %s", errors[-1000:])
                    return {"accepted": False}
                return json.loads(output)
            except subprocess.TimeoutExpired:
                content = None
        return {"accepted": False}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def peer_key(handler, transfer_id):
    certificate = handler.connection.getpeercert(binary_form=True) or b""
    return (handler.client_address[0], hashlib.sha256(certificate).digest(), transfer_id)


def approve(handler, ask):
    try:
        transfer_id = ask.get("TransferID", "")
        if isinstance(transfer_id, dict):
            transfer_id = transfer_id.get("id", "")
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", transfer_id):
            return False
        files = ask.get("Files") or []
        items = ask.get("Items") or []
        if items:
            if files or "links" not in (ask.get("TransferType") or {}):
                return False
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                return False
            contents = []
            for item in items:
                if not isinstance(item, str) or any(ord(c) < 32 or ord(c) == 127 for c in item):
                    return False
                parts = urlsplit(item)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    return False
                contents.append((item + "\n").encode("utf-8"))
            if sum(map(len, contents)) > 1024 * 1024:
                return False
            request = {"peer": str(ask.get("SenderComputerName") or "Apple device")[:100],
                       "kind": "text", "description": "\n".join(items)[:200], "urls": items,
                       "files": [{"name": "Website link.txt" if len(items) == 1 else f"Website link {i + 1}.txt",
                                  "size": len(content)} for i, content in enumerate(contents)]}
            result = request_approval(handler, request)
            if result.get("accepted") is not True:
                return False
            handler._omadrop_link_consent = {"contents": contents,
                "id": result.get("transferId"), "token": result.get("token"), "bytes": 0, "reported": 0}
            return True
        if "links" in (ask.get("TransferType") or {}) or not isinstance(files, list) or not 1 <= len(files) <= 100:
            return False
        expected = {}
        for row in files:
            name, size = row["FileName"], row["FileSize"]
            if not isinstance(name, str) or not name or name in expected:
                return False
            if type(size) is not int or size < 0:
                return False
            expected[name] = size
        request = {"peer": str(ask.get("SenderComputerName") or "Apple device")[:100],
                   "files": [{"name": name, "size": size} for name, size in expected.items()]}
        key = peer_key(handler, transfer_id)
        result = request_approval(handler, request)
        if result.get("accepted") is not True:
            return False
        with _lock:
            now = time.monotonic()
            for expired in [key for key, grant in _grants.items() if grant["until"] <= now]:
                _grants.pop(expired)
            if len(_grants) >= 100:
                return False
            _grants[key] = {"until": now + 60, "expected": expected,
                            "id": result.get("transferId"), "token": result.get("token"),
                            "bytes": 0, "reported": 0}
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError) as error:
        logging.warning("AirDrop approval bridge refused request: %s", error)
        return False


def consume(handler):
    try:
        key = peer_key(handler, handler.headers.get("TransferID", ""))
        with _lock:
            grant = _grants.pop(key, None)
        if grant and grant["until"] > time.monotonic():
            return grant
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def store_links(handler, destination):
    """Complete the approved Ask; the desktop coordinator opens its websites."""
    grant = getattr(handler, "_omadrop_link_consent", None)
    handler._omadrop_link_consent = None
    if not grant:
        raise ValueError("Links require an approved Ask")
    report(grant, count=sum(map(len, grant["contents"])))
    report(grant, "complete")
    return grant["contents"]


def check_file(grant, name, size):
    if grant is None or grant["expected"].pop(name, None) != size:
        raise ValueError("Archive does not match the approved files")


def report(grant, state="transferring", count=0):
    grant["bytes"] += count
    now = time.monotonic()
    if not grant.get("id") or (state == "transferring" and now - grant["reported"] < 0.25):
        return
    grant["reported"] = now
    packet = {"id": grant["id"], "token": grant["token"], "state": state, "bytes": grant["bytes"]}
    if state == "failed":
        packet["error"] = "AirDrop upload did not finish"
    try:
        result = subprocess.run([str(Path.home() / ".local/bin/omadrop"), "receive-progress"],
                                input=json.dumps(packet), capture_output=True, text=True, timeout=15 if state == "complete" else 2)
        if state == "complete" and (result.returncode or json.loads(result.stdout).get("error")):
            raise ValueError("Could not finish the approved transfer")
    except (OSError, subprocess.TimeoutExpired) as error:
        if state == "complete":
            raise ValueError("Could not finish the approved transfer") from error
        logging.warning("AirDrop receive progress could not be reported")


def finish_upload(grant, state, release_slot):
    """Report the terminal state and release the bounded receiver slot."""
    try:
        report(grant, state)
    finally:
        release_slot()
