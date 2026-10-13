import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// The OmaDrop panel body: two tabs, Send and Receive. It owns layout and tab
// state only. Every fact comes in through properties and every action leaves
// as a signal, so ui/OmaDropPanel.qml owns the backend and a test harness can
// drive this file without one.
//
// `status` is the parsed `omadrop status` object, or null before the first
// answer. `backendError` is set when that command could not be run or parsed.
Item {
  id: root

  property var status: null
  property string backendError: ""
  property string actionError: ""
  property string leaseError: ""
  property string homePath: ""
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  // Staged content: [{path, name, size, kind}], chosen before a recipient.
  property var staged: []
  // "" | "picking" | "pasting": what is currently filling `staged`.
  property string staging: ""
  // Peer id with a send command in flight, so a second press cannot send twice.
  property string sendingPeer: ""
  // Ids with an action in flight, so a button cannot be pressed twice.
  property var busy: ({})
  property bool pickingDirectory: false
  // A quiet, expected outcome of staging (an empty clipboard), not a failure.
  property string stageHint: ""
  // Startup, from the panel: the backend is bringing the radios up, or it has
  // failed (the watchdog passed its bound, or the start command gave an error).
  property bool startupWaiting: false
  property bool startupFailed: false
  // The start command is still running, so Retry would only queue behind it.
  property bool leaseBusy: false
  property int startupSeconds: 65
  // Share list: the first five files show unless the person asks for all.
  property bool stageExpanded: false

  property int tab: 0

  signal chooseFilesRequested()
  signal pasteRequested()
  signal pathsDropped(var paths)
  signal textDropped(string text)
  signal removeStagedRequested(string path)
  signal clearStagedRequested()
  signal sendRequested(string peerId)
  signal acceptRequested(string id)
  signal rejectRequested(string id)
  signal cancelRequested(string id)
  signal dismissRequested(string id)
  signal keepVisibleRequested()
  signal stopVisibleRequested()
  signal pickDirectoryRequested()
  signal retryRequested()
  signal retryStartupRequested()
  signal closeRequested()

  // Called by the panel once the backend confirms a Clear, so the row leaves
  // only when it is really gone and never reappears on the next poll.
  function markCleared(id) {
    var c = Object.assign({}, cleared)
    c[id] = true
    cleared = c
  }

  onStagedChanged: if (staged.length <= 5) stageExpanded = false

  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color rule: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.35)
  readonly property bool ready: status !== null && backendError === ""

  readonly property var protocols: ready && status.protocols ? status.protocols : []
  readonly property var incoming: ready && status.incoming ? status.incoming : []
  readonly property var transfers: ready && status.transfers ? status.transfers : []
  readonly property string directory: ready && status.directory ? status.directory : ""
  readonly property string mode: ready && status.mode ? String(status.mode) : "off"
  readonly property bool windowActive: mode === "window"

  // Named devices from every protocol in one list, by name. The backend drops
  // unnamed Apple devices; an empty name is skipped here as well.
  readonly property var peers: {
    if (!ready || !status.peers) return []
    var list = status.peers.filter(function(p) { return p.name && String(p.name).trim() !== "" })
    list.sort(function(a, b) {
      var an = String(a.name).toLowerCase(), bn = String(b.name).toLowerCase()
      return an < bn ? -1 : an > bn ? 1 : 0
    })
    return list
  }
  readonly property var unavailable: protocols.filter(function(p) { return p.state === "unavailable" })
  readonly property int availableCount: protocols.filter(function(p) { return p.state !== "unavailable" }).length

  // Finished transfers the backend has confirmed forgetting (see markCleared).
  property var cleared: ({})

  readonly property var outgoingLive: liveFor("out")
  readonly property var outgoingDone: finishedFor("out")
  readonly property var incomingLive: liveFor("in")
  readonly property var incomingDone: finishedFor("in")
  readonly property var outgoingList: outgoingLive.concat(outgoingDone)
  readonly property var incomingList: incomingLive.concat(incomingDone)

  readonly property real stagedBytes: staged.reduce(function(sum, f) { return sum + (Number(f.size) > 0 ? Number(f.size) : 0) }, 0)

  // Seconds left in an explicit window, counted down locally between polls.
  property real deadlineMs: 0
  property real nowMs: Date.now()
  readonly property bool hasDeadline: incoming.some(function(i) { return expiryAt(i) > 0 })
  readonly property int remaining: windowActive ? Math.max(0, Math.round((deadlineMs - nowMs) / 1000)) : 0

  onStatusChanged: {
    nowMs = Date.now()
    var s = ready && status.remainingSeconds !== undefined && status.remainingSeconds !== null
      ? Number(status.remainingSeconds) : 0
    deadlineMs = nowMs + (s > 0 ? s * 1000 : 0)
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.windowActive || root.hasDeadline
    onTriggered: root.nowMs = Date.now()
  }

  // Startup is checked first: the mode can read "window" or "menu" while the
  // protocols are still starting, and nothing is visible until they are up.
  readonly property string subtitle: {
    if (!ready) return status === null && backendError === "" ? "Checking" : "Unavailable"
    if (startupWaiting) return "Starting"
    if (startupFailed) return "Not started"
    if (windowActive) return "Visible · " + clock(remaining) + " left"
    if (mode === "menu") return "Visible while open"
    return "Not visible"
  }

  // Why startup failed, in the start command's own words when it gave one.
  readonly property string startupMessage: leaseError !== "" ? leaseError
    : leaseBusy ? "The start command has not answered after " + startupSeconds + " seconds."
    : "Nearby sharing is still off after " + startupSeconds + " seconds."

  // How far the scrolling viewport extends past its content; see `flick`.
  readonly property int clipGutter: Style.space(4)

  implicitWidth: Style.space(400)
  implicitHeight: column.implicitHeight

  // ---------------------------------------------------------------- helpers
  function isLive(t) {
    return t.state === "preparing" || t.state === "waiting" || t.state === "transferring"
  }

  // Newest first. The four latest finished transfers stay listed as Recent.
  function liveFor(direction) {
    return transfers.filter(function(t) { return t.direction === direction && isLive(t) }).reverse()
  }

  function finishedFor(direction) {
    return transfers.filter(function(t) {
      return t.direction === direction && !isLive(t) && cleared[t.id] !== true
    }).reverse().slice(0, 4)
  }

  function protocolName(id) {
    for (var i = 0; i < protocols.length; i++)
      if (protocols[i].id === id) return protocols[i].name
    return id
  }

  function clock(total) {
    var m = Math.floor(total / 60), s = total % 60
    return m + ":" + (s < 10 ? "0" : "") + s
  }

  function formatSize(n) {
    n = Number(n)
    if (!(n >= 0)) return ""
    var units = ["B", "KB", "MB", "GB", "TB"]
    var i = 0
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++ }
    var s = (i === 0 || n >= 100) ? String(Math.round(n)) : n.toFixed(n >= 10 ? 0 : 1)
    return s + " " + units[i]
  }

  function plural(n, one, many) { return n + " " + (n === 1 ? one : many) }

  function tildify(p) {
    if (homePath !== "" && p.indexOf(homePath) === 0) return "~" + p.substring(homePath.length)
    return p
  }

  // `kind` is "file", "text" or "url"; older backends send none. A text or link
  // request carries a description until its payload arrives.
  function incomingTitle(item) {
    if ((item.kind === "text" || item.kind === "url") && item.description) return String(item.description)
    var files = item.files || []
    if (files.length === 0) return item.name || "Files"
    return files.length === 1 ? files[0].name : files[0].name + " +" + (files.length - 1) + " more"
  }

  function incomingFrom(item) {
    var what = item.kind === "url" ? "Link " : item.kind === "text" ? "Text " : ""
    return what + "from " + item.name + " via " + protocolName(item.protocol)
  }

  // `expiresAt` is epoch seconds. Absent or empty means the backend set no
  // deadline, so the request stays usable (-1). A number is a deadline, and an
  // explicit 0 or anything that is not a number counts as already lapsed (0).
  function expiryAt(item) {
    if (item.expiresAt === undefined || item.expiresAt === null || item.expiresAt === "") return -1
    var at = Number(item.expiresAt)
    return isFinite(at) && at >= 0 ? at : 0
  }

  // A deadline at or before now has lapsed, and so has an explicit 0.
  function incomingExpired(item) {
    var at = expiryAt(item)
    return at >= 0 && at <= nowMs / 1000
  }

  // Whole seconds until an incoming request lapses; -1 when it has no deadline.
  function incomingSecondsLeft(item) {
    var at = expiryAt(item)
    return at < 0 ? -1 : Math.max(0, Math.ceil(at - nowMs / 1000))
  }

  function transferTitle(t) {
    var n = Number(t.files)
    var first = t.fileName ? String(t.fileName) : String(t.name)
    return n > 1 ? first + " +" + (n - 1) + " more" : first
  }

  function transferFraction(t) {
    if (t.state === "complete") return 1
    var total = Number(t.total)
    return total > 0 ? Math.max(0, Math.min(1, Number(t.bytes) / total)) : 0
  }

  // One plain sentence about where a transfer stands.
  function transferStatus(t) {
    var out = t.direction === "out"
    switch (t.state) {
    case "preparing":    return "Preparing"
    case "waiting":      return out ? "Waiting for approval" : "Waiting for sender"
    case "transferring": return Math.round(transferFraction(t) * 100) + "%"
    case "complete":     return out ? "Sent" : "Saved"
    case "rejected":     return out ? "Declined" : "Rejected"
    case "failed":       return "Failed"
    case "cancelled":    return "Cancelled"
    default:             return String(t.state)
    }
  }

  function isBusy(id) { return busy[id] === true }

  // Keeps a control that takes keyboard focus inside the scrolling body, so Tab
  // never lands on a control the person cannot see. Only body controls count.
  function revealItem(item) {
    var inBody = false
    for (var it = item; it; it = it.parent) if (it === body) { inBody = true; break }
    if (!inBody) return
    var top = item.mapToItem(flick.contentItem, 0, 0).y - root.clipGutter
    var bottom = top + item.height + 2 * root.clipGutter
    var y = flick.contentY
    if (top < y) y = top
    else if (bottom > y + flick.height) y = bottom - flick.height
    flick.contentY = Math.max(0, Math.min(Math.max(0, flick.contentHeight - flick.height), y))
  }

  function scrollBy(dy) {
    var max = Math.max(0, flick.contentHeight - flick.height)
    flick.contentY = Math.max(0, Math.min(max, flick.contentY + dy))
  }

  function scrollToTop() { flick.contentY = 0 }

  function selectTab(i) {
    tab = Math.max(0, Math.min(1, i))
    flick.contentY = 0
  }

  // Device and file names come from the network. Everything below is
  // PlainText so a name can never inject markup.
  component Label: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    elide: Text.ElideRight
  }

  component Meta: Label {
    color: root.dim
    font.pixelSize: Style.font.caption
  }

  component Btn: Button {
    id: control
    bordered: true
    focusable: true
    foreground: root.foreground
    fontFamily: root.fontFamily
    fontSize: Style.font.bodySmall
    horizontalPadding: Style.space(10)
    verticalPadding: Style.space(5)
    Accessible.role: Accessible.Button
    Accessible.name: text
    opacity: enabled ? 1.0 : 0.45
    onActiveFocusChanged: if (activeFocus) root.revealItem(control)

    // The kit's focus ring is a faint tint; a full-strength outline keeps the
    // keyboard target obvious on a black panel.
    Rectangle {
      anchors.fill: parent
      z: 2
      visible: parent.activeFocus
      color: "transparent"
      radius: Style.cornerRadius
      border.width: 1
      border.color: root.foreground
    }
  }

  component SmallBtn: Btn {
    horizontalPadding: Style.space(8)
    verticalPadding: Style.space(3)
  }

  component SectionRow: RowLayout {
    property string title
    property string meta
    Layout.fillWidth: true
    PanelSectionHeader {
      text: title.toUpperCase()
      foreground: root.foreground
      fontFamily: root.fontFamily
      font.letterSpacing: 1.2
      Layout.fillWidth: true
    }
    Meta { text: meta; horizontalAlignment: Text.AlignRight }
  }

  component Note: Meta {
    Layout.fillWidth: true
    wrapMode: Text.WordWrap
    elide: Text.ElideNone
  }

  component Notice: Rectangle {
    id: notice
    property string text
    property string label: "Error"
    property bool closable: false
    signal closed()
    Layout.fillWidth: true
    implicitHeight: noticeText.implicitHeight + Style.space(16)
    color: "transparent"
    border.width: 1
    border.color: root.foreground
    radius: Style.cornerRadius
    Accessible.role: Accessible.AlertMessage
    Accessible.name: label + ": " + text
    Label {
      id: noticeText
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: notice.closable ? Style.space(28) : Style.space(8)
      text: notice.label + ": " + notice.text
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.Wrap
      elide: Text.ElideNone
    }
    SmallBtn {
      visible: notice.closable
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(3)
      text: "×"
      horizontalPadding: Style.space(6)
      verticalPadding: Style.space(1)
      Accessible.name: "Dismiss"
      onClicked: notice.closed()
    }
  }

  // One transfer: title and status on one line, a square progress bar while it
  // moves, and the failure reason in the backend's own words.
  component TransferRow: ColumnLayout {
    id: xfer
    property var t
    readonly property bool live: root.isLive(t)
    readonly property bool bad: t.state === "failed" || t.state === "rejected" || t.state === "cancelled"
    Layout.fillWidth: true
    spacing: Style.space(3)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)
      Label {
        Layout.fillWidth: true
        text: (xfer.t.direction === "in" ? "↓ " : "↑ ") + root.transferTitle(xfer.t)
        color: xfer.bad ? root.dim : root.foreground
      }
      SmallBtn {
        visible: xfer.live
        enabled: !root.isBusy(xfer.t.id)
        text: "Cancel"
        Accessible.name: "Cancel transfer " + root.transferTitle(xfer.t)
        onClicked: root.cancelRequested(xfer.t.id)
      }
      SmallBtn {
        visible: !xfer.live
        enabled: !root.isBusy(xfer.t.id)
        text: "Clear"
        Accessible.name: "Clear transfer " + root.transferTitle(xfer.t)
        onClicked: root.dismissRequested(xfer.t.id)
      }
    }
    Rectangle {
      visible: xfer.t.state === "transferring" || xfer.t.state === "complete"
      Layout.fillWidth: true
      implicitHeight: Style.space(4)
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)
      Accessible.role: Accessible.ProgressBar
      Accessible.name: root.transferTitle(xfer.t) + " " + root.transferStatus(xfer.t)
      Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: parent.width * root.transferFraction(xfer.t)
        color: root.foreground
      }
    }
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)
      Meta {
        Layout.fillWidth: true
        text: root.transferStatus(xfer.t) + " · " + (xfer.t.direction === "in" ? "from " : "to ")
              + xfer.t.peer + " · " + root.protocolName(xfer.t.protocol)
        wrapMode: Text.Wrap
        elide: Text.ElideNone
        maximumLineCount: 2
      }
      Meta {
        visible: xfer.t.state === "transferring" && Number(xfer.t.total) > 0
        text: root.formatSize(xfer.t.bytes) + " / " + root.formatSize(xfer.t.total)
      }
    }
    Note {
      visible: !!xfer.t.error && xfer.bad && xfer.t.state !== "rejected"
      text: String(xfer.t.error)
    }
  }

  // Dropping files anywhere on the panel stages them. File URLs become plain
  // paths; a dropped text selection is staged by the backend as a text file.
  DropArea {
    id: drop
    anchors.fill: parent
    onEntered: function(drag) { drag.accepted = drag.hasUrls || drag.hasText }
    onDropped: function(event) {
      if (event.hasUrls) {
        var paths = []
        for (var i = 0; i < event.urls.length; i++) {
          var u = String(event.urls[i])
          if (u.indexOf("file://") === 0) paths.push(decodeURIComponent(u.substring(7)))
        }
        if (paths.length > 0) root.pathsDropped(paths)
      } else if (event.hasText && event.text !== "") {
        root.textDropped(event.text)
      }
      event.accepted = true
      root.selectTab(0)
    }
  }

  ColumnLayout {
    id: column
    anchors.fill: parent
    spacing: Style.space(10)

    // ------------------------------------------------------------ header
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(14)

      // The same simple mark is used in the header and the bar.
      OmaDropMark {
        Layout.preferredWidth: Style.space(30)
        Layout.preferredHeight: Style.space(30)
        tint: root.foreground
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(2)
        Label {
          Layout.fillWidth: true
          text: "OmaDrop"
          font.pixelSize: Style.font.title
          font.bold: true
        }
        Meta {
          Layout.fillWidth: true
          text: root.subtitle.toUpperCase()
          font.bold: true
          font.letterSpacing: 1.2
          Accessible.name: "Visibility: " + root.subtitle
        }
      }
    }

    // -------------------------------------------------------------- tabs
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)
      visible: root.ready

      Repeater {
        model: [
          "Send",
          root.incoming.length > 0 ? "Receive (" + root.incoming.length + ")" : "Receive"
        ]
        Btn {
          required property int index
          required property string modelData
          Layout.fillWidth: true
          Layout.preferredWidth: 1
          implicitHeight: Style.space(34)
          text: modelData
          selected: root.tab === index
          fontSize: Style.font.body
          Accessible.role: Accessible.PageTab
          Accessible.name: text
          Accessible.selected: selected
          onClicked: root.selectTab(index)
        }
      }
    }

    // --------------------------------------------- notices and backend state
    ColumnLayout {
      visible: !root.ready
      Layout.fillWidth: true
      spacing: Style.space(8)

      Note {
        visible: root.status === null && root.backendError === ""
        text: "Waiting for the OmaDrop service…"
      }
      Notice {
        visible: root.backendError !== ""
        text: root.backendError
      }
      Btn {
        visible: root.backendError !== ""
        text: "Retry"
        onClicked: root.retryRequested()
      }
    }

    Notice {
      visible: root.ready && root.status.error !== undefined && root.status.error !== ""
      text: root.ready && root.status.error ? root.status.error : ""
    }
    Note {
      visible: root.ready && root.startupWaiting && !root.startupFailed
      text: "Starting nearby sharing. This can take up to a minute."
    }
    Notice {
      visible: root.ready && root.startupFailed
      label: "Not started"
      text: root.startupMessage
    }
    Btn {
      visible: root.ready && root.startupFailed && !root.leaseBusy
      Layout.fillWidth: true
      text: "Retry"
      Accessible.name: "Retry starting nearby sharing"
      onClicked: root.retryStartupRequested()
    }
    Notice {
      visible: root.actionError !== ""
      text: root.actionError
      closable: true
      onClosed: root.retryRequested()
    }

    PanelSeparator { visible: root.ready; Layout.fillWidth: true; foreground: root.foreground }

    // -------------------------------------------------------------- body
    Flickable {
      id: flick
      visible: root.ready
      Layout.fillWidth: true
      Layout.fillHeight: true
      Layout.preferredHeight: body.implicitHeight + 2 * root.clipGutter
      Layout.minimumHeight: Math.min(body.implicitHeight, Style.space(110)) + 2 * root.clipGutter
      // A Flickable clips to its own rectangle, and on a fractionally scaled
      // output that rectangle rounds in or out depending on where the popup
      // lands. Content flush with it loses the 1px border of a button on one
      // side. So the viewport reaches `clipGutter` past the content on every
      // side and the negative margins hand that space back to the layout; the
      // body sits inside it and no border touches the clip edge.
      Layout.leftMargin: -root.clipGutter
      Layout.topMargin: -root.clipGutter
      Layout.bottomMargin: -root.clipGutter
      // The scroll track lives in the card's padding, so content keeps equal
      // left and right margins whether or not it scrolls.
      Layout.rightMargin: -Style.space(8)
      contentWidth: width
      contentHeight: body.implicitHeight + 2 * root.clipGutter
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height
      // A thin square track keeps "there is more below" visible without the
      // theme's rounded overlay scrollbar.
      ScrollBar.vertical: ScrollBar {
        id: scrollbar
        policy: flick.interactive ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        implicitWidth: Style.space(3)
        padding: 0
        background: Rectangle { color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12) }
        contentItem: Rectangle {
          implicitWidth: Style.space(3)
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, scrollbar.pressed ? 0.9 : 0.55)
        }
      }

      Item {
        id: body
        x: root.clipGutter
        y: root.clipGutter
        width: flick.width - root.clipGutter - Style.space(8)
        implicitHeight: root.tab === 0 ? sendTab.implicitHeight : receiveTab.implicitHeight

        // ============================================================ SEND
        ColumnLayout {
          id: sendTab
          visible: root.tab === 0
          width: parent.width
          spacing: Style.space(8)

          // ---- transfers first, so progress is the first thing seen
          // "Sending" appears only while an outbound transfer is live; finished
          // ones are listed under "Recent", which appears alone when nothing is live.
          ColumnLayout {
            visible: root.outgoingList.length > 0
            Layout.fillWidth: true
            spacing: Style.space(8)
            ColumnLayout {
              visible: root.outgoingLive.length > 0
              Layout.fillWidth: true
              spacing: Style.space(8)
              SectionRow {
                title: "Sending"
                meta: root.outgoingLive.length + " active"
              }
              Repeater {
                model: root.outgoingLive
                TransferRow {
                  required property var modelData
                  t: modelData
                }
              }
            }
            ColumnLayout {
              visible: root.outgoingDone.length > 0
              Layout.fillWidth: true
              spacing: Style.space(8)
              SectionRow { title: "Recent"; meta: "" }
              Repeater {
                model: root.outgoingDone
                TransferRow {
                  required property var modelData
                  t: modelData
                }
              }
            }
            PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }
          }

          // ---- staged content
          SectionRow {
            title: "Share"
            meta: root.staged.length === 0 ? ""
                  : root.plural(root.staged.length, "item", "items")
                    + (root.stagedBytes > 0 ? " · " + root.formatSize(root.stagedBytes) : "")
          }

          Rectangle {
            Layout.fillWidth: true
            implicitHeight: stage.implicitHeight + Style.space(20)
            color: drop.containsDrag ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1) : "transparent"
            border.width: 1
            border.color: drop.containsDrag ? root.foreground : root.rule
            radius: Style.cornerRadius

            ColumnLayout {
              id: stage
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.margins: Style.space(10)
              spacing: Style.space(6)

              Label {
                visible: root.staged.length === 0
                Layout.fillWidth: true
                text: drop.containsDrag ? "Release to add" : "Drop files here"
                horizontalAlignment: Text.AlignHCenter
              }
              Meta {
                visible: root.staged.length === 0 && root.stageHint === ""
                Layout.fillWidth: true
                text: root.staging === "picking" ? "The file picker is open in another window."
                      : "Or choose files, or paste with Ctrl+V."
                horizontalAlignment: Text.AlignHCenter
              }

              Repeater {
                model: root.stageExpanded ? root.staged : root.staged.slice(0, 5)
                RowLayout {
                  required property var modelData
                  Layout.fillWidth: true
                  spacing: Style.space(8)
                  Label {
                    Layout.fillWidth: true
                    text: modelData.name
                    elide: Text.ElideMiddle
                  }
                  Meta { text: Number(modelData.size) > 0 ? root.formatSize(modelData.size) : modelData.kind === "image" ? "image" : "" }
                  SmallBtn {
                    text: "×"
                    horizontalPadding: Style.space(6)
                    verticalPadding: Style.space(1)
                    Accessible.name: "Remove " + modelData.name
                    onClicked: root.removeStagedRequested(modelData.path)
                  }
                }
              }
              SmallBtn {
                visible: root.staged.length > 5
                Layout.alignment: Qt.AlignHCenter
                text: root.stageExpanded ? "Show fewer" : "Show all " + root.staged.length
                Accessible.name: root.stageExpanded ? "Show fewer files to share"
                  : "Show all " + root.staged.length + " files to share"
                onClicked: root.stageExpanded = !root.stageExpanded
              }

              Meta {
                visible: root.stageHint !== ""
                Layout.fillWidth: true
                text: root.stageHint
                wrapMode: Text.Wrap
                elide: Text.ElideNone
                horizontalAlignment: Text.AlignHCenter
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(8)
                Btn {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  enabled: root.staging === ""
                  text: root.staging === "picking" ? "Choosing…" : "Choose files"
                  onClicked: root.chooseFilesRequested()
                }
                Btn {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  enabled: root.staging === ""
                  text: root.staging === "pasting" ? "Reading…" : "Paste"
                  Accessible.name: "Paste from clipboard"
                  onClicked: root.pasteRequested()
                }
                Btn {
                  visible: root.staged.length > 0
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  text: "Clear"
                  onClicked: root.clearStagedRequested()
                }
              }
            }
          }

          // ---- one recipient
          SectionRow {
            Layout.topMargin: Style.space(4)
            title: "Send to"
            meta: root.peers.length > 0 ? root.peers.length + " nearby" : ""
          }
          Note {
            visible: root.peers.length > 0 && root.staged.length === 0
            text: "Add something to share, then choose one device."
          }
          Note {
            visible: root.peers.length === 0 && root.availableCount > 0
            text: root.startupWaiting ? "Starting discovery…"
                  : root.startupFailed ? "Nearby sharing did not start, so no devices can be seen."
                  : "No named devices found yet. Keep the other device's sharing screen open."
          }
          Note {
            visible: root.peers.length === 0 && root.availableCount === 0
            text: "Nothing can be found: every protocol is unavailable."
          }

          Repeater {
            model: root.unavailable
            Note {
              required property var modelData
              text: modelData.name + " unavailable: " + (modelData.reason || "no reason given")
            }
          }

          Repeater {
            model: root.peers
            RowLayout {
              required property var modelData
              readonly property bool sendingThis: root.sendingPeer === modelData.id
              Layout.fillWidth: true
              Layout.minimumHeight: Style.space(30)
              spacing: Style.space(8)

              Label {
                Layout.fillWidth: true
                text: modelData.name
              }
              Meta {
                Layout.maximumWidth: Style.space(140)
                text: root.protocolName(modelData.protocol)
                      + (modelData.deviceType ? " · " + modelData.deviceType : "")
              }
              SmallBtn {
                enabled: root.staged.length > 0 && root.sendingPeer === ""
                text: sendingThis ? "Sending…" : "Send"
                Accessible.name: "Send " + root.plural(root.staged.length, "item", "items") + " to " + modelData.name
                onClicked: root.sendRequested(modelData.id)
              }
            }
          }

        }

        // ========================================================== RECEIVE
        ColumnLayout {
          id: receiveTab
          visible: root.tab === 1
          width: parent.width
          spacing: Style.space(8)

          // ---- incoming requests: nothing is saved before an explicit Accept
          SectionRow {
            title: "Incoming"
            meta: root.incoming.length > 0 ? root.incoming.length + " waiting" : ""
          }
          Note {
            visible: root.incoming.length === 0
            text: "No requests. Nothing saves without your approval."
          }
          Repeater {
            model: root.incoming
            ColumnLayout {
              id: req
              required property var modelData
              required property int index
              readonly property bool working: root.isBusy(modelData.id)
              readonly property bool expired: root.incomingExpired(modelData)
              Layout.fillWidth: true
              spacing: Style.space(4)

              PanelSeparator {
                visible: req.index > 0
                Layout.fillWidth: true
                Layout.topMargin: Style.space(2)
                Layout.bottomMargin: Style.space(2)
                foreground: root.foreground
              }
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(10)
                Label {
                  Layout.fillWidth: true
                  text: root.incomingTitle(req.modelData)
                }
                Meta { text: root.formatSize(req.modelData.totalSize) }
              }
              Meta {
                Layout.fillWidth: true
                text: root.incomingFrom(req.modelData)
                wrapMode: Text.Wrap
                elide: Text.ElideNone
                maximumLineCount: 2
              }
              Meta {
                readonly property int secsLeft: root.incomingSecondsLeft(req.modelData)
                visible: secsLeft >= 0
                Layout.fillWidth: true
                text: req.expired ? "Expired" : "Expires in " + root.clock(secsLeft)
              }
              Repeater {
                model: (req.modelData.files || []).slice(1, 4)
                RowLayout {
                  required property var modelData
                  Layout.fillWidth: true
                  spacing: Style.space(10)
                  Meta { Layout.fillWidth: true; text: modelData.name }
                  Meta { text: root.formatSize(modelData.size) }
                }
              }
              Meta {
                visible: (req.modelData.files || []).length > 4
                Layout.fillWidth: true
                text: "+" + ((req.modelData.files || []).length - 4) + " more files"
              }
              Label {
                visible: req.modelData.pin !== null && req.modelData.pin !== undefined && req.modelData.pin !== ""
                Layout.fillWidth: true
                text: "PIN " + req.modelData.pin
                font.bold: true
                font.letterSpacing: 2
                Accessible.name: "PIN " + req.modelData.pin
              }
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(8)
                // Accept needs a live request. Reject stays enabled on a lapsed
                // one: it only clears the row and asks the backend to drop it.
                Btn {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  enabled: !req.working && !req.expired
                  selected: true
                  text: "Accept"
                  Accessible.name: "Accept " + root.incomingTitle(req.modelData)
                  onClicked: root.acceptRequested(req.modelData.id)
                }
                Btn {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  enabled: !req.working
                  text: "Reject"
                  Accessible.name: "Reject " + root.incomingTitle(req.modelData)
                  onClicked: root.rejectRequested(req.modelData.id)
                }
              }
            }
          }

          // ---- receiving progress
          // Same rule as Send: "Receiving" only while an inbound transfer is live.
          ColumnLayout {
            visible: root.incomingList.length > 0
            Layout.fillWidth: true
            spacing: Style.space(8)
            PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }
            ColumnLayout {
              visible: root.incomingLive.length > 0
              Layout.fillWidth: true
              spacing: Style.space(8)
              SectionRow {
                title: "Receiving"
                meta: root.incomingLive.length + " active"
              }
              Repeater {
                model: root.incomingLive
                TransferRow {
                  required property var modelData
                  t: modelData
                }
              }
            }
            ColumnLayout {
              visible: root.incomingDone.length > 0
              Layout.fillWidth: true
              spacing: Style.space(8)
              SectionRow { title: "Recent"; meta: "" }
              Repeater {
                model: root.incomingDone
                TransferRow {
                  required property var modelData
                  t: modelData
                }
              }
            }
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          // ---- visibility. All three protocols are shown together, so a
          // healthy one needs no row; only one that cannot show is named.
          SectionRow { title: "Visibility" }
          Note {
            text: root.startupWaiting
                  ? "Starting nearby sharing. Other devices cannot see this one yet."
                  : root.startupFailed
                    ? "Not visible: nearby sharing did not start."
                    : root.windowActive
                      ? "Visible to nearby devices until the timer ends, even with this panel closed."
                      : root.mode === "menu"
                        ? "Visible to nearby devices while this panel is open."
                        : "Not visible yet."
          }
          Repeater {
            model: root.unavailable
            Notice {
              required property var modelData
              label: "Can't show on " + modelData.name
              text: modelData.reason || "no reason given"
            }
          }
          Btn {
            Layout.fillWidth: true
            enabled: !root.isBusy("window")
            text: root.windowActive ? "Stop early" : "Stay visible for 10 minutes"
            onClicked: root.windowActive ? root.stopVisibleRequested() : root.keepVisibleRequested()
          }
        }
      }
    }

    // ------------------------------------------------------------ footer
    ColumnLayout {
      visible: root.ready && root.tab === 1
      Layout.fillWidth: true
      spacing: Style.space(10)

      PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(10)

        Meta {
          text: "SAVE TO"
          font.bold: true
          font.letterSpacing: 1.2
        }
        Label {
          Layout.fillWidth: true
          text: root.directory !== "" ? root.tildify(root.directory) : "Not set"
          elide: Text.ElideMiddle
          font.pixelSize: Style.font.bodySmall
          Accessible.name: "Save folder " + text
        }
        SmallBtn {
          enabled: !root.pickingDirectory
          text: root.pickingDirectory ? "Choosing…" : "Change"
          Accessible.name: "Change save folder"
          onClicked: root.pickDirectoryRequested()
        }
      }
    }
  }

  // Keys the focused buttons do not claim: tabs, paste, scrolling, close.
  // Buttons handle Return and Space themselves, so Tab/Shift-Tab reach every
  // control.
  focus: true
  Keys.onPressed: function(event) {
    if (event.key === Qt.Key_Escape) { root.closeRequested(); event.accepted = true }
    else if (event.matches(StandardKey.Paste)) { root.selectTab(0); root.pasteRequested(); event.accepted = true }
    else if (event.key === Qt.Key_Left) { root.selectTab(root.tab - 1); event.accepted = true }
    else if (event.key === Qt.Key_Right) { root.selectTab(root.tab + 1); event.accepted = true }
    else if (event.key === Qt.Key_Down) { root.scrollBy(Style.space(56)); event.accepted = true }
    else if (event.key === Qt.Key_Up) { root.scrollBy(-Style.space(56)); event.accepted = true }
    else if (event.key === Qt.Key_1 || event.key === Qt.Key_2) { root.selectTab(event.key - Qt.Key_1); event.accepted = true }
  }
}
