/*
 * Cognitive Stress — KDE Plasma 6 widget. Thin host only.
 *
 * The card itself — TODAY's full daily view (composite, sparkline, off-hours
 * nag, per-hour concurrency chart, axis tiles) — is rendered by the CLI:
 * `aicogstress --emit-html-card` prints one self-contained HTML fragment
 * (inline CSS + SVG, no scripts), built by stress_levels/widget_card.py.
 * That module is the SINGLE renderer shared with the macOS Übersicht widget
 * and the browser preview, so the surfaces can't drift. This file just runs
 * the CLI on a timer and shows its output in a WebEngineView.
 *
 * The compact (panel) representation reads the headline from the card's
 * data-* attributes (data-composite-label / -color / data-has-activity) —
 * no second CLI invocation, no HTML parsing beyond the root tag.
 *
 * Local-only: shells out to a local CLI and renders its stdout from memory
 * (loadHtml, about:blank base). The card contains no scripts and no external
 * references; the only JavaScript run in the view is our own one-liner that
 * measures the card's height.
 *
 * Plasma 6 / Qt 6 only. Needs the QtWebEngine QML module (the same dependency
 * as KDE's own webbrowser applet; package `qml6-module-qtwebengine` /
 * `qt6-webengine` depending on distro).
 */
import QtQuick
import QtQuick.Layouts
import QtWebEngine
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property string cardHtml: ""
    property string errorText: ""
    readonly property bool ready: cardHtml.length > 0 && errorText === ""

    // Headline for the compact (panel) label, parsed from the card root's
    // data-* attributes.
    function cardAttr(name) {
        const m = root.cardHtml.match(new RegExp(name + '="([^"]*)"'));
        return m ? m[1] : "";
    }
    readonly property string compositeLabel: ready ? cardAttr("data-composite-label") : "—"
    readonly property string compositeColor: cardAttr("data-composite-color")
    readonly property bool hasActivity: cardAttr("data-has-activity") === "true"

    // This is a desktop widget: show the full card inline. In a panel
    // (Horizontal/Vertical form factor) fall back to the compact composite
    // score, which expands to the full view on click.
    preferredRepresentation: (Plasmoid.formFactor === PlasmaCore.Types.Horizontal
                              || Plasmoid.formFactor === PlasmaCore.Types.Vertical)
        ? compactRepresentation : fullRepresentation

    // --- data: run the local CLI on a timer, keep its HTML stdout ----------
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
        if (exitCode === 0 && stdout.indexOf('class="cogstress"') !== -1) {
            root.cardHtml = stdout;
            root.errorText = "";
        } else if (exitCode === 0) {
            root.errorText = i18n("Unexpected output — `%1` did not print a widget card.", Plasmoid.configuration.command);
        } else {
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
            text: root.compositeLabel || "—"
            color: (root.ready && root.hasActivity) ? root.compositeColor : Kirigami.Theme.disabledTextColor
            font.pixelSize: Math.round(parent.height * 0.55)
            font.bold: true
        }
        MouseArea {
            anchors.fill: parent
            onClicked: root.expanded = !root.expanded
        }
    }

    // --- full representation: the card in a web view -------------------------
    fullRepresentation: ColumnLayout {
        spacing: 0

        // Error banner — shown instead of (or before the first) card.
        PlasmaComponents.Label {
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.smallSpacing
            Layout.maximumWidth: cardView.cardWidth
            visible: root.errorText.length > 0
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.negativeTextColor
            text: root.errorText
        }

        WebEngineView {
            id: cardView

            // The card is fixed-width (widget_card.CARD_WIDTH); height follows
            // the content, measured after each load.
            readonly property int cardWidth: 384
            property int cardHeight: Kirigami.Units.gridUnit * 30

            Layout.preferredWidth: cardWidth
            Layout.minimumWidth: cardWidth
            Layout.preferredHeight: cardHeight
            visible: root.cardHtml.length > 0

            backgroundColor: "transparent"
            settings.showScrollBars: false
            settings.localContentCanAccessRemoteUrls: false
            settings.localContentCanAccessFileUrls: false

            Connections {
                target: root
                function onCardHtmlChanged() { cardView.showCard() }
            }
            Component.onCompleted: if (root.cardHtml.length > 0) showCard()

            function showCard() {
                loadHtml("<!doctype html><html><head><meta charset='utf-8'></head>"
                         + "<body style='margin:0;background:transparent'>"
                         + root.cardHtml + "</body></html>");
            }

            onLoadingChanged: function (loadingInfo) {
                if (loadingInfo.status === WebEngineView.LoadSucceededStatus) {
                    // Size the widget to the card so nothing scrolls.
                    runJavaScript("document.body.scrollHeight", function (h) {
                        if (h && h > 0)
                            cardView.cardHeight = h;
                    });
                }
            }
        }
    }
}
