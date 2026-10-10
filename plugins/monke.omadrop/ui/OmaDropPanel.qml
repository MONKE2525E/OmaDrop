import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// OmaDrop bar widget: AirDrop, Quick Share and LocalSend in one popup with two
// tabs, Send and Receive.
//
// The `omadrop` command installed beside this file is the only source of
// truth. It owns the service, which keeps running when this panel is closed;
// the panel polls `omadrop status` and runs every action as its own Process
// with an argv array, so a device name can never reach a shell.
//
// Presence. Nothing advertises by default. While the panel is open (or while a
// picker, a send, an incoming request or a transfer needs the radios) the
// panel renews a short `omadrop lease`; when none of those hold it sends
// `omadrop release`. The lease expires on its own if the shell dies. An
// explicit 10-minute window (`omadrop start`) is separate: it survives closing
// the panel and ends by itself.
Panel {
  id: root
  moduleName: "monke.omadrop"
  ipcTarget: "monke.omadrop"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Shipped beside this file so the widget works without anything on PATH.
  readonly property string cli: Qt.resolvedUrl("../bin/omadrop").toString().replace("file://", "")

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property var status: null
  property string backendError: ""
  property string actionError: ""
  property string leaseError: ""
  property var busy: ({})
  property bool pickingDirectory: false

  // Content waiting for a recipient: [{path, name, size, kind}].
  property var staged: []
  // "" | "picking" | "pasting". The picker is a separate window; the panel
  // closes for it and comes back when it returns.
  property string staging: ""
  property string sendingPeer: ""
  // Quiet note under the share area, for outcomes that are expected rather
  // than wrong, such as pasting an empty clipboard. Clears on its own.
  property string stageHint: ""
  property bool notificationsEnabled: manageIpc
  property var notificationJobs: ({})

  // Tallest the popup may grow before the body scrolls; also lets a test
  // simulate a short screen.
  property int maxContentHeight: Style.space(640)
  // Test hooks: the popup card and its body.
  readonly property alias card: panel
  readonly property alias body: view

  readonly property int incomingCount: status && status.incoming ? status.incoming.length : 0
  readonly property int liveTransfers: status && status.transfers
    ? status.transfers.filter(function(t) {
        return t.state === "preparing" || t.state === "waiting" || t.state === "transferring"
      }).length : 0

  // The radios are needed while the panel is open and for as long as work
  // that depends on them is pending. Everything else lets presence lapse.
  readonly property bool leaseWanted: opened || staging === "picking" || pickingDirectory
    || sendingPeer !== "" || liveTransfers > 0 || incomingCount > 0

  // ---------------------------------------------------------------- status
  //
  // A request that arrives while a poll is running is queued, not dropped: an
  // answer that began before an action finished is stale and must not paint
  // the old state back over the new one.
  property bool statusStale: false

  function refreshStatus() {
    if (statusProc.running) statusStale = true
    else statusProc.running = true
  }

  function applyStatus(text, code, errText) {
    var parsed = null
    try { parsed = JSON.parse(text) } catch (e) { parsed = null }
    if (parsed === null || typeof parsed !== "object") {
      backendError = code === 127 || code === -1 ? "omadrop is not installed next to this plugin."
        : (errText.trim() !== "" ? errText.trim().split("\n").pop()
           : "omadrop status gave no usable answer (exit " + code + ").")
      return
    }
    backendError = ""
    status = parsed
    syncNotifications(parsed.incoming || [])
  }

  // One transient toast per request. Clicking it opens Receive; acceptance
  // stays inside the panel. Withdraw when the sender cancels or a decision
  // removes the request, so an expired request cannot linger as a toast.
  Component {
    id: closeNotificationComp
    Process {
      onExited: destroy()
    }
  }

  function closeNotification(id, summary) {
    if (!id) return
    var proc = closeNotificationComp.createObject(root, {
      command: ["busctl", "--user", "call", "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
        "CloseNotification", "u", String(id)]
    })
    proc.running = true
    // The shell retains popup rows after the freedesktop object closes.
    // Dismiss the matching visible toast as well as closing its live action.
    var dismiss = closeNotificationComp.createObject(root, {
      command: ["omarchy-shell", "notifications", "dismiss", summary]
    })
    dismiss.running = true
  }

  Component {
    id: notificationComp
    Process {
      id: toast
      property string requestId: ""
      property int notificationId: 0
      property string summary: ""
      property bool withdrawn: false
      function withdraw() {
        withdrawn = true
        root.closeNotification(notificationId, summary)
      }
      stdout: SplitParser {
        onRead: function(line) {
          var value = String(line).trim()
          if (/^[0-9]+$/.test(value)) {
            toast.notificationId = Number(value)
            if (toast.withdrawn) root.closeNotification(toast.notificationId, toast.summary)
          } else if (value === "default" && !toast.withdrawn) {
            view.selectTab(1)
            root.open()
          }
        }
      }
      onExited: {
        if (root.notificationJobs[requestId] === toast) root.notificationJobs[requestId] = null
        destroy()
      }
    }
  }

  function syncNotifications(requests) {
    var next = {}
    if (notificationsEnabled) requests.forEach(function(request) {
      if (!request.id) return
      if (Object.prototype.hasOwnProperty.call(notificationJobs, request.id)) {
        next[request.id] = notificationJobs[request.id]
        return
      }
      var protocol = request.protocol === "airdrop" ? "AirDrop"
        : request.protocol === "quickshare" ? "Quick Share" : "LocalSend"
      var summary = "Share from " + String(request.name || "Nearby device").replace(/[\r\n\t]/g, " ").slice(0, 80)
        + " via " + protocol
      var proc = notificationComp.createObject(root, {
        requestId: request.id,
        summary: summary,
        // A bar widget exists on each output. The private runtime lock makes
        // only one of those instances announce the same incoming request.
        command: ["flock", "--nonblock", "--no-fork",
          Quickshell.env("XDG_RUNTIME_DIR") + "/omadrop/notification-" + String(request.id).replace(/[^a-zA-Z0-9_-]/g, "_"),
          "notify-send", "--app-name=OmaDrop", "--transient", "--print-id",
          "--expire-time=30000", "--action=default=Review request", "--",
          summary, "Open Receive to accept or reject."]
      })
      next[request.id] = proc
      proc.running = true
    })
    Object.keys(notificationJobs).forEach(function(id) {
      if (!Object.prototype.hasOwnProperty.call(next, id) && notificationJobs[id])
        notificationJobs[id].withdraw()
    })
    notificationJobs = next
  }

  Component.onDestruction: Object.keys(notificationJobs).forEach(function(id) {
    if (notificationJobs[id]) notificationJobs[id].withdraw()
  })

  Process {
    id: statusProc
    command: [root.cli, "status"]
    stdout: StdioCollector {
      id: statusOut
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: statusErr
      waitForEnd: true
    }
    onExited: function(code) {
      if (root.statusStale) {
        root.statusStale = false
        statusProc.running = true
        return
      }
      root.applyStatus(statusOut.text, code, statusErr.text)
    }
  }

  Timer {
    interval: root.liveTransfers > 0 || root.incomingCount > 0 ? 1000
      : root.opened || (root.status && root.status.running) ? 2000 : 10000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: if (!statusProc.running) statusProc.running = true
  }

  // -------------------------------------------------------------- presence
  Process {
    id: leaseProc
    stdout: StdioCollector { id: leaseOut; waitForEnd: true }
    stderr: StdioCollector { id: leaseErr; waitForEnd: true }
    onExited: function(code) {
      root.leaseError = code === 0 ? "" : root.failureText(code, leaseOut.text, leaseErr.text)
      root.refreshStatus()
    }
  }

  function renewLease() {
    if (leaseProc.running || !leaseWanted) return
    leaseProc.command = [cli, "lease", "--seconds", "30"]
    leaseProc.running = true
  }

  function releaseLease() {
    if (leaseProc.running) { releasePending = true; return }
    leaseProc.command = [cli, "release"]
    leaseProc.running = true
  }

  // A release that arrives while a lease call is still running is replayed
  // afterwards so the later intent wins.
  property bool releasePending: false
  Connections {
    target: leaseProc
    function onRunningChanged() {
      if (!leaseProc.running && root.releasePending) {
        root.releasePending = false
        if (!root.leaseWanted) root.releaseLease()
      }
    }
  }

  Timer {
    interval: 10000
    repeat: true
    running: root.leaseWanted
    triggeredOnStart: true
    onTriggered: root.renewLease()
  }

  // Release only after the need has stayed gone for a moment. The state it
  // reads (a send that just returned, a transfer the next poll will show, a
  // picker handing the panel back) briefly lags the work, and an early release
  // would cancel a transfer that is about to appear.
  Timer {
    id: releaseTimer
    interval: 4000
    onTriggered: if (!root.leaseWanted) { root.leaseError = ""; root.releaseLease() }
  }

  onLeaseWantedChanged: if (leaseWanted) releaseTimer.stop(); else releaseTimer.restart()

  // --------------------------------------------------------------- actions
  //
  // One Process per action so two quick presses (accept two requests, cancel
  // two transfers) do not share state. `key` marks the row as busy until the
  // command returns; `done` runs after the shared bookkeeping.
  Component {
    id: actionComp
    Process {
      id: action
      property string key: ""
      property var done: null
      property string stdinText: ""
      property bool outDone: false
      property bool errDone: false
      property bool exited: false
      property int exitCode: 0

      stdinEnabled: stdinText !== ""
      onStarted: if (stdinText !== "") { write(stdinText); stdinEnabled = false }

      stdout: StdioCollector {
        id: actOut
        waitForEnd: true
        onStreamFinished: { action.outDone = true; action.maybeFinish() }
      }
      stderr: StdioCollector {
        id: actErr
        waitForEnd: true
        onStreamFinished: { action.errDone = true; action.maybeFinish() }
      }
      onExited: function(code) { action.exitCode = code; action.exited = true; action.maybeFinish() }

      function maybeFinish() {
        if (!(outDone && errDone && exited)) return
        root.actionFinished(key, exitCode, actOut.text, actErr.text)
        if (done) done(exitCode, actOut.text, actErr.text)
        action.destroy()
      }
    }
  }

  function setBusy(key, on) {
    var b = Object.assign({}, busy)
    if (on) b[key] = true
    else delete b[key]
    busy = b
  }

  function run(args, key, done, stdinText) {
    actionError = ""
    if (key !== "") setBusy(key, true)
    var proc = actionComp.createObject(root, {
      command: [cli].concat(args), key: key, done: done || null, stdinText: stdinText || ""
    })
    proc.running = true
  }

  function parseJson(text) {
    try { return JSON.parse(text) } catch (e) { return null }
  }

  function failureText(code, out, err) {
    var parsed = parseJson(out)
    if (parsed && parsed.error) return String(parsed.error)
    if (code === 0) return ""
    var line = err.trim().split("\n").pop()
    return line !== "" ? line : "omadrop exited with code " + code + "."
  }

  function actionFinished(key, code, out, err) {
    if (key !== "") setBusy(key, false)
    actionError = failureText(code, out, err)
    refreshStatus()
  }

  Timer {
    interval: 20000
    running: root.actionError !== ""
    onTriggered: root.actionError = ""
  }

  function accept(id) { run(["accept", id], id) }
  function reject(id) { run(["reject", id], id) }
  function cancel(id) { run(["cancel", id], id) }
  function dismiss(id) { run(["dismiss", id], "") }
  function keepVisible() { run(["start", "--seconds", "600"], "window") }
  function stopVisible() {
    // `stop` ends the window and the menu lease with it; take the lease back
    // at once if the panel is still open.
    run(["stop"], "window", function() { root.renewLease() })
  }

  // ---------------------------------------------------------------- staging
  function baseName(path) {
    var i = path.lastIndexOf("/")
    return i >= 0 ? path.substring(i + 1) : path
  }

  // The backend answers an empty clipboard with `error` and no files. That is
  // a normal result of pressing Paste with nothing copied, so it becomes a
  // gentle hint; every other error stays an error.
  function emptyStageHint(parsed) {
    if (!parsed || (parsed.files && parsed.files.length > 0) || !parsed.error) return ""
    switch (String(parsed.error)) {
    case "Clipboard is empty": return "Nothing to paste yet. Copy a file, text or image first."
    case "Nothing to share":   return "Nothing to share yet."
    default:                   return ""
    }
  }

  Timer {
    interval: 6000
    running: root.stageHint !== ""
    onTriggered: root.stageHint = ""
  }

  function addStaged(files) {
    stageHint = ""
    var seen = {}
    var list = staged.slice()
    list.forEach(function(f) { seen[f.path] = true })
    files.forEach(function(f) {
      if (f && f.path && !seen[f.path]) { seen[f.path] = true; list.push(f) }
    })
    staged = list.slice(0, 100)
  }

  function removeStaged(path) {
    staged = staged.filter(function(f) { return f.path !== path })
  }

  function stageDropped(paths) {
    addStaged(paths.map(function(p) { return { path: p, name: baseName(p), size: 0, kind: "file" } }))
  }

  function stageText(text) {
    if (staging !== "") return
    staging = "pasting"
    run(["stage-text"], "", function(code, out, err) {
      root.staging = ""
      var parsed = root.parseJson(out)
      if (parsed && parsed.files) root.addStaged(parsed.files)
      var hint = root.emptyStageHint(parsed)
      root.stageHint = hint
      root.actionError = hint !== "" ? "" : parsed && parsed.error ? String(parsed.error) : root.actionError
    }, text)
  }

  function pasteClipboard() {
    if (staging !== "") return
    staging = "pasting"
    run(["stage-clipboard"], "", function(code, out, err) {
      root.staging = ""
      var parsed = root.parseJson(out)
      if (parsed && parsed.files) root.addStaged(parsed.files)
      // `run` already turned the backend's `error` into an action error.
      var hint = root.emptyStageHint(parsed)
      if (hint !== "") { root.actionError = ""; root.stageHint = hint }
    })
  }

  // The file chooser is a separate window and the popup is an overlay above
  // it, so the panel steps aside while it is open. Its Process lives here, so
  // it outlives the popup, and the panel comes back when the chooser returns,
  // whether or not anything was chosen.
  Process {
    id: pickProc
    command: [root.cli, "pick"]
    stdout: StdioCollector { id: pickOut; waitForEnd: true }
    stderr: StdioCollector { id: pickErr; waitForEnd: true }
    onExited: function(code) {
      var parsed = root.parseJson(pickOut.text)
      if (parsed && parsed.files) root.addStaged(parsed.files)
      root.actionError = root.failureText(code, pickOut.text, pickErr.text)
      root.open()
      root.staging = ""
      root.refreshStatus()
    }
  }

  function chooseFiles() {
    if (staging !== "" || pickProc.running) return
    actionError = ""
    staging = "picking"
    pickProc.running = true
    close()
  }

  // One recipient, one send. The staged paths go to exactly this peer id.
  function sendTo(peerId) {
    if (sendingPeer !== "" || staged.length === 0) return
    sendingPeer = peerId
    var paths = staged.map(function(f) { return f.path })
    run(["send", "--peer", peerId, "--"].concat(paths), "", function(code, out, err) {
      root.sendingPeer = ""
      var parsed = root.parseJson(out)
      if (code === 0 && parsed && parsed.ok) {
        root.staged = []
        view.scrollToTop()
      }
    })
  }

  Process {
    id: dirProc
    stdout: StdioCollector { id: dirOut; waitForEnd: true }
    stderr: StdioCollector { id: dirErr; waitForEnd: true }
    onExited: function(code) {
      var parsed = root.parseJson(dirOut.text)
      // A dismissed picker is not a failure; only a message is worth showing.
      root.actionError = parsed && parsed.cancelled ? "" : root.failureText(code, dirOut.text, dirErr.text)
      root.open()
      root.pickingDirectory = false
      root.refreshStatus()
    }
  }

  function pickDirectory() {
    if (dirProc.running) return
    actionError = ""
    pickingDirectory = true
    dirProc.command = [cli, "directory", "--pick"]
    dirProc.running = true
    close()
  }

  // -------------------------------------------------------------- lifecycle
  onOpenedChanged: if (opened) {
    stageHint = ""
    refreshStatus()
    // A request waiting for a decision is what the person most needs to see.
    if (incomingCount > 0) view.selectTab(1)
    Qt.callLater(function() { view.forceActiveFocus() })
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Shared nearby-sharing mark, drawn without an icon font.
    iconComponent: Component {
      OmaDropMark {
        tint: button.active && button.useActiveColor ? button.activeColor : button.foreground
      }
    }
    active: root.incomingCount > 0
    onPressed: function(buttonCode) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: view
    // Keep a clear gap to the screen edge: the theme can set gaps_out to 0,
    // and a card flush with the edge loses its outer border pixel on a
    // fractionally scaled output.
    margin: Math.max(Style.gapsOut, Style.space(8))
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(view.implicitHeight, root.maxContentHeight)

    OmaDropView {
      id: view
      anchors.fill: parent
      status: root.status
      backendError: root.backendError
      actionError: root.actionError
      leaseError: root.leaseError
      homePath: Quickshell.env("HOME") || ""
      foreground: root.foreground
      fontFamily: root.fontFamily
      busy: root.busy
      staged: root.staged
      staging: root.staging
      sendingPeer: root.sendingPeer
      stageHint: root.stageHint
      pickingDirectory: root.pickingDirectory
      leasing: root.leaseWanted

      onChooseFilesRequested: root.chooseFiles()
      onPasteRequested: root.pasteClipboard()
      onPathsDropped: function(paths) { root.stageDropped(paths) }
      onTextDropped: function(text) { root.stageText(text) }
      onRemoveStagedRequested: function(path) { root.removeStaged(path) }
      onClearStagedRequested: root.staged = []
      onSendRequested: function(id) { root.sendTo(id) }
      onAcceptRequested: function(id) { root.accept(id) }
      onRejectRequested: function(id) { root.reject(id) }
      onCancelRequested: function(id) { root.cancel(id) }
      onDismissRequested: function(id) { root.dismiss(id) }
      onKeepVisibleRequested: root.keepVisible()
      onStopVisibleRequested: root.stopVisible()
      onPickDirectoryRequested: root.pickDirectory()
      onRetryRequested: { root.actionError = ""; root.refreshStatus() }
      onCloseRequested: root.close()
    }
  }
}
