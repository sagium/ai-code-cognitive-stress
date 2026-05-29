import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    // The cfg_<name> aliases bind automatically to the matching <entry> in
    // config/main.xml (cfg_command -> "command", etc.).
    property alias cfg_command: commandField.text
    property alias cfg_refreshSeconds: refreshField.value

    QQC2.TextField {
        id: commandField
        Kirigami.FormData.label: i18n("Command:")
        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
    }

    QQC2.SpinBox {
        id: refreshField
        Kirigami.FormData.label: i18n("Refresh (seconds):")
        from: 10
        to: 3600
        stepSize: 10
    }

    QQC2.Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 18
        wrapMode: Text.WordWrap
        opacity: 0.7
        font: Kirigami.Theme.smallFont
        text: i18n("The widget runs this command every refresh and reads its JSON. If the score stays blank, set an absolute path to aicogstress (Plasma may not inherit your shell PATH).")
    }
}
