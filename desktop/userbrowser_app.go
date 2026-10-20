package main

// The launcher's face of the operator's own Chrome: the report the app's "Your Chrome" card and the
// doctor read, the register and unregister the card's button asks for, the work a start does, and
// the lines `status` prints.

import (
	"fmt"
	"os"
	"strings"
)

// UserBrowserReport is everything the launcher knows about the operator's Chrome. The app renders
// it; the connection itself (extension version, profile, heartbeat) is the user daemon's to report,
// through the host, and is not here.
type UserBrowserReport struct {
	Browsers    []ChromeInstall `json:"browsers"`
	Policies    []PolicyFinding `json:"policies"`
	Connector   ConnectorState  `json:"connector"`
	Extension   ExtensionState  `json:"extension"`
	HostName    string          `json:"host_name"`
	ExtensionID string          `json:"extension_id"`
	// UserDir is the user daemon's run directory, the same value the host has as BROWSER_USER_DIR.
	UserDir string `json:"user_dir"`
}

// newConnector is the connector of this installation on this machine, for the probe's system.
func newConnector(p Paths, pr chromeProbe) connector {
	exe, _ := os.Executable()
	return connector{
		goos:     pr.goos,
		local:    p.Local,
		browserd: browserdBinary(exe, os.Getenv, exists),
		runDir:   browserdUserRunDir(p),
		places:   chromePlacesFor(pr.goos, pr.home, pr.getenv),
		present:  func(cp chromePlaces) bool { return channelPresent(pr, cp) },
		registry: pr.registry,
	}
}

// channelPresent reports whether a channel is on this machine: its executable or its profiles.
func channelPresent(pr chromeProbe, cp chromePlaces) bool {
	if cp.UserData != "" && pr.exists(cp.UserData) {
		return true
	}
	for _, exe := range cp.Executables {
		if pr.exists(exe) {
			return true
		}
	}
	return false
}

// buildUserBrowserReport reads the whole report. It writes nothing.
func buildUserBrowserReport(p Paths, pr chromeProbe, c connector) UserBrowserReport {
	report := UserBrowserReport{
		Browsers:    detectChrome(pr),
		Policies:    chromePolicyFindings(pr),
		Connector:   c.State(),
		HostName:    chromeHostName,
		ExtensionID: chromeExtensionID,
		UserDir:     browserdUserRunDir(p),
	}
	if report.Policies == nil {
		report.Policies = []PolicyFinding{}
	}
	for i := range report.Browsers {
		if report.Browsers[i].Supported {
			report.Browsers[i].Connector = report.Connector.channelState(report.Browsers[i].Channel)
		}
	}
	report.Extension = ExtensionState{Path: extensionDir(p)}
	if version, _, err := verifyExtension(extensionDir(p)); err == nil {
		report.Extension.Ready, report.Extension.Version = true, version
	} else if bundledExtension(executable(), os.Getenv, exists) == "" {
		report.Extension.Error = "this build carries no browser extension"
	} else {
		report.Extension.Error = "the extension folder is not in place yet; it is copied when Daedalus starts"
	}
	return report
}

func executable() string {
	exe, _ := os.Executable()
	return exe
}

// UserBrowser is the report for this installation.
func (a *App) UserBrowser() UserBrowserReport {
	pr := systemChromeProbe()
	return buildUserBrowserReport(a.paths, pr, newConnector(a.paths, pr))
}

// RegisterConnector writes the connector at the operator's request and reports the result. A Chrome
// that is running reads host manifests only when it starts, so the answer says to restart it.
func (a *App) RegisterConnector() (UserBrowserReport, error) {
	pr := systemChromeProbe()
	c := newConnector(a.paths, pr)
	if err := c.Register(); err != nil {
		return buildUserBrowserReport(a.paths, pr, c), err
	}
	a.log("registered the Chrome connector %s", chromeHostName)
	report := buildUserBrowserReport(a.paths, pr, c)
	for _, b := range report.Browsers {
		if b.Running && b.Supported {
			report.Connector.RestartChrome = true
		}
	}
	return report, nil
}

// UnregisterConnector removes the connector at the operator's request.
func (a *App) UnregisterConnector() (UserBrowserReport, error) {
	pr := systemChromeProbe()
	c := newConnector(a.paths, pr)
	err := c.Unregister()
	if err == nil {
		a.log("removed the Chrome connector %s", chromeHostName)
	}
	return buildUserBrowserReport(a.paths, pr, c), err
}

// removeConnector is the uninstaller's share: the connector names a browserd that is about to be
// deleted, and a registration left behind would have Chrome start a program that is not there.
// Only this installation's own registration is removed (connector.Owns).
func removeConnector(p Paths, log func(string, ...any)) {
	pr := systemChromeProbe()
	c := newConnector(p, pr)
	if !c.Owns() {
		return
	}
	if err := c.Unregister(); err != nil {
		log("the Chrome connector could not be removed: %v", err)
		return
	}
	log("removed the Chrome connector %s", chromeHostName)
}

// prepareUserBrowser is a start's share: the extension folder brought up to this release, and a
// registration that exists rewritten if it names another browserd. Failures are logged, not
// returned — the rest of Daedalus does not depend on the operator's Chrome.
func (n *Native) prepareUserBrowser() {
	src := bundledExtension(executable(), os.Getenv, exists)
	if state, err := installExtension(src, extensionDir(n.paths)); err != nil {
		n.log("the browser extension: %v", err)
	} else if src != "" && state.Ready {
		n.log("the browser extension %s is in %s", state.Version, state.Path)
	}
	pr := systemChromeProbe()
	if rewritten, err := newConnector(n.paths, pr).Refresh(); err != nil {
		n.log("the Chrome connector could not be brought up to date: %v", err)
	} else if rewritten {
		n.log("the Chrome connector now points at this release's browserd")
	}
}

// userBrowserLines are the doctor's lines for `status`: whether Chrome is here, whether the
// connector is registered, and what policy stands in the way.
func userBrowserLines(r UserBrowserReport) []string {
	var lines []string
	var found []string
	for _, b := range r.Browsers {
		label := b.Name
		if !b.Supported {
			label += " — unsupported: " + b.Note
		} else if len(b.Profiles) > 0 {
			label += fmt.Sprintf(" (%d profiles)", len(b.Profiles))
		}
		found = append(found, label)
	}
	if len(found) == 0 {
		lines = append(lines, "chrome       not found")
	} else {
		lines = append(lines, "chrome       "+strings.Join(found, "; "))
	}
	switch {
	case !r.Connector.Available:
		lines = append(lines, "connector    unavailable — "+r.Connector.Reason)
	case r.Connector.Registered:
		lines = append(lines, "connector    registered ("+chromeHostName+")")
	case r.Connector.Stale:
		lines = append(lines, "connector    registered, outdated — the next start rewrites it")
	default:
		lines = append(lines, "connector    not registered — Settings → Environments → Your Chrome")
	}
	if r.Extension.Ready {
		lines = append(lines, "extension    "+r.Extension.Version+" in "+r.Extension.Path)
	} else {
		lines = append(lines, "extension    "+r.Extension.Error)
	}
	if len(r.Policies) == 0 {
		lines = append(lines, "policies     none in the way")
	}
	for _, f := range r.Policies {
		lines = append(lines, "policies     "+f.Policy+": "+f.Detail+" ("+f.Source+")")
	}
	return lines
}
