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
 * Private: shells out to a local CLI and renders its stdout from memory
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
import QtQuick.Window
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

    // The card draws its own glass chrome — never wrap it in the Plasma
    // background frame (which would also pad the widget beyond the card).
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    // Inline on the desktop, the containment sizes the applet from THIS
    // item's layout hints (the full representation's own hints only govern
    // the panel popup) — so mirror the card's exact size up here, with
    // min = preferred = max, and the applet can be neither larger nor
    // smaller than the card. In a panel the compact representation rules;
    // leave the hints unconstrained there.
    readonly property bool inPanel: (Plasmoid.formFactor === PlasmaCore.Types.Horizontal
                                     || Plasmoid.formFactor === PlasmaCore.Types.Vertical)
    property real fullW: Kirigami.Units.gridUnit
    property real fullH: Kirigami.Units.gridUnit
    Layout.minimumWidth: inPanel ? 0 : fullW
    Layout.preferredWidth: inPanel ? -1 : fullW
    Layout.maximumWidth: inPanel ? Number.POSITIVE_INFINITY : fullW
    Layout.minimumHeight: inPanel ? 0 : fullH
    Layout.preferredHeight: inPanel ? -1 : fullH
    Layout.maximumHeight: inPanel ? Number.POSITIVE_INFINITY : fullH

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
        id: fullView
        spacing: 0

        // The plasmoid hugs the card EXACTLY: min = preferred = max on both
        // axes (the implicit size is the visible children's — the card view
        // sized to its measured content, plus the error banner if shown), so
        // the containment can neither stretch nor shrink it. There is no
        // shadow margin to reserve: the card's CSS box-shadow doesn't affect
        // layout and falls outside the view either way. Floored at a gridUnit
        // so the applet never collapses to 0×0 (and stays grabbable) in the
        // instant before the first card arrives and is measured.
        readonly property real exactWidth: Math.max(implicitWidth, Kirigami.Units.gridUnit)
        readonly property real exactHeight: Math.max(implicitHeight, Kirigami.Units.gridUnit)
        Layout.minimumWidth: exactWidth
        Layout.preferredWidth: exactWidth
        Layout.maximumWidth: exactWidth
        Layout.minimumHeight: exactHeight
        Layout.preferredHeight: exactHeight
        Layout.maximumHeight: exactHeight

        // …and mirror it to the root item, which is what the desktop
        // containment actually reads (see the hints on PlasmoidItem).
        onExactWidthChanged: root.fullW = exactWidth
        onExactHeightChanged: root.fullH = exactHeight
        Component.onCompleted: { root.fullW = exactWidth; root.fullH = exactHeight }

        // Error banner — shown instead of (or before the first) card.
        PlasmaComponents.Label {
            Layout.preferredWidth: cardCell.cardWidth
            Layout.maximumWidth: cardCell.cardWidth
            visible: root.errorText.length > 0
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.negativeTextColor
            text: root.errorText
        }

        // Supersampling host — crisp text on fractionally-scaled displays.
        // On a fractional display scale (e.g. 1.5×) QtWebEngine rasterizes web
        // content at the fractional device-pixel ratio and the result gets
        // resampled, giving pixelated text and faint tile-seam lines through
        // the card. We dodge it by rendering the card larger internally
        // (zoomFactor + a proportionally larger pixel box) and scaling the view
        // back down to the card's true size for display: the on-screen size is
        // unchanged, but the card is backed by more pixels and downsampled, so
        // text stays crisp. Integer scales (1×, 2×, 3×) rasterize cleanly, so
        // there superSample is 1 and this is a no-op with zero overhead. This
        // layout cell carries the *display* size; the WebEngineView inside is
        // the (super-sampled) render target.
        Item {
            id: cardCell
            // The display's device-pixel ratio (1.0 = unscaled, 1.5 = 150% …),
            // re-evaluated if the widget moves to a screen with a different
            // scale. Only fractional ratios need the supersample; render at the
            // next whole multiple (1.5×→2, 2.5×→3) and downsample.
            readonly property real dpr: Screen.devicePixelRatio
            readonly property real superSample: (Math.abs(dpr - Math.round(dpr)) > 0.01)
                                                 ? Math.ceil(dpr) : 1.0
            readonly property int cardWidth: 384
            property int cardHeight: 0   // card's CSS height, measured after load

            Layout.preferredWidth: cardWidth
            Layout.minimumWidth: cardWidth
            Layout.maximumWidth: cardWidth
            Layout.preferredHeight: cardHeight
            Layout.minimumHeight: cardHeight
            Layout.maximumHeight: cardHeight
            visible: root.cardHtml.length > 0 && cardHeight > 0
            clip: true

            WebEngineView {
                id: cardView
                // Render box is the display size × superSample; the page is
                // zoomed by the same factor so the (fixed-width) card fills it
                // exactly, then a top-left Scale shrinks the whole view back to
                // the display size. getBoundingClientRect (CSS px) is unaffected
                // by zoomFactor, so the measured height stays in card CSS px.
                width: cardCell.cardWidth * cardCell.superSample
                height: cardCell.cardHeight * cardCell.superSample
                zoomFactor: cardCell.superSample
                transform: Scale {
                    xScale: 1.0 / cardCell.superSample
                    yScale: 1.0 / cardCell.superSample
                }

                // When supersampling, downsample through a mipmapped layer.
                // A plain transform-scale only does bilinear filtering, which
                // thins glyph edges (descenders/serifs look clipped); a
                // mipmapped layer picks the right texture level and downsamples
                // cleanly. No layer at 1× (integer scaling) — zero overhead.
                smooth: true
                antialiasing: true
                layer.enabled: cardCell.superSample > 1
                layer.smooth: true
                layer.mipmap: true

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
                    // The wrapper strips the card's drop shadow: this view hugs
                    // the card exactly, so the shadow would be clipped to a faint
                    // corner spill anyway — without it the corners are pure
                    // desktop pass-through. (Übersicht and the browser preview
                    // keep the full shadow; they render unclipped.)
                    loadHtml("<!doctype html><html><head><meta charset='utf-8'>"
                             + "<style>.cogstress { box-shadow: none !important; }</style>"
                             + "</head><body style='margin:0;background:transparent'>"
                             + root.cardHtml + "</body></html>");
                }

                onLoadingChanged: function (loadingInfo) {
                    if (loadingInfo.status === WebEngineView.LoadSucceededStatus) {
                        // Measure the card's border-box (CSS px; ceil — the card
                        // has no margins) so the cell is exactly as tall as the
                        // card. The 2× render box derives from this. The tabbed
                        // card also pushes its height live via document.title
                        // (see onTitleChanged); this is the initial/fallback
                        // measure for that and the only one for a plain card.
                        runJavaScript(
                            "Math.ceil(document.querySelector('.cogstress').getBoundingClientRect().height)",
                            function (h) {
                                if (h && h > 0)
                                    cardCell.cardHeight = h;
                            });
                    }
                }

                // Height bridge: the tabbed card sets document.title to
                // 'cogstress:h:<px>' on load and whenever a tab switch changes
                // its height, so the widget grows/shrinks to the active view
                // without a CLI round-trip. (No QWebChannel needed — title is
                // the lightest one-way page→host channel QtWebEngine exposes.)
                onTitleChanged: {
                    var m = /^cogstress:h:(\d+)$/.exec(cardView.title);
                    if (m) {
                        var hh = parseInt(m[1], 10);
                        if (hh > 0)
                            cardCell.cardHeight = hh;
                    }
                }
            }
        }
    }
}
