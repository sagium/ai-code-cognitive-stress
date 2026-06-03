/*
 * Cognitive Stress — KDE Plasma 6 widget.
 *
 * Renders TODAY's full daily view — the same data and format as the HTML day
 * drill-down and the macOS widget — produced by `aicogstress --emit-json` (schema
 * ai-code-cognitive-stress.dayview.v1). Shows the composite, work window, the
 * per-hour concurrency chart, and the three axis tiles with zone range bars
 * (baseline / optimum / you markers + boundary ticks) and collapsible
 * methodology. Colours and 0..1 fractions come straight from the JSON
 * (dayview.py / scales.py), so the three surfaces can't drift.
 *
 * Native look: Kirigami.Theme drives backgrounds/text/typography; the data-viz
 * (chart, range bars) is drawn on Canvas with the shared zone colours. Works as
 * an inline DESKTOP widget (full representation) and a panel applet (compact).
 *
 * Local-only: shells out to a local CLI and parses its stdout. No network.
 * Plasma 6 / Qt 6 only (versionless imports; data engine moved to
 * org.kde.plasma.plasma5support — a P6 naming quirk, not a P5 dependency).
 */
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property var dv: null
    property string errorText: ""
    readonly property bool ready: dv !== null && errorText === ""

    // Project semantic colours (match the HTML report) for the data-viz; text
    // and chrome use Kirigami.Theme so the widget sits natively in any theme.
    readonly property color barColor: "#d99058"
    readonly property color shadeColor: "#6c9a8b"

    // This is a desktop widget: show the full daily view inline. In a panel
    // (Horizontal/Vertical form factor) fall back to the compact composite
    // score, which expands to the full view on click.
    preferredRepresentation: (Plasmoid.formFactor === PlasmaCore.Types.Horizontal
                              || Plasmoid.formFactor === PlasmaCore.Types.Vertical)
        ? compactRepresentation : fullRepresentation

    // --- data: run the local CLI on a timer, parse its JSON stdout ----------
    Plasma5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: function (source, data) {
            executable.disconnectSource(source) // one-shot per run
            root.handleResult(data)
        }
        function run(cmd) {
            if (cmd && cmd.length > 0)
                executable.connectSource(cmd)
        }
    }

    function handleResult(data) {
        const exitCode = data["exit code"];
        const stdout = (data["stdout"] || "").trim();
        const stderr = (data["stderr"] || "").trim();
        if (exitCode === 0 && stdout.length > 0) {
            try {
                root.dv = JSON.parse(stdout);
                root.errorText = "";
            } catch (e) {
                root.dv = null;
                root.errorText = i18n("Could not parse the daily-view output.");
            }
        } else {
            root.dv = null;
            root.errorText = stderr.length > 0 ? stderr : i18n("`%1` failed (exit %2). Is aicogstress on PATH?", Plasmoid.configuration.command, exitCode);
        }
    }

    Timer {
        interval: Math.max(10, Plasmoid.configuration.refreshSeconds) * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: executable.run(Plasmoid.configuration.command)
    }

    // --- compact (panel / tray): composite number tinted by zone -----------
    compactRepresentation: Item {
        Layout.minimumWidth: compactLabel.implicitWidth + Kirigami.Units.smallSpacing * 2

        PlasmaComponents.Label {
            id: compactLabel
            anchors.centerIn: parent
            text: root.ready ? (root.dv.composite_label || "—") : "—"
            color: (root.ready && root.dv.has_activity) ? root.dv.composite_color : Kirigami.Theme.disabledTextColor
            font.pixelSize: Math.round(parent.height * 0.55)
            font.bold: true
        }
        MouseArea {
            anchors.fill: parent
            onClicked: root.expanded = !root.expanded
        }
    }

    // --- full representation: the daily view --------------------------------
    fullRepresentation: ColumnLayout {
        id: fullView
        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
        Layout.preferredWidth: Kirigami.Units.gridUnit * 24
        // Size the widget to its full content so nothing scrolls.
        Layout.preferredHeight: implicitHeight
        spacing: Kirigami.Units.smallSpacing

            // Error banner
            PlasmaComponents.Label {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                visible: root.errorText.length > 0
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.negativeTextColor
                text: root.errorText
            }

            // Header: composite / 100 on the left, the one-word advice on the
            // right, vertically centred against the score. No date.
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.smallSpacing
                Layout.rightMargin: Kirigami.Units.smallSpacing
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents.Label {
                    Layout.alignment: Qt.AlignVCenter
                    text: root.ready ? (root.dv.composite_label || "—") : "…"
                    color: (root.ready && root.dv.has_activity) ? root.dv.composite_color : Kirigami.Theme.disabledTextColor
                    font.pixelSize: Kirigami.Units.gridUnit * 2.2
                    font.bold: true
                }
                PlasmaComponents.Label {
                    Layout.alignment: Qt.AlignBottom
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    visible: root.ready && root.dv.has_activity
                    opacity: 0.6
                    text: "/ 100"
                }
                Item { Layout.fillWidth: true }

                // Intraday score progression as a severity gradient.
                Canvas {
                    id: spark
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 9
                    Layout.preferredHeight: Math.round(Kirigami.Units.gridUnit * 2.4)
                    visible: root.ready && root.dv.score_progression.length >= 2
                    Connections {
                        target: root
                        function onDvChanged() { spark.requestPaint() }
                    }
                    onWidthChanged: requestPaint()
                    onPaint: {
                        var ctx = getContext("2d");
                        ctx.reset();
                        if (!root.ready) return;
                        var s = root.dv.score_progression;
                        var n = s.length;
                        if (n < 2) return;
                        var W = width, H = height, p = 4;
                        function sx(i) { return p + (i / (n - 1)) * (W - 2 * p); }
                        function sy(v) { return H - p - (Math.max(0, Math.min(100, v)) / 100) * (H - 2 * p); }
                        ctx.strokeStyle = Qt.alpha(Kirigami.Theme.textColor, 0.15);
                        ctx.beginPath(); ctx.moveTo(p, H - p); ctx.lineTo(W - p, H - p); ctx.stroke();
                        ctx.lineWidth = 2;
                        for (var i = 0; i < n - 1; i++) {
                            ctx.strokeStyle = s[i + 1].color;
                            ctx.beginPath();
                            ctx.moveTo(sx(i), sy(s[i].value));
                            ctx.lineTo(sx(i + 1), sy(s[i + 1].value));
                            ctx.stroke();
                        }
                        var last = s[n - 1];
                        ctx.fillStyle = last.color;
                        ctx.beginPath(); ctx.arc(sx(n - 1), sy(last.value), 2.5, 0, 2 * Math.PI); ctx.fill();
                    }
                }

                Item { Layout.fillWidth: true }
                PlasmaComponents.Label {
                    Layout.alignment: Qt.AlignVCenter
                    visible: root.ready
                    text: root.ready ? root.dv.advice : ""
                    color: (root.ready && root.dv.has_activity) ? root.dv.composite_color : Kirigami.Theme.disabledTextColor
                    font.pixelSize: Math.round(Kirigami.Units.gridUnit * 1.1)
                    font.bold: true
                }
            }

            // Off-hours nag banner — visible when off-hours work is materially
            // driving the composite up. Tells the user WHY the score just jumped.
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.smallSpacing
                Layout.rightMargin: Kirigami.Units.smallSpacing
                visible: root.ready && root.dv.off_hours_nag && root.dv.off_hours_nag.length > 0
                height: nagLabel.implicitHeight + Kirigami.Units.smallSpacing * 2
                radius: Kirigami.Units.smallSpacing * 0.5
                color: Qt.rgba(0.85, 0.56, 0.22, 0.15)  // amber tint

                PlasmaComponents.Label {
                    id: nagLabel
                    anchors {
                        left: parent.left; right: parent.right
                        verticalCenter: parent.verticalCenter
                        leftMargin: Kirigami.Units.smallSpacing * 2
                        rightMargin: Kirigami.Units.smallSpacing * 2
                    }
                    text: root.ready ? (root.dv.off_hours_nag || "") : ""
                    wrapMode: Text.WordWrap
                    color: "#b36800"
                    font.bold: true
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                }
            }

            // Hourly concurrency chart
            Canvas {
                id: chart
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.smallSpacing
                Layout.rightMargin: Kirigami.Units.smallSpacing
                Layout.bottomMargin: Kirigami.Units.largeSpacing
                Layout.preferredHeight: Kirigami.Units.gridUnit * 10
                visible: root.ready && root.dv.has_activity

                Connections {
                    target: root
                    function onDvChanged() { chart.requestPaint() }
                }
                onWidthChanged: requestPaint()
                onPaint: {
                    var ctx = getContext("2d");
                    ctx.reset();
                    if (!root.ready || !root.dv.has_activity)
                        return;
                    var hours = root.dv.hours;
                    var peak = Math.max(root.dv.peak_concurrent, 1);
                    var W = width, H = height;
                    var mTop = 34, mBottom = 16, mLeft = 20, mRight = 4;
                    var plotW = W - mLeft - mRight, plotH = H - mTop - mBottom;
                    var barW = plotW / 24;

                    var title = Kirigami.Theme.textColor;
                    var faint = Kirigami.Theme.disabledTextColor;

                    // Title left-aligned with the tile titles (no subtitle).
                    ctx.fillStyle = title;
                    ctx.font = "bold 11px sans-serif";
                    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                    ctx.fillText(i18n("Concurrent agent sessions per hour"), Kirigami.Units.smallSpacing, 14);

                    var ww = root.dv.work_window;
                    if (ww && ww.end_hour > ww.start_hour) {
                        ctx.globalAlpha = 0.13; ctx.fillStyle = root.shadeColor;
                        ctx.fillRect(mLeft + ww.start_hour * barW, mTop, (ww.end_hour - ww.start_hour) * barW, plotH);
                        ctx.globalAlpha = 1.0;
                    }

                    ctx.strokeStyle = Qt.alpha(title, 0.15);
                    ctx.fillStyle = faint; ctx.font = "9px sans-serif";
                    ctx.textAlign = "right"; ctx.textBaseline = "middle";
                    for (var i = 0; i <= peak; i++) {
                        var y = mTop + plotH - (i / peak) * plotH;
                        ctx.beginPath(); ctx.moveTo(mLeft, y); ctx.lineTo(mLeft + plotW, y); ctx.stroke();
                        ctx.fillText("" + i, mLeft - 3, y);
                    }

                    var hourColors = root.dv.hour_colors || [];
                    for (var h = 0; h < 24; h++) {
                        var c = hours[h];
                        if (c <= 0) continue;
                        var barPx = (c / peak) * plotH;
                        var x = mLeft + h * barW + barW * 0.08;
                        var yb = mTop + plotH - barPx;
                        // Per-bar colour by CODL zone of the count (shared model),
                        // falling back to the flat bar colour if absent.
                        ctx.globalAlpha = 0.85;
                        ctx.fillStyle = hourColors[h] || root.barColor;
                        ctx.fillRect(x, yb, barW * 0.84, barPx);
                        ctx.globalAlpha = 1.0;
                        ctx.fillStyle = title;
                        ctx.font = "bold 9px sans-serif";
                        ctx.textAlign = "center"; ctx.textBaseline = "bottom";
                        ctx.fillText("" + c, x + barW * 0.42, yb - 1);
                    }

                    ctx.strokeStyle = Qt.alpha(title, 0.4);
                    ctx.beginPath(); ctx.moveTo(mLeft, mTop + plotH); ctx.lineTo(mLeft + plotW, mTop + plotH); ctx.stroke();
                    ctx.fillStyle = faint; ctx.font = "9px sans-serif";
                    ctx.textAlign = "center"; ctx.textBaseline = "top";
                    for (var hh = 0; hh <= 24; hh += 3)
                        ctx.fillText(("0" + hh).slice(-2), mLeft + hh * barW, mTop + plotH + 2);
                }
            }

            // Axis tiles
            Repeater {
                model: root.ready ? root.dv.axes : []
                delegate: Rectangle {
                    id: tile
                    required property var modelData

                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.smallSpacing
                    Layout.rightMargin: Kirigami.Units.smallSpacing
                    Layout.preferredHeight: tileCol.implicitHeight + Kirigami.Units.smallSpacing * 2
                    radius: Kirigami.Units.smallSpacing
                    color: Kirigami.Theme.backgroundColor
                    border.width: 1
                    border.color: Qt.alpha(Kirigami.Theme.textColor, 0.12)

                    ColumnLayout {
                        id: tileCol
                        anchors.fill: parent
                        anchors.margins: Kirigami.Units.smallSpacing
                        spacing: 2

                        RowLayout {
                            Layout.fillWidth: true
                            PlasmaComponents.Label {
                                text: tile.modelData.name
                                font.bold: true
                            }
                            Item { Layout.fillWidth: true }
                            PlasmaComponents.Label {
                                text: tile.modelData.zone_label
                                color: tile.modelData.color
                                font.bold: true
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                            }
                        }

                        PlasmaComponents.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.75
                            font: Kirigami.Theme.smallFont
                            text: tile.modelData.description
                        }

                        // Zone range bar
                        Canvas {
                            id: bar
                            Layout.fillWidth: true
                            Layout.preferredHeight: Kirigami.Units.gridUnit * 5.4
                            onWidthChanged: requestPaint()
                            Component.onCompleted: requestPaint()
                            onPaint: {
                                var ctx = getContext("2d");
                                ctx.reset();
                                var m = tile.modelData;
                                var W = width;
                                var pad = 18, inner = W - 2 * pad;
                                var barY = 46, barH = 14;
                                var baseLabelY = 10, optLabelY = 28;   // separate rows above the bar
                                var tickY = barY + barH + 8;           // boundary + endpoint numbers
                                var youY = barY + barH + 20;           // "you" on its own row below
                                var text = Kirigami.Theme.textColor;
                                var faint = Kirigami.Theme.disabledTextColor;
                                function xAt(f) { return pad + Math.max(0, Math.min(1, f)) * inner; }
                                function anch(x) { return x < pad + 22 ? "left" : x > W - pad - 22 ? "right" : "center"; }

                                ctx.globalAlpha = 0.6;
                                for (var si = 0; si < m.segments.length; si++) {
                                    var s = m.segments[si];
                                    ctx.fillStyle = s.color;
                                    ctx.fillRect(xAt(s.start), barY, xAt(s.end) - xAt(s.start), barH);
                                }
                                ctx.globalAlpha = 1.0;

                                ctx.fillStyle = faint; ctx.font = "8px sans-serif";
                                ctx.textAlign = "center"; ctx.textBaseline = "top";
                                for (var ti = 0; ti < m.boundary_ticks.length; ti++)
                                    ctx.fillText(m.boundary_ticks[ti].label, xAt(m.boundary_ticks[ti].fraction), tickY);
                                ctx.fillText("0", pad, tickY);
                                ctx.fillText("" + m.range_max, W - pad, tickY);

                                if (m.baseline_fraction !== null) {
                                    var bx = xAt(m.baseline_fraction);
                                    ctx.strokeStyle = faint; ctx.setLineDash([2, 2]);
                                    ctx.beginPath(); ctx.moveTo(bx, baseLabelY + 5); ctx.lineTo(bx, barY + barH + 4); ctx.stroke();
                                    ctx.fillStyle = faint; ctx.textBaseline = "alphabetic"; ctx.textAlign = anch(bx);
                                    ctx.fillText(m.baseline_label, bx, baseLabelY + 3);
                                }
                                if (m.optimum_fraction !== null) {
                                    var ox = xAt(m.optimum_fraction);
                                    ctx.strokeStyle = Kirigami.Theme.highlightColor; ctx.setLineDash([3, 3]);
                                    ctx.beginPath(); ctx.moveTo(ox, optLabelY + 5); ctx.lineTo(ox, barY + barH + 4); ctx.stroke();
                                    ctx.fillStyle = Kirigami.Theme.highlightColor; ctx.textBaseline = "alphabetic"; ctx.textAlign = anch(ox);
                                    ctx.fillText(m.optimum_label, ox, optLabelY + 3);
                                }
                                ctx.setLineDash([]);

                                // No-data axis (only a day with no activity at all now):
                                // draw the scale for context but no "you" marker — a 0-position
                                // marker would read as a perfect score, not "not measured".
                                if (m.has_data === false) {
                                    ctx.fillStyle = Kirigami.Theme.disabledTextColor;
                                    ctx.font = "italic 9px sans-serif";
                                    ctx.textBaseline = "top"; ctx.textAlign = "center";
                                    ctx.fillText(i18n("not measured this day"), width / 2, youY);
                                } else {
                                    var ux = xAt(Math.min(1, m.fraction));
                                    ctx.strokeStyle = text; ctx.lineWidth = 2;
                                    ctx.beginPath(); ctx.moveTo(ux, barY - 7); ctx.lineTo(ux, barY + barH + 7); ctx.stroke();
                                    ctx.fillStyle = text; ctx.font = "bold 9px sans-serif";
                                    ctx.textBaseline = "top"; ctx.textAlign = anch(ux);
                                    ctx.fillText(i18n("you %1", m.value.toFixed(2)) + (m.off_scale ? " ▶" : ""), ux, youY);
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            PlasmaComponents.Label {
                                text: tile.modelData.value_label
                                color: tile.modelData.color
                                font.bold: true
                            }
                            Item { Layout.fillWidth: true }
                            PlasmaComponents.Label {
                                opacity: 0.75
                                font: Kirigami.Theme.smallFont
                                text: tile.modelData.unit_text
                            }
                        }
                    }
                }
            }

            PlasmaComponents.Label {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                visible: root.errorText.length === 0
                opacity: 0.5
                font: Kirigami.Theme.smallFont
                text: i18n("local-only · updates live")
            }
        }
}
