import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "./ui"

// Test only: the real ui/OmaDropPanel.qml (real KeyboardPanel popup) under a
// stand-in bar, fed by test-harness/fake-omadrop.py.
//
//   OMADROP_SCREEN            output name for the bar and popup (default: first screen)
//   OMADROP_MAXH              override the panel's height cap (px) for short screens
//   OMADROP_SHIFT             move the bar widget left by N px (sub-pixel placement)
//   OMADROP_STARTUP_SECONDS   shorten the startup watchdog (default 65)
//   OMADROP_LEASE_SECONDS     shorten the start command's limit (default 70)
//   OMADROP_STEPS             semicolon-separated steps, run in order:
//     tab:N  focusnext:N  focusreport  click:LABEL[#n]  stage:/a|/b
//     droptext:TEXT  paste  choose  send:PEERID  wait:MS  shot:PATH
//     dump  audit  buttons  focusall  scroll:DY  close  open  drag:/path
//   `focusnext` moves keyboard focus along the focus chain, as Tab does, and
//   `focusreport` logs whether the focused control lies inside the scroll
//   viewport. `focusall` walks the whole chain once from the start and logs every
//   stop; a body control that is not fully in view is counted in bodyOutOfView.
//   `buttons` logs each visible control with its label, accessible name and
//   enabled state. `audit` logs controls that leave the card or the screen, or
//   that the scroll viewport cuts. `click` emits the nth visible, enabled control
//   whose text is LABEL, the path a pointer press takes. `shot` crops the popup
//   (plus a margin, so its border shows) with grim.
//   Every open and close is logged as `STEP opened=`. Outside presses cannot
//   close the popup in this harness (see isolate-dismiss.py), so a close here is
//   only the harness's own close or an error.
ShellRoot {
  id: harness
  readonly property string wanted: Quickshell.env("OMADROP_SCREEN") || ""
  readonly property var scr: {
    for (var i = 0; i < Quickshell.screens.length; i++)
      if (Quickshell.screens[i].name === wanted) return Quickshell.screens[i]
    return Quickshell.screens[0]
  }
  property var popout: null
  property var steps: (Quickshell.env("OMADROP_STEPS") || "").split(";").filter(function(s) { return s !== "" })
  property int stepIndex: 0
  property string shotPath: ""

  PluginBarApi {
    id: api
    pluginId: "monke.omadrop"
    moduleName: "monke.omadrop"
    foreground: Color.foreground
    barForeground: Color.foreground
    background: Color.background
    fontFamily: Style.font.family
    position: "top"
    barSize: 36
    _requestPopout: function(o) { harness.popout = o }
    _releasePopout: function(o) { if (harness.popout === o) harness.popout = null }
    activePopout: harness.popout
  }

  PanelWindow {
    screen: harness.scr
    anchors { top: true; left: true; right: true }
    implicitHeight: 36
    color: Color.background
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "omadrop-harness-bar"
    OmaDropPanel {
      id: panel
      bar: api
      manageIpc: false
      anchors.right: parent.right
      anchors.rightMargin: parseInt(Quickshell.env("OMADROP_SHIFT") || "0")
      anchors.verticalCenter: parent.verticalCenter
      Component.onCompleted: {
        // Exercise layer-shell geometry without taking the user's keyboard.
        panel.card.WlrLayershell.keyboardFocus = WlrKeyboardFocus.None
        var h = parseInt(Quickshell.env("OMADROP_MAXH") || "0")
        if (h > 0) panel.maxContentHeight = h
        // Shorter startup bounds run the same stall and timeout logic in seconds.
        var su = parseInt(Quickshell.env("OMADROP_STARTUP_SECONDS") || "0")
        if (su > 0) panel.startupSeconds = su
        var lu = parseInt(Quickshell.env("OMADROP_LEASE_SECONDS") || "0")
        if (lu > 0) panel.leaseLimitSeconds = lu
      }
    }
  }

  // A source inside the popup's own scene, so the DropArea receives a real
  // drag-and-drop sequence (enter, move, drop) carrying file URLs.
  Item {
    id: dragSource
    parent: panel.body
    x: 8; y: 8; width: 40; height: 40; z: 100
    Drag.hotSpot.x: 20; Drag.hotSpot.y: 20
    property string uri: ""
    Drag.active: false
    Drag.dragType: Drag.Internal
    Drag.supportedActions: Qt.CopyAction
    Drag.mimeData: ({ "text/uri-list": dragSource.uri })
    Drag.onDragStarted: console.log("STEP drag started")
    Drag.onDragFinished: function(a) { console.log("STEP drag finished " + a) }
  }

  Timer {
    id: dragMove
    interval: 200
    onTriggered: {
      if (!dragSource.Drag.active) return
      dragSource.x = 120; dragSource.y = 200; console.log('STEP drag target=' + dragSource.Drag.target + ' parent=' + dragSource.parent + ' active=' + dragSource.Drag.active + ' win=' + dragSource.Window.window)
      dragDrop.restart()
    }
  }
  Timer {
    id: dragDrop
    interval: 200
    onTriggered: { dragSource.Drag.drop(); dragSource.Drag.active = false }
  }

  function findControl(item, label, skip) {
    // Depth-first, document order; returns the skip-th match.
    var found = { n: skip, hit: null }
    function visit(it) {
      // A hidden parent hides its children too, so their own `visible` is not enough.
      if (!it || !it.visible || found.hit) return
      if (it.visible && it.enabled !== false && it.text === label && typeof it.clicked === "function") {
        if (found.n === 0) { found.hit = it; return }
        found.n--
      }
      var kids = it.children || []
      for (var i = 0; i < kids.length; i++) visit(kids[i])
      if (it.contentItem && it.contentItem.children) {
        var inner = it.contentItem.children
        for (var j = 0; j < inner.length; j++) visit(inner[j])
      }
    }
    visit(item)
    return found.hit
  }

  // Reports every visible control whose box leaves the popup card or the
  // screen, and every one the scroll viewport cuts (reachable by scrolling,
  // but not whole). A clean popup prints "audit n=<count> outside=0".
  function audit(v) {
    var card = panel.card
    var flick = null
    function find(it) {
      if (flick || !it) return
      if (it.contentHeight !== undefined && it.contentY !== undefined && it.visible) { flick = it; return }
      var kids = it.children || []
      for (var i = 0; i < kids.length; i++) find(kids[i])
    }
    find(v)
    var fr = flick ? flick.mapToItem(null, 0, 0) : null
    var n = 0, outside = [], cut = []
    function visit(it) {
      if (!it || !it.visible) return
      if (typeof it.clicked === "function" && it.width > 0) {
        n++
        var p = it.mapToItem(null, 0, 0)
        var cx = card.cardOrigin.x, cy = card.cardOrigin.y
        var inCard = p.x >= cx && p.y >= cy && p.x + it.width <= cx + card.contentWidth && p.y + it.height <= cy + card.contentHeight
        var onScreen = p.x >= 0 && p.y >= 0 && p.x + it.width <= harness.scr.width && p.y + it.height <= harness.scr.height
        var label = it.text || it.Accessible.name || "?"
        if (!inCard || !onScreen) outside.push(label + "@" + Math.round(p.x) + "," + Math.round(p.y) + " " + Math.round(it.width) + "x" + Math.round(it.height))
        else if (flick) {
          var inFlick = p.y >= fr.y && p.y + it.height <= fr.y + flick.height
          var isIn = p.y + it.height > fr.y && p.y < fr.y + flick.height && p.x >= fr.x - 0.5
          if (isIn && !inFlick) cut.push(label)
        }
      }
      var kids = it.children || []
      for (var i = 0; i < kids.length; i++) visit(kids[i])
    }
    visit(v)
    console.log("STEP audit n=" + n + " outside=" + outside.length + " " + JSON.stringify(outside) + " scrollCut=" + JSON.stringify(cut))
  }

  // The scroll viewport that holds the popup body, found as `audit` finds it.
  function bodyFlick(v) {
    var found = null
    function find(it) {
      if (found || !it) return
      if (it.contentHeight !== undefined && it.contentY !== undefined && it.visible) { found = it; return }
      var kids = it.children || []
      for (var i = 0; i < kids.length; i++) find(kids[i])
    }
    find(v)
    return found
  }

  // Tab, n times: each press moves focus to the next control in the focus chain.
  function focusNext(n) {
    for (var k = 0; k < n; k++) {
      var cur = panel.body.Window.activeFocusItem
      var nxt = cur ? cur.nextItemInFocusChain(true) : panel.body.nextItemInFocusChain(true)
      if (nxt) nxt.forceActiveFocus()
    }
  }

  // Where a focused control sits. A control in the scrolling body must lie fully
  // inside the viewport; a control in the fixed header or footer is always shown.
  function placeOf(cur) {
    var label = cur.text !== undefined ? cur.text : "?"
    var flick = bodyFlick(panel.body)
    if (!flick) return { ok: true, line: label + " scope=none" }
    // Only controls inside the scrolling viewport count; the view itself is the
    // whole popup, so the test is for the Flickable and not for the view.
    var inBody = false
    for (var it = cur; it; it = it.parent) if (it === flick) { inBody = true; break }
    if (!inBody) return { ok: true, line: label + " scope=fixed" }
    var p = cur.mapToItem(flick, 0, 0)
    var inView = p.y >= 0 && p.y + cur.height <= flick.height
    return { ok: inView, line: label + " scope=body y=" + Math.round(p.y) + " h=" + Math.round(cur.height)
      + " viewport=" + Math.round(flick.height) + " inView=" + inView }
  }

  function focusReport() {
    var cur = panel.body.Window.activeFocusItem
    if (!cur) { console.log("STEP focus none"); return }
    console.log("STEP focus " + placeOf(cur).line)
  }

  // Tab once through the whole focus chain from its start, logging each stop. The
  // walk ends when the chain returns to the start.
  function focusAll() {
    var start = panel.body
    var cur = start
    var visited = []
    var seen = 0, bad = 0
    for (var k = 0; k < 400; k++) {
      var nxt = cur.nextItemInFocusChain(true)
      // The chain may wrap to an item other than the start, so stop at any repeat.
      if (!nxt || nxt === start || visited.indexOf(nxt) >= 0) break
      visited.push(nxt)
      nxt.forceActiveFocus()
      cur = nxt
      seen++
      var r = placeOf(nxt)
      if (!r.ok) bad++
      console.log("STEP focusall " + r.line)
    }
    console.log("STEP focusall n=" + seen + " bodyOutOfView=" + bad)
  }

  // Every visible control with its label, accessible name and enabled state.
  function listButtons(v) {
    var n = 0
    function visit(it) {
      if (!it || !it.visible) return
      // Buttons carry a text; the unlabeled clicked handlers inside them are skipped.
      if (it.text !== undefined && typeof it.clicked === "function" && it.width > 0) {
        n++
        var name = it.Accessible ? it.Accessible.name : ""
        console.log("STEP btn " + (it.text || "?") + " | " + (name || "") + " | enabled=" + it.enabled)
      }
      var kids = it.children || []
      for (var i = 0; i < kids.length; i++) visit(kids[i])
    }
    visit(v)
    console.log("STEP buttons n=" + n)
  }

  function runStep(step) {
    var i = step.indexOf(":")
    var name = i < 0 ? step : step.substring(0, i)
    var arg = i < 0 ? "" : step.substring(i + 1)
    var v = panel.body
    switch (name) {
    case "tab": v.selectTab(parseInt(arg)); break
    case "click": {
      var h = arg.lastIndexOf("#")
      var label = h > 0 ? arg.substring(0, h) : arg
      var nth = h > 0 ? parseInt(arg.substring(h + 1)) : 0
      var c = findControl(v, label, nth)
      console.log("STEP click " + arg + " -> " + (c ? "hit" : "MISSING"))
      if (c) c.clicked()
      break
    }
    case "stage": v.pathsDropped(arg.split("|")); break
    case "drag":
      dragSource.uri = "file://" + arg
      dragSource.x = 8; dragSource.y = 8
      dragSource.Drag.active = true
      dragMove.restart()
      return 900
    case "droptext": v.textDropped(arg); break
    case "paste": v.pasteRequested(); break
    case "notify": panel.notificationsEnabled = true; panel.syncNotifications(panel.status.incoming); break
    case "notifyclear": panel.notificationsEnabled = false; panel.syncNotifications([]); break
    case "choose": v.chooseFilesRequested(); break
    case "send": v.sendRequested(arg); break
    case "close": panel.close(); break
    case "open": panel.open(); break
    case "dump":
      console.log("STEP dump " + JSON.stringify({
        opened: panel.opened, tab: v.tab, staged: panel.staged.map(function(f) { return f.name }),
        staging: panel.staging, sendingPeer: panel.sendingPeer, actionError: panel.actionError,
        leaseError: panel.leaseError, mode: v.mode, leaseWanted: panel.leaseWanted,
        startupWaiting: panel.startupWaiting, startupStalled: panel.startupStalled,
        startupFailed: panel.startupFailed, starting: panel.backendStarting,
        notifications: Object.keys(panel.notificationJobs).map(function(id) {
          return panel.notificationJobs[id] ? panel.notificationJobs[id].notificationId : 0
        }),
        out: v.outgoingList.map(function(t) { return t.state }),
        h: panel.card.contentHeight, w: panel.card.contentWidth, avail: panel.card.availableCardHeight }))
      break
    case "audit": audit(v); break
    case "focusnext": focusNext(parseInt(arg)); break
    case "focusreport": focusReport(); break
    case "focusall": focusAll(); break
    case "buttons": listButtons(v); break
    case "scroll": v.scrollBy(parseInt(arg)); break
    case "shot": harness.shotPath = arg; shot.running = true; return 700
    case "wait": return parseInt(arg)
    }
    return 400
  }

  Connections {
    target: panel
    function onOpenedChanged() { console.log("STEP opened=" + panel.opened) }
    function onStartupWaitingChanged() { console.log("STEP startup waiting=" + panel.startupWaiting) }
    function onStartupStalledChanged() { console.log("STEP startup stalled=" + panel.startupStalled) }
    function onStartupFailedChanged() { console.log("STEP startup failed=" + panel.startupFailed + " error=" + panel.leaseError) }
  }

  Timer {
    id: stepper
    interval: 1500
    running: true
    onTriggered: {
      if (harness.stepIndex === 0) panel.open()
      if (harness.stepIndex >= harness.steps.length) {
        console.log("PANEL done opened=" + panel.opened)
        Qt.quit()
        return
      }
      var wait = harness.runStep(harness.steps[harness.stepIndex++])
      stepper.interval = wait
      stepper.restart()
    }
  }

  // grim wants layout coordinates: the output origin plus the card position.
  Process {
    id: shot
    command: {
      var s = harness.scr
      var m = 4
      var x = Math.max(0, panel.card.cardOrigin.x - m), y = Math.max(0, panel.card.cardOrigin.y - m)
      var w = Math.min(s.width - x, panel.card.contentWidth + 2 * m)
      var h = Math.min(s.height - y, panel.card.contentHeight + 2 * m)
      var cap = parseInt(Quickshell.env("OMADROP_SHOT_MAXH") || "0")
      if (cap > 0) h = Math.min(h, cap)
      return ["grim", "-g", (s.x + x) + "," + (s.y + y) + " " + w + "x" + h, harness.shotPath]
    }
    onExited: function(code) { console.log("STEP shot " + harness.shotPath + " exit=" + code) }
  }
}
