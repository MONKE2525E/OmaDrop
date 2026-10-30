#!/usr/bin/env python3
"""Writes a test-only copy of Omarchy's KeyboardPanel.qml. Test use only.

On a shared desktop a person's press outside the popup closes it, because the
dismiss surface takes every pointer event on the output and on the others. This
copy keeps the component's layout and API and only removes that input, so a
scripted run cannot be closed by someone else's clicks:

  - the overlay input region covers only the panel card,
  - its outside-click MouseArea is disabled,
  - the dismiss surfaces on the other outputs are not shown.

Production stays as it is: this writes to the run's temporary config directory
and never touches /usr/share or the user's configuration.

Usage: isolate-dismiss.py <source KeyboardPanel.qml> <destination>
"""
import sys

EDITS = [
    # The overlay's input region: the whole screen in production.
    ("  mask: Region {\n    width: root.screenW\n    height: root.screenH\n  }",
     "  mask: Region { x: root.cardOrigin.x; y: root.cardOrigin.y; width: root.contentWidth; height: root.drawnHeight }  // test: card input only"),
    # The outside-click MouseArea that closes the popup in production.
    ("    enabled: root.open\n    acceptedButtons: Qt.AllButtons\n    hoverEnabled: true",
     "    enabled: false  // test: no outside-click dismissal\n"
     "    acceptedButtons: Qt.AllButtons\n    hoverEnabled: true"),
    # The per-output twins that catch a press on the other monitors.
    ("        visible: root.open && !!root.screen && modelData.name !== root.screen.name",
     "        visible: false  // test: no dismiss twins"),
]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().splitlines()[-1])
    with open(sys.argv[1], encoding="utf-8") as source:
        text = source.read()
    for old, new in EDITS:
        found = text.count(old)
        if found != 1:
            sys.exit("isolate-dismiss: expected one match, found %d for %r" % (found, old.splitlines()[0]))
        text = text.replace(old, new)
    with open(sys.argv[2], "w", encoding="utf-8") as out:
        out.write("// TEST-ONLY copy from test-harness/isolate-dismiss.py. Not production.\n" + text)


if __name__ == "__main__":
    main()
