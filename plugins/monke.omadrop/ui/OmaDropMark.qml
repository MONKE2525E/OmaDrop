import QtQuick

// Two facing corners surround one shared item. One silhouette at every size.
Item {
  id: mark
  property color tint: "#000000"

  readonly property real dpr: Screen.devicePixelRatio || 1
  readonly property real unit: Math.min(width, height) / 16
  readonly property real stroke: Math.max(1, Math.round(2 * unit * dpr)) / dpr

  function snap(value) { return Math.round(value * dpr) / dpr }
  function coord(value, dimension) {
    return snap((dimension - 16 * unit) / 2 + value * unit)
  }

  Rectangle {
    x: mark.coord(2, mark.width); y: mark.coord(2, mark.height)
    width: mark.snap(8 * mark.unit); height: mark.stroke
    color: mark.tint
  }
  Rectangle {
    x: mark.coord(2, mark.width); y: mark.coord(2, mark.height)
    width: mark.stroke; height: mark.snap(8 * mark.unit)
    color: mark.tint
  }
  Rectangle {
    x: mark.coord(14, mark.width) - mark.stroke; y: mark.coord(6, mark.height)
    width: mark.stroke; height: mark.coord(14, mark.height) - y
    color: mark.tint
  }
  Rectangle {
    x: mark.coord(6, mark.width); y: mark.coord(14, mark.height) - mark.stroke
    width: mark.coord(14, mark.width) - x; height: mark.stroke
    color: mark.tint
  }
  Rectangle {
    x: mark.coord(6, mark.width); y: mark.coord(6, mark.height)
    width: mark.coord(10, mark.width) - x
    height: mark.coord(10, mark.height) - y
    color: mark.tint
  }
}
