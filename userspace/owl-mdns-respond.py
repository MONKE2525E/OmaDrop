#!/usr/bin/env python3
"""Use the pinned mDNS responder with OWL's plain Ethernet TAP interface."""
import importlib

responder = importlib.import_module("awdl-mdns-respond")
original_wrap = responder.adv.wrap


def plain_wrap(mac, payload, seq):
    return original_wrap(mac, payload, seq, snap=False)


responder.adv.wrap = plain_wrap
if __name__ == "__main__":
    responder.main()
