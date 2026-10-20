package main

// The operator's own Chrome, driven through the Daedalus extension. The launcher's share of it is
// what only something on this machine can do: find which Google Chrome is installed and which
// profiles it has, register the native-messaging host Chrome starts the bridge through, keep the
// unpacked extension where the operator loads it from, and run the second browser daemon — the
// `user` one, which launches no Chromium and waits for the extension to connect.
//
// Chrome only, whatever its version. Other Chromium browsers may happen to read the same host
// registration, but nothing here looks for them or promises them anything.
//
// Nothing is registered by itself. The connector is written when the operator presses the button
// in the app (or runs the launcher's API), and removed the same way or on uninstall: an installer
// that silently writes native hosts into every browser it finds is exactly the behaviour the
// operator would be right to distrust.

import (
	"encoding/json"
	"os"
	"os/exec"
	"path"
	"runtime"
	"sort"
	"strings"
)

const (
	// chromeHostName is the native-messaging host the extension connects to with
	// chrome.runtime.connectNative. It has to match the extension's own constant (NATIVE_HOST in
	// extension/src/protocol.ts) letter for letter, and Chrome allows only lowercase letters,
	// digits, dots and underscores in it.
	chromeHostName = "com.daedalus.browser"

	// chromeExtensionID is the extension's id, pinned by the public key in its manifest. It is the
	// only origin the host manifest allows, so no other extension — however it got installed — can
	// start the bridge. The payload copy refuses an extension whose key derives to another id.
	chromeExtensionID = "lkhhklicnjlplokpadpccndipomikoaj"

	// bridgeRunDirFlag is how the wrapper tells `browserd bridge` where the user daemon writes its
	// endpoint and token. Chrome starts a native host with the caller's origin as its first argument
	// and nothing else of ours, so the run directory travels in the wrapper the manifest points at.
	bridgeRunDirFlag = "--run-dir"
)

// chromeOrigin is the extension's origin as Chrome passes it to the host and as the manifest's
// allowed_origins must spell it, trailing slash included.
func chromeOrigin() string { return "chrome-extension://" + chromeExtensionID + "/" }

// chromeChannel is one of Google Chrome's release channels. Each installs side by side with the
// others and keeps its own user data directory, so each has its own profiles and, on macOS and
// Linux, its own folder of native-messaging hosts.
type chromeChannel struct {
	ID   string // stable, beta, dev, canary
	Name string // what the operator sees in the app
}

var chromeChannels = []chromeChannel{
	{"stable", "Google Chrome"},
	{"beta", "Google Chrome Beta"},
	{"dev", "Google Chrome Dev"},
	{"canary", "Google Chrome Canary"},
}

// chromePlaces is where one channel lives on one operating system: the executables it is
// installed as, the commands it answers to on PATH, its user data directory, and the folder Chrome
// reads user-level native-messaging hosts from (empty on Windows, which reads the registry).
type chromePlaces struct {
	Channel     chromeChannel
	Executables []string
	Commands    []string
	UserData    string
	HostsDir    string
}

// joinFor joins path elements the way goos spells a path, so the tables below can be tested for
// every system from one machine: filepath.Join would write a Windows path with forward slashes on
// Linux and hide a wrong separator from the test that is supposed to catch it.
func joinFor(goos string, elem ...string) string {
	if goos != "windows" {
		return path.Join(elem...)
	}
	parts := make([]string, 0, len(elem))
	for i, e := range elem {
		e = strings.ReplaceAll(e, "/", `\`)
		if i > 0 {
			e = strings.TrimLeft(e, `\`)
		}
		if i < len(elem)-1 {
			e = strings.TrimRight(e, `\`)
		}
		if e != "" {
			parts = append(parts, e)
		}
	}
	return strings.Join(parts, `\`)
}

// linuxChromeConfig is the folder Chrome on Linux keeps its user data directories in. Chrome
// honours CHROME_CONFIG_HOME first and then XDG_CONFIG_HOME, and a relative value of either is
// ignored by it, so it is ignored here too.
func linuxChromeConfig(home string, getenv func(string) string) string {
	for _, name := range []string{"CHROME_CONFIG_HOME", "XDG_CONFIG_HOME"} {
		if dir := getenv(name); dir != "" && strings.HasPrefix(dir, "/") {
			return dir
		}
	}
	return path.Join(home, ".config")
}

// chromePlacesFor is the table of every channel's places on goos.
func chromePlacesFor(goos, home string, getenv func(string) string) []chromePlaces {
	var out []chromePlaces
	switch goos {
	case "windows":
		local := getenv("LOCALAPPDATA")
		if local == "" && home != "" {
			local = joinFor(goos, home, "AppData", "Local")
		}
		roots := []string{}
		for _, name := range []string{"ProgramFiles", "ProgramFiles(x86)"} {
			if dir := getenv(name); dir != "" {
				roots = append(roots, dir)
			}
		}
		if len(roots) == 0 {
			roots = []string{`C:\Program Files`, `C:\Program Files (x86)`}
		}
		if local != "" {
			roots = append(roots, local)
		}
		// Canary is "Chrome SxS" on Windows, a name from when it was the side-by-side build.
		folders := map[string]string{"stable": "Chrome", "beta": "Chrome Beta", "dev": "Chrome Dev", "canary": "Chrome SxS"}
		for _, ch := range chromeChannels {
			folder := folders[ch.ID]
			var exes []string
			for _, root := range roots {
				exes = append(exes, joinFor(goos, root, "Google", folder, "Application", "chrome.exe"))
			}
			places := chromePlaces{Channel: ch, Executables: exes}
			if local != "" {
				places.UserData = joinFor(goos, local, "Google", folder, "User Data")
			}
			out = append(out, places)
		}
	case "darwin":
		apps := map[string]string{"stable": "Google Chrome", "beta": "Google Chrome Beta", "dev": "Google Chrome Dev", "canary": "Google Chrome Canary"}
		support := map[string]string{"stable": "Chrome", "beta": "Chrome Beta", "dev": "Chrome Dev", "canary": "Chrome Canary"}
		for _, ch := range chromeChannels {
			app := apps[ch.ID]
			exe := path.Join(app+".app", "Contents", "MacOS", app)
			exes := []string{path.Join("/Applications", exe)}
			if home != "" {
				exes = append(exes, path.Join(home, "Applications", exe))
			}
			udd := path.Join(home, "Library", "Application Support", "Google", support[ch.ID])
			out = append(out, chromePlaces{Channel: ch, Executables: exes, UserData: udd, HostsDir: path.Join(udd, "NativeMessagingHosts")})
		}
	default:
		config := linuxChromeConfig(home, getenv)
		// Dev is "unstable" in every Linux name Google ships it under.
		names := map[string]string{"stable": "google-chrome", "beta": "google-chrome-beta", "dev": "google-chrome-unstable", "canary": "google-chrome-canary"}
		opt := map[string]string{"stable": "chrome", "beta": "chrome-beta", "dev": "chrome-unstable", "canary": "chrome-canary"}
		for _, ch := range chromeChannels {
			name := names[ch.ID]
			commands := []string{name}
			if ch.ID == "stable" {
				commands = append(commands, "google-chrome-stable")
			}
			udd := path.Join(config, name)
			out = append(out, chromePlaces{
				Channel:     ch,
				Executables: []string{path.Join("/opt/google", opt[ch.ID], "chrome")},
				Commands:    commands,
				UserData:    udd,
				HostsDir:    path.Join(udd, "NativeMessagingHosts"),
			})
		}
	}
	return out
}

// chromeHostsKey is the registry key, under HKEY_CURRENT_USER, Chrome reads native-messaging hosts
// from on Windows. Every channel of Google Chrome reads this one key; a value under it per host
// names that host's manifest file.
const chromeHostsKey = `Software\Google\Chrome\NativeMessagingHosts`

// ChromeProfile is one profile of a user data directory, as Chrome's own Local State names it.
type ChromeProfile struct {
	Dir      string `json:"dir"`
	Name     string `json:"name"`
	LastUsed bool   `json:"last_used,omitempty"`
}

// ChromeInstall is one installed channel of Chrome, or one the launcher found and cannot use.
type ChromeInstall struct {
	Channel    string          `json:"channel"`
	Name       string          `json:"name"`
	Executable string          `json:"executable,omitempty"`
	UserData   string          `json:"user_data_dir,omitempty"`
	Profiles   []ChromeProfile `json:"profiles"`
	Running    bool            `json:"running"`
	// Packaging is "native", "flatpak" or "snap". A sandboxed Chrome cannot start a program outside
	// its sandbox, so a native-messaging host never runs for it: such an install is listed, with
	// Supported false and the reason, rather than left out and the operator left wondering.
	Packaging string `json:"packaging"`
	Supported bool   `json:"supported"`
	Note      string `json:"note,omitempty"`
	// Connector is this channel's view of the registration: "registered", "stale" (ours, but
	// pointing at another bridge — an upgrade moved it, or it was edited) or "absent".
	Connector string `json:"connector"`
}

// chromeProbe is everything detection reads, so each system's detection runs in a test on any
// machine against a fake of it.
type chromeProbe struct {
	goos     string
	home     string
	getenv   func(string) string
	exists   func(string) bool
	readFile func(string) ([]byte, error)
	lookPath func(string) (string, error)
	readDir  func(string) ([]os.DirEntry, error)
	registry chromeRegistry
	running  func(userData string) bool
}

func systemChromeProbe() chromeProbe {
	home, _ := os.UserHomeDir()
	return chromeProbe{
		goos:     runtime.GOOS,
		home:     home,
		getenv:   os.Getenv,
		exists:   exists,
		readFile: os.ReadFile,
		lookPath: exec.LookPath,
		readDir:  os.ReadDir,
		registry: systemChromeRegistry(),
		running:  chromeRunning,
	}
}

// appPathsKey is where Windows records an installed program's executable for "Run" and for the
// shell. Chrome's installer writes chrome.exe there, per user or per machine, and that path is the
// one to trust over a guessed folder when the operator installed somewhere unusual.
const appPathsKey = `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe`

// detectChrome lists every channel of Google Chrome found on this machine, with its profiles and
// whether it is running. A channel counts as found when its executable is there or its user data
// directory is: Chrome removed but its profiles left behind still has profiles to show, and the
// app says the executable is missing rather than pretending there is nothing.
func detectChrome(pr chromeProbe) []ChromeInstall {
	var out []ChromeInstall
	appPath := ""
	if pr.goos == "windows" && pr.registry != nil {
		for _, root := range []regRoot{regCurrentUser, regLocalMachine} {
			if v, ok := pr.registry.String(root, appPathsKey, ""); ok && v != "" {
				appPath = strings.Trim(v, `"`)
				break
			}
		}
	}
	for _, places := range chromePlacesFor(pr.goos, pr.home, pr.getenv) {
		exe := ""
		for _, command := range places.Commands {
			if found, err := pr.lookPath(command); err == nil {
				exe = found
				break
			}
		}
		if exe == "" && appPath != "" && windowsChannelOf(appPath) == places.Channel.ID && pr.exists(appPath) {
			exe = appPath
		}
		if exe == "" {
			for _, candidate := range places.Executables {
				if pr.exists(candidate) {
					exe = candidate
					break
				}
			}
		}
		hasData := places.UserData != "" && pr.exists(places.UserData)
		if exe == "" && !hasData {
			continue
		}
		install := ChromeInstall{
			Channel:    places.Channel.ID,
			Name:       places.Channel.Name,
			Executable: exe,
			UserData:   places.UserData,
			Profiles:   []ChromeProfile{},
			Packaging:  "native",
			Supported:  true,
		}
		if hasData {
			install.Profiles = readChromeProfiles(pr.readFile, joinFor(pr.goos, places.UserData, "Local State"))
			if pr.running != nil {
				install.Running = pr.running(places.UserData)
			}
		}
		if exe == "" {
			install.Note = "its profiles are here but the browser itself was not found"
		}
		out = append(out, install)
	}
	if pr.goos == "linux" {
		out = append(out, sandboxedChrome(pr)...)
	}
	return out
}

// windowsChannelOf names the channel an executable path belongs to by the folder it is in.
func windowsChannelOf(exe string) string {
	lower := strings.ToLower(exe)
	switch {
	case strings.Contains(lower, `\chrome sxs\`):
		return "canary"
	case strings.Contains(lower, `\chrome beta\`):
		return "beta"
	case strings.Contains(lower, `\chrome dev\`):
		return "dev"
	}
	return "stable"
}

// sandboxedChrome finds the Chrome packages a native host cannot serve on Linux. Flatpak's Chrome
// is the community package com.google.Chrome; there is no portal through which it may start a
// program outside its sandbox. Google publishes no snap of Chrome, but a snap by that name would be
// confined the same way, so it is named too.
func sandboxedChrome(pr chromeProbe) []ChromeInstall {
	const why = "a sandboxed Chrome cannot start the connector; install Chrome from Google's own package instead"
	var out []ChromeInstall
	flatpaks := []struct{ id, channel, name string }{
		{"com.google.Chrome", "stable", "Google Chrome (Flatpak)"},
		{"com.google.ChromeDev", "dev", "Google Chrome Dev (Flatpak)"},
	}
	for _, f := range flatpaks {
		for _, dir := range []string{
			path.Join(pr.home, ".local/share/flatpak/app", f.id),
			path.Join("/var/lib/flatpak/app", f.id),
		} {
			if pr.exists(dir) {
				out = append(out, ChromeInstall{Channel: f.channel, Name: f.name, Executable: dir, Profiles: []ChromeProfile{}, Packaging: "flatpak", Note: why, Connector: "absent"})
				break
			}
		}
	}
	if pr.exists("/snap/google-chrome") {
		out = append(out, ChromeInstall{Channel: "stable", Name: "Google Chrome (Snap)", Executable: "/snap/google-chrome", Profiles: []ChromeProfile{}, Packaging: "snap", Note: why, Connector: "absent"})
	}
	return out
}

// readChromeProfiles reads the profiles Chrome lists in Local State's profile.info_cache: the
// folder each lives in and the name the operator gave it. An unreadable or foreign file is no
// profiles rather than an error — the operator still sees the browser.
func readChromeProfiles(readFile func(string) ([]byte, error), localState string) []ChromeProfile {
	out := []ChromeProfile{}
	body, err := readFile(localState)
	if err != nil {
		return out
	}
	var state struct {
		Profile struct {
			InfoCache map[string]struct {
				Name string `json:"name"`
			} `json:"info_cache"`
			LastUsed string `json:"last_used"`
		} `json:"profile"`
	}
	if json.Unmarshal(body, &state) != nil {
		return out
	}
	for dir, info := range state.Profile.InfoCache {
		name := info.Name
		if name == "" {
			name = dir
		}
		out = append(out, ChromeProfile{Dir: dir, Name: name, LastUsed: dir == state.Profile.LastUsed})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Dir < out[j].Dir })
	return out
}
