package quartz

import "github.com/ascorblack/daedalus/deskd/internal/backend"

// The privacy panes of System Settings. The anchors date from System Preferences and still open the
// right list on Ventura and later, which kept the old URL scheme rather than break every app's link.
const settingsBase = "x-apple.systempreferences:com.apple.preference.security"

// SettingsURL is the System Settings deep link where the operator grants the permission called which
// (backend.PermScreen, PermAccessibility or PermInputMonitoring); an unknown name opens Privacy &
// Security itself.
func SettingsURL(which string) string {
	switch which {
	case backend.PermScreen:
		return settingsBase + "?Privacy_ScreenCapture"
	case backend.PermAccessibility:
		return settingsBase + "?Privacy_Accessibility"
	case backend.PermInputMonitoring:
		return settingsBase + "?Privacy_ListenEvent"
	}
	return settingsBase
}

// StaleGrantHint explains the state an ad hoc signed update leaves behind: System Settings lists the
// app as allowed while macOS refuses it, because the grant is keyed to the old build's signature. A
// toggle off and on is not enough; the entry has to go.
const StaleGrantHint = "if System Settings already shows Daedalus as allowed, the grant belongs to " +
	"an earlier build (the app is signed ad hoc, so every update looks like a new program): remove " +
	"Daedalus from the list with the minus button and add it again"

// permissionOf is a probe's answer as a Permission. The probes cannot tell "refused" from "never
// asked", so a false is reported as denied either way.
func permissionOf(granted bool) backend.Permission {
	if granted {
		return backend.Granted
	}
	return backend.Denied
}

// permissionDetail is the Detail of a PermissionError for which: where to grant it, and the stale
// grant case, which is the likelier one once the operator has granted it before.
func permissionDetail(which string) string {
	return "grant it in System Settings (" + SettingsURL(which) + "); " + StaleGrantHint
}
