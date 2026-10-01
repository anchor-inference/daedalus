package main

import (
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// A link like daedalus://open/<session-id> opens that conversation in the app, from anywhere the
// operating system can follow a link: a message, a notification, a page, a terminal. The desktop
// application is what the system hands the link to; it passes the link to this launcher, which
// turns it into an address on this machine and has the application's window show it.
//
// The application is registered for the scheme by its installers and by itself (the bundle's
// CFBundleURLTypes, the .deb's desktop entry, and Electron's own registration under HKCU on
// Windows). RegisterScheme below is for a launcher with no application beside it — a build from
// source — which registers itself on a first start.
const linkScheme = "daedalus"

// IsDeepLink reports whether an argument is one of our links rather than a command.
func IsDeepLink(arg string) bool {
	return strings.HasPrefix(strings.ToLower(arg), linkScheme+"://")
}

// DeepLinkTarget turns a link into the address that shows it, given where the app lives on this
// machine. An unknown link is not an error worth stopping for: it opens the app.
//
//	daedalus://open/<session-id>     the conversation
//	daedalus://open                  the app
//
// session/<id> is accepted for the same thing, because that is the other way the link reads.
func DeepLinkTarget(link, appURL string) string {
	parsed, err := url.Parse(strings.TrimSpace(link))
	if err != nil || !strings.EqualFold(parsed.Scheme, linkScheme) {
		return appURL
	}
	// In daedalus://open/x the host is "open" and the path is "/x"; a link written with three
	// slashes puts everything in the path instead, so the two are read as one list of segments.
	segments := make([]string, 0, 3)
	if parsed.Host != "" {
		segments = append(segments, parsed.Host)
	}
	// The escaped path is what is split, so that a segment with an encoded slash in it stays one
	// segment and is refused below rather than quietly becoming two.
	for _, segment := range strings.Split(strings.Trim(parsed.EscapedPath(), "/"), "/") {
		decoded, err := url.PathUnescape(segment)
		if err != nil {
			return appURL
		}
		if decoded != "" {
			segments = append(segments, decoded)
		}
	}
	if len(segments) < 2 {
		return appURL
	}
	switch strings.ToLower(segments[0]) {
	case "open", "session", "sessions", "agents":
		if !isIdentifier(segments[1]) {
			return appURL
		}
		return appPath(appURL, "/agents/"+segments[1])
	}
	return appURL
}

// appPath puts a path under the app's address. The address carries the language the launcher is
// speaking after the `?`, and that belongs on a deep link as much as on the front page: the link
// is often the first thing a new installation opens.
func appPath(appURL, path string) string {
	base, query, hasQuery := strings.Cut(appURL, "?")
	target := strings.TrimSuffix(base, "/") + path
	if hasQuery {
		target += "?" + query
	}
	return target
}

// isIdentifier reports whether a segment is something the app could have made: letters, digits and
// the two punctuation marks an id is written with. A link arrives from outside — a message, a page,
// anything the desktop can be persuaded to open — so what it says is checked rather than escaped,
// and anything else opens the app instead of a path made of someone else's characters.
func isIdentifier(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for _, r := range value {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
		default:
			return false
		}
	}
	return true
}

// RegisterScheme makes this machine hand daedalus:// links to this executable. It is called on
// every start and does the work once: the marker beside the data records which executable was
// registered, so moving or replacing the launcher registers the new one and an unchanged one costs
// nothing. A failure is never fatal — the app works, links do not.
func RegisterScheme(p Paths) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	marker := filepath.Join(p.Data, "scheme.txt")
	if recorded, err := os.ReadFile(marker); err == nil && strings.TrimSpace(string(recorded)) == exe {
		return nil
	}
	if err := registerScheme(exe); err != nil {
		return err
	}
	return os.WriteFile(marker, []byte(exe+"\n"), 0o644)
}

func registerScheme(exe string) error {
	switch runtime.GOOS {
	case "windows":
		return registerSchemeWindows(exe)
	case "darwin":
		// The bundle's Info.plist is the registration, and Launch Services reads it when the app is
		// first seen. There is nothing for a running process to do.
		return nil
	default:
		return registerSchemeLinux(exe)
	}
}

// registerSchemeWindows writes the three values a scheme handler is: the description, the empty
// "URL Protocol" value that marks the key as one, and the command the link is handed to. They go
// under HKCU, so no administrator is involved and the registration belongs to the operator who
// installed the launcher.
func registerSchemeWindows(exe string) error {
	key := `HKCU\Software\Classes\` + linkScheme
	commands := [][]string{
		{"add", key, "/ve", "/d", "URL:Daedalus", "/f"},
		{"add", key, "/v", "URL Protocol", "/d", "", "/f"},
		{"add", key + `\DefaultIcon`, "/ve", "/d", exe + ",0", "/f"},
		{"add", key + `\shell\open\command`, "/ve", "/d", `"` + exe + `" "%1"`, "/f"},
	}
	for _, args := range commands {
		if out, err := exec.Command("reg", args...).CombinedOutput(); err != nil {
			return combinedError("reg", out, err)
		}
	}
	return nil
}

// registerSchemeLinux writes a desktop entry, which is two things at once: the scheme handler, and
// the entry that gives the launcher a name and an icon in the desktop's own menu and task bar. It
// goes in the per-user directory, so nothing outside the operator's account is touched.
func registerSchemeLinux(exe string) error {
	dir := filepath.Join(xdgDataHome(), "applications")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	entry := "[Desktop Entry]\n" +
		"Type=Application\n" +
		"Name=Daedalus\n" +
		"Comment=Daedalus on this machine\n" +
		// Quoted per the Desktop Entry spec: unquoted, a launcher unpacked into a path with a space
		// in it is split into arguments and daedalus:// links stop working with nothing said.
		"Exec=\"" + exe + "\" %u\n" +
		"Terminal=false\n" +
		"Categories=Development;Utility;\n" +
		"MimeType=x-scheme-handler/" + linkScheme + ";\n" +
		"StartupWMClass=daedalus-desktop\n"
	file := filepath.Join(dir, "daedalus-desktop.desktop")
	if err := os.WriteFile(file, []byte(entry), 0o644); err != nil {
		return err
	}
	// Both of these are how a desktop notices; neither exists on every machine, and a desktop that
	// reads the directory by itself needs neither.
	_ = exec.Command("update-desktop-database", dir).Run()
	_ = exec.Command("xdg-mime", "default", "daedalus-desktop.desktop", "x-scheme-handler/"+linkScheme).Run()
	return nil
}

func xdgDataHome() string {
	if dir := os.Getenv("XDG_DATA_HOME"); dir != "" {
		return dir
	}
	return filepath.Join(homeDir(), ".local", "share")
}
