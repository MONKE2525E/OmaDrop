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
//   OMADROP_SCREEN  output name for the bar and popup (default: first screen)
//   OMADROP_MAXH    override the panel's height cap (px) to test short screens
//   OMADROP_SHIFT   move the bar widget left by N px, which moves the popup's
//                   x and so its sub-pixel placement on fractionally scaled outputs
//   OMADROP_STEPS   semicolon-separated steps, run in order:
//     tab:N  click:LABEL[#n]  stage:/a|/b  droptext:TEXT  paste  choose
//     send:PEERID  wait:MS  shot:PATH  dump  audit  scroll:DY  close  open  drag:/path
//   `audit` logs controls that leave the card or the screen or are cut by the
//   scroll viewport.
//   `click` finds the nth visible, enabled control whose text is LABEL and
//   emits its clicked() signal, the same path a pointer press takes.
//   `shot` crops the popup (plus a margin, so its border is visible) with grim.
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
      if (!it || found.hit) return
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
        notifications: Object.keys(panel.notificationJobs).map(function(id) {
          return panel.notificationJobs[id] ? panel.notificationJobs[id].notificationId : 0
        }),
        out: v.outgoingList.map(function(t) { return t.state }),
        h: panel.card.contentHeight, w: panel.card.contentWidth, avail: panel.card.availableCardHeight }))
      break
    case "audit": audit(v); break
    case "scroll": v.scrollBy(parseInt(arg)); break
    case "shot": harness.shotPath = arg; shot.running = true; return 700
    case "wait": return parseInt(arg)
    }
    return 400
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
    onExited: console.log("STEP shot " + harness.shotPath + " exit=" + exitCode)
  }
}
