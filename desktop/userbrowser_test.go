package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"strings"
	"testing"
)

// extensionPublicKey is the public half of the key that pins the extension's id: what its
// manifest carries as "key". Public by nature — every copy of the extension holds it.
const extensionPublicKey = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAplP1rwlqrvewb62Pu3s8mFau+6kjNQMrqeOADm8HN+IVbozjE7IEcG8O14jq+aTQQ1V6J0AdcSQ7vqaUaNqY77SH5qJ2M1VQZsNhQB3px/DVg52ZgRlRkcob58YFtGxqcSuVBDgHH+11G05BFuhVtQaRmxHqNZPwwYlE73krKZ+6laIWPtMflDniQZoq0RHEc7RYh4o2iwU6rN3EExd8BQ3QbAzIYU4gsY81OGRaITowWPCWSF+JSfbvzPoFQSnB14T37WAtTznJ+wEMUh5htS+hqZOxlCqR7aE4Cv5yh1W8kKvmAI255Cx1GhVbgJE5u7sUNBOG3Cyg/agy2tCkqQIDAQAB"

func envOf(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func placesByChannel(list []chromePlaces) map[string]chromePlaces {
	out := map[string]chromePlaces{}
	for _, p := range list {
		out[p.Channel.ID] = p
	}
	return out
}

// Where each channel keeps its profiles and reads its hosts from, per system. These are the paths
// Chrome itself uses; a wrong one is a connector written where Chrome never looks.
func TestChromePlacesPerSystem(t *testing.T) {
	cases := []struct {
		goos, home string
		env        map[string]string
		channel    string
		userData   string
		hostsDir   string
		executable string
	}{
		{"windows", `C:\Users\someone`, map[string]string{"LOCALAPPDATA": `C:\Users\someone\AppData\Local`, "ProgramFiles": `C:\Program Files`},
			"stable", `C:\Users\someone\AppData\Local\Google\Chrome\User Data`, "", `C:\Program Files\Google\Chrome\Application\chrome.exe`},
		{"windows", `C:\Users\someone`, map[string]string{"LOCALAPPDATA": `C:\Users\someone\AppData\Local`},
			"canary", `C:\Users\someone\AppData\Local\Google\Chrome SxS\User Data`, "", `C:\Program Files\Google\Chrome SxS\Application\chrome.exe`},
		{"windows", `C:\Users\someone`, map[string]string{"LOCALAPPDATA": `C:\Users\someone\AppData\Local`},
			"beta", `C:\Users\someone\AppData\Local\Google\Chrome Beta\User Data`, "", `C:\Program Files\Google\Chrome Beta\Application\chrome.exe`},
		{"darwin", "/Users/someone", nil,
			"stable", "/Users/someone/Library/Application Support/Google/Chrome", "/Users/someone/Library/Application Support/Google/Chrome/NativeMessagingHosts", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"},
		{"darwin", "/Users/someone", nil,
			"canary", "/Users/someone/Library/Application Support/Google/Chrome Canary", "/Users/someone/Library/Application Support/Google/Chrome Canary/NativeMessagingHosts", "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"},
		{"linux", "/home/someone", nil,
			"stable", "/home/someone/.config/google-chrome", "/home/someone/.config/google-chrome/NativeMessagingHosts", "/opt/google/chrome/chrome"},
		{"linux", "/home/someone", nil,
			"dev", "/home/someone/.config/google-chrome-unstable", "/home/someone/.config/google-chrome-unstable/NativeMessagingHosts", "/opt/google/chrome-unstable/chrome"},
		// Chrome honours XDG_CONFIG_HOME, and so must the connector, or it lands in a folder this
		// Chrome never reads.
		{"linux", "/home/someone", map[string]string{"XDG_CONFIG_HOME": "/home/someone/cfg"},
			"stable", "/home/someone/cfg/google-chrome", "/home/someone/cfg/google-chrome/NativeMessagingHosts", "/opt/google/chrome/chrome"},
		// A relative value is ignored by Chrome and here alike.
		{"linux", "/home/someone", map[string]string{"XDG_CONFIG_HOME": "cfg"},
			"beta", "/home/someone/.config/google-chrome-beta", "/home/someone/.config/google-chrome-beta/NativeMessagingHosts", "/opt/google/chrome-beta/chrome"},
	}
	for _, c := range cases {
		p, ok := placesByChannel(chromePlacesFor(c.goos, c.home, envOf(c.env)))[c.channel]
		if !ok {
			t.Fatalf("%s has no %s channel", c.goos, c.channel)
		}
		if p.UserData != c.userData || p.HostsDir != c.hostsDir {
			t.Errorf("%s %s: user data %q hosts %q", c.goos, c.channel, p.UserData, p.HostsDir)
		}
		if !slices.Contains(p.Executables, c.executable) {
			t.Errorf("%s %s: %q not among %v", c.goos, c.channel, c.executable, p.Executables)
		}
	}
	if chromeHostsKey != `Software\Google\Chrome\NativeMessagingHosts` {
		t.Fatalf("the Windows hosts key is %q", chromeHostsKey)
	}
}

// fakeRegistry is the registry as a map from a key's path (case-folded, as Windows compares them)
// to its values by name; "" is the default value.
type fakeRegistry struct{ keys map[string]map[string]any }

func newFakeRegistry() *fakeRegistry { return &fakeRegistry{keys: map[string]map[string]any{}} }

func regKeyPath(root regRoot, key string) string { return strings.ToLower(root.String() + `\` + key) }

func (f *fakeRegistry) put(root regRoot, key, name string, value any) {
	path := regKeyPath(root, key)
	if f.keys[path] == nil {
		f.keys[path] = map[string]any{}
	}
	f.keys[path][name] = value
}

func (f *fakeRegistry) String(root regRoot, key, name string) (string, bool) {
	s, ok := f.keys[regKeyPath(root, key)][name].(string)
	return s, ok
}

func (f *fakeRegistry) Values(root regRoot, key string) map[string]any {
	out := map[string]any{}
	for name, v := range f.keys[regKeyPath(root, key)] {
		if name != "" {
			out[name] = v
		}
	}
	return out
}

func (f *fakeRegistry) SetDefault(key, value string) error {
	f.put(regCurrentUser, key, "", value)
	return nil
}

func (f *fakeRegistry) DeleteKey(key string) error {
	path := regKeyPath(regCurrentUser, key)
	for other := range f.keys {
		if strings.HasPrefix(other, path+`\`) {
			return errors.New("the key has subkeys")
		}
	}
	delete(f.keys, path)
	return nil
}

func writeLocalState(t *testing.T, udd string, body string) {
	t.Helper()
	if err := os.MkdirAll(udd, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(udd, "Local State"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

const twoProfiles = `{"profile":{"last_used":"Profile 1","info_cache":{"Default":{"name":"Personal"},"Profile 1":{"name":"Work"}}}}`

// Linux: Google's own package is found with its profiles and its running state; a Flatpak Chrome is
// listed as unsupported with the reason, not left out.
func TestChromeIsDetectedOnLinuxWithItsProfiles(t *testing.T) {
	home := t.TempDir()
	// The probe joins Linux paths with forward slashes on every system, so the expected one does too.
	udd := path.Join(home, ".config", "google-chrome")
	writeLocalState(t, udd, twoProfiles)
	if err := os.MkdirAll(filepath.Join(home, ".local/share/flatpak/app/com.google.Chrome"), 0o755); err != nil {
		t.Fatal(err)
	}
	pr := chromeProbe{
		goos: "linux", home: home, getenv: envOf(nil), exists: exists, readFile: os.ReadFile, readDir: os.ReadDir,
		lookPath: func(name string) (string, error) {
			if name == "google-chrome" {
				return "/usr/bin/google-chrome", nil
			}
			return "", errors.New("not found")
		},
		running: func(dir string) bool { return dir == udd },
	}
	found := detectChrome(pr)
	if len(found) != 2 {
		t.Fatalf("found %+v", found)
	}
	stable := found[0]
	if stable.Channel != "stable" || stable.Executable != "/usr/bin/google-chrome" || !stable.Running || !stable.Supported || stable.Packaging != "native" {
		t.Fatalf("stable: %+v", stable)
	}
	want := []ChromeProfile{{Dir: "Default", Name: "Personal"}, {Dir: "Profile 1", Name: "Work", LastUsed: true}}
	if !slices.Equal(stable.Profiles, want) {
		t.Fatalf("profiles %+v", stable.Profiles)
	}
	flatpak := found[1]
	if flatpak.Packaging != "flatpak" || flatpak.Supported || flatpak.Note == "" {
		t.Fatalf("flatpak: %+v", flatpak)
	}
}

// Windows: App Paths names where Chrome really is, which wins over a guessed folder; a channel with
// neither an executable nor profiles is not listed.
func TestChromeIsDetectedOnWindowsThroughAppPaths(t *testing.T) {
	reg := newFakeRegistry()
	exe := `D:\Apps\Google\Chrome\Application\chrome.exe`
	reg.put(regLocalMachine, appPathsKey, "", `"`+exe+`"`)
	udd := `C:\Users\someone\AppData\Local\Google\Chrome\User Data`
	files := map[string]string{exe: "", udd: "", udd + `\Local State`: twoProfiles}
	pr := chromeProbe{
		goos: "windows", home: `C:\Users\someone`,
		getenv: envOf(map[string]string{"LOCALAPPDATA": `C:\Users\someone\AppData\Local`}),
		exists: func(p string) bool { _, ok := files[p]; return ok },
		readFile: func(p string) ([]byte, error) {
			s, ok := files[p]
			if !ok {
				return nil, os.ErrNotExist
			}
			return []byte(s), nil
		},
		lookPath: func(string) (string, error) { return "", errors.New("no") },
		registry: reg,
	}
	found := detectChrome(pr)
	if len(found) != 1 || found[0].Executable != exe || found[0].UserData != udd || len(found[0].Profiles) != 2 {
		t.Fatalf("found %+v", found)
	}
}

func TestProfilesSurviveAForeignLocalState(t *testing.T) {
	read := func(body string) func(string) ([]byte, error) {
		return func(string) ([]byte, error) { return []byte(body), nil }
	}
	if got := readChromeProfiles(read("not json"), "x"); len(got) != 0 {
		t.Fatalf("garbage gave %v", got)
	}
	got := readChromeProfiles(read(`{"profile":{"info_cache":{"Profile 3":{}}}}`), "x")
	if len(got) != 1 || got[0].Name != "Profile 3" {
		t.Fatalf("an unnamed profile is named after its folder: %v", got)
	}
}

// The manifest is what Chrome checks: our name, stdio, an absolute path to the wrapper, and our
// extension as the only origin allowed to start it.
func TestTheHostManifestAllowsOnlyOurExtension(t *testing.T) {
	wrapper := filepath.Join(t.TempDir(), "browser-connector", "daedalus-bridge")
	var manifest struct {
		Name           string   `json:"name"`
		Description    string   `json:"description"`
		Path           string   `json:"path"`
		Type           string   `json:"type"`
		AllowedOrigins []string `json:"allowed_origins"`
	}
	if err := json.Unmarshal(hostManifest(wrapper), &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.Name != chromeHostName || manifest.Type != "stdio" || manifest.Description == "" {
		t.Fatalf("manifest %+v", manifest)
	}
	if manifest.Path != wrapper || !filepath.IsAbs(manifest.Path) {
		t.Fatalf("the path %q is not the absolute wrapper", manifest.Path)
	}
	if !slices.Equal(manifest.AllowedOrigins, []string{"chrome-extension://" + chromeExtensionID + "/"}) {
		t.Fatalf("allowed origins %v", manifest.AllowedOrigins)
	}
	for _, c := range chromeHostName {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '.' || c == '_') {
			t.Fatalf("Chrome refuses the host name %q", chromeHostName)
		}
	}
}

// The wrapper puts the run directory in front of what Chrome passes, quoted so a home folder with a
// space or a quote in it survives; on Windows a percent sign is doubled or cmd eats it.
func TestTheWrapperHandsTheBridgeItsRunDirectory(t *testing.T) {
	sh := string(bridgeWrapper("linux", "/opt/Dae dalus/browserd", "/home/o'neil/state/browserd-user/run"))
	want := `exec '/opt/Dae dalus/browserd' bridge --run-dir '/home/o'\''neil/state/browserd-user/run' "$@"`
	if !strings.HasPrefix(sh, "#!/bin/sh\n") || !strings.Contains(sh, want+"\n") {
		t.Fatalf("the shell wrapper:\n%s", sh)
	}
	bat := string(bridgeWrapper("windows", `C:\Users\someone\AppData\Local\Programs\Daedalus\browserd.exe`, `C:\Users\100%\State\browserd-user\run`))
	if !strings.Contains(bat, `"C:\Users\someone\AppData\Local\Programs\Daedalus\browserd.exe" bridge --run-dir "C:\Users\100%%\State\browserd-user\run" %*`+"\r\n") || !strings.HasPrefix(bat, "@echo off\r\n") {
		t.Fatalf("the batch wrapper:\n%s", bat)
	}
}

func linuxConnector(t *testing.T, home, local, browserd string) connector {
	t.Helper()
	pr := chromeProbe{goos: "linux", home: home, getenv: envOf(nil), exists: exists}
	return connector{
		goos: "linux", local: local, browserd: browserd, runDir: filepath.Join(local, "browserd-user", "run"),
		places:  chromePlacesFor("linux", home, envOf(nil)),
		present: func(cp chromePlaces) bool { return channelPresent(pr, cp) },
	}
}

// Linux and macOS: registered into the hosts folder of every channel this machine has, read back as
// registered, rewritten when the browserd it names has moved, and removed without touching the
// other programs' hosts beside it.
func TestTheConnectorIsRegisteredAndRemovedOnLinux(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the hosts folders are POSIX paths")
	}
	home, local := t.TempDir(), t.TempDir()
	beta := filepath.Join(home, ".config", "google-chrome-beta")
	if err := os.MkdirAll(beta, 0o755); err != nil {
		t.Fatal(err)
	}
	stableHosts := filepath.Join(home, ".config", "google-chrome", "NativeMessagingHosts")
	other := filepath.Join(stableHosts, "com.example.other.json")
	if err := os.MkdirAll(stableHosts, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(other, []byte(`{"name":"com.example.other"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	c := linuxConnector(t, home, local, "/opt/daedalus/browserd")
	if s := c.State(); s.Registered || s.Stale || !s.Available {
		t.Fatalf("before: %+v", s)
	}
	if err := c.Register(); err != nil {
		t.Fatal(err)
	}
	s := c.State()
	if !s.Registered || s.Stale {
		t.Fatalf("after registering: %+v", s)
	}
	for _, dir := range []string{stableHosts, filepath.Join(beta, "NativeMessagingHosts")} {
		if !fileHolds(filepath.Join(dir, chromeHostName+".json"), hostManifest(c.wrapperPath())) {
			t.Fatalf("no manifest in %s", dir)
		}
	}
	// Dev and canary are not on this machine: nothing is written for them.
	if exists(filepath.Join(home, ".config", "google-chrome-unstable")) {
		t.Fatal("a channel that is not installed got a folder")
	}
	info, err := os.Stat(c.wrapperPath())
	if err != nil || info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("the wrapper is not executable: %v %v", info, err)
	}

	// A new release with browserd elsewhere: the registration is stale, and the start rewrites it.
	moved := linuxConnector(t, home, local, "/opt/daedalus-2/browserd")
	if s := moved.State(); !s.Stale || s.Registered {
		t.Fatalf("a moved browserd: %+v", s)
	}
	if !moved.Owns() {
		t.Fatal("the registration of this installation's wrapper is not recognised as its own")
	}
	if rewritten, err := moved.Refresh(); err != nil || !rewritten {
		t.Fatalf("refresh: %v %v", rewritten, err)
	}
	if s := moved.State(); !s.Registered {
		t.Fatalf("after the refresh: %+v", s)
	}
	// Another installation's registration is not this one's to rewrite.
	elsewhere := linuxConnector(t, home, t.TempDir(), "/opt/daedalus-3/browserd")
	if elsewhere.Owns() {
		t.Fatal("another installation's registration reads as ours")
	}
	if rewritten, _ := elsewhere.Refresh(); rewritten {
		t.Fatal("a start rewrote another installation's registration")
	}
	// Refresh never registers what the operator did not.
	fresh := linuxConnector(t, t.TempDir(), t.TempDir(), "/opt/daedalus/browserd")
	if rewritten, _ := fresh.Refresh(); rewritten || exists(fresh.wrapperPath()) {
		t.Fatal("a start registered the connector by itself")
	}

	if err := moved.Unregister(); err != nil {
		t.Fatal(err)
	}
	if s := moved.State(); s.Registered || s.Stale {
		t.Fatalf("after removing: %+v", s)
	}
	if !exists(other) {
		t.Fatal("another program's host was removed with ours")
	}
	if exists(moved.wrapperPath()) || exists(filepath.Join(beta, "NativeMessagingHosts", chromeHostName+".json")) {
		t.Fatal("something of ours was left behind")
	}
}

// Windows: the manifest in the local state, the registry value under HKCU naming it, and on removal
// our key gone and another program's key beside it untouched.
func TestTheConnectorIsRegisteredAndRemovedOnWindows(t *testing.T) {
	local := t.TempDir()
	reg := newFakeRegistry()
	otherKey := chromeHostsKey + `\com.example.other`
	reg.put(regCurrentUser, otherKey, "", `C:\elsewhere\other.json`)
	c := connector{goos: "windows", local: local, browserd: `C:\Programs\Daedalus\browserd.exe`, runDir: `C:\State\browserd-user\run`, registry: reg,
		places: chromePlacesFor("windows", `C:\Users\someone`, envOf(nil))}
	if err := c.Register(); err != nil {
		t.Fatal(err)
	}
	value, ok := reg.String(regCurrentUser, chromeHostsKey+`\`+chromeHostName, "")
	if !ok || value != c.windowsManifestPath() {
		t.Fatalf("the registry names %q", value)
	}
	if !strings.HasSuffix(c.wrapperPath(), ".bat") || !fileHolds(c.windowsManifestPath(), hostManifest(c.wrapperPath())) {
		t.Fatal("the manifest does not name the batch wrapper")
	}
	s := c.State()
	if !s.Registered || len(s.Places) != 1 || s.Places[0].Channel != "all" {
		t.Fatalf("state %+v", s)
	}
	if !c.Owns() {
		t.Fatal("this installation's registration is not recognised as its own")
	}
	// A value pointing at another installation's manifest is stale, and not this one's to touch.
	reg.put(regCurrentUser, chromeHostsKey+`\`+chromeHostName, "", `C:\old\com.daedalus.browser.json`)
	if s := c.State(); !s.Stale || c.Owns() {
		t.Fatalf("a foreign value: %+v", s)
	}
	if err := c.Unregister(); err != nil {
		t.Fatal(err)
	}
	if _, ok := reg.String(regCurrentUser, chromeHostsKey+`\`+chromeHostName, ""); ok {
		t.Fatal("our key is still there")
	}
	if v, ok := reg.String(regCurrentUser, otherKey, ""); !ok || v != `C:\elsewhere\other.json` {
		t.Fatal("another program's key was touched")
	}
	if exists(c.wrapperPath()) || exists(c.windowsManifestPath()) {
		t.Fatal("our files were left behind")
	}
}

func TestNothingIsRegisteredWithoutABrowserd(t *testing.T) {
	c := connector{goos: "linux", local: t.TempDir()}
	if err := c.Register(); err == nil {
		t.Fatal("a build with no browserd registered a connector to nothing")
	}
	if s := c.State(); s.Available || s.Reason == "" {
		t.Fatalf("state %+v", s)
	}
}

func findingEffects(findings []PolicyFinding) []string {
	var out []string
	for _, f := range findings {
		out = append(out, f.Policy+"="+f.Effect)
	}
	sort.Strings(out)
	return out
}

// Policies are reported, from every place an administrator can set them.
func TestPoliciesInTheWayAreReported(t *testing.T) {
	reg := newFakeRegistry()
	reg.put(regLocalMachine, chromePolicyKey+`\ExtensionInstallBlocklist`, "1", "*")
	reg.put(regLocalMachine, chromePolicyKey, "DeveloperToolsAvailability", uint64(2))
	reg.put(regCurrentUser, chromePolicyKey+`\NativeMessagingBlocklist`, "1", "*")
	reg.put(regCurrentUser, chromePolicyKey+`\NativeMessagingAllowlist`, "1", chromeHostName)
	reg.put(regCurrentUser, chromePolicyKey, "NativeMessagingUserLevelHosts", uint64(0))
	got := findingEffects(chromePolicyFindings(chromeProbe{goos: "windows", registry: reg}))
	want := []string{"DeveloperToolsAvailability=debugger", "ExtensionInstallBlocklist=extension", "NativeMessagingUserLevelHosts=connector"}
	if !slices.Equal(got, want) {
		t.Fatalf("windows: %v", got)
	}

	// The allowlist exception, and ExtensionSettings as JSON text.
	allowed := evaluateChromePolicies("x", map[string]any{
		"ExtensionInstallBlocklist":      []any{"*"},
		"ExtensionInstallAllowlist":      []any{chromeExtensionID},
		"ExtensionDeveloperModeSettings": float64(1),
		"ExtensionSettings":              `{"*":{"installation_mode":"blocked","runtime_blocked_hosts":["*://bank.example"]}}`,
	})
	if got := findingEffects(allowed); !slices.Equal(got, []string{"ExtensionDeveloperModeSettings=developer_mode", "ExtensionSettings=extension", "ExtensionSettings=sites"}) {
		t.Fatalf("settings: %v", got)
	}

	dir := t.TempDir()
	files := map[string]string{
		"/etc/opt/chrome/policies/managed/a.json": `{"ExtensionInstallBlocklist":["` + chromeExtensionID + `"]}`,
		"/etc/opt/chrome/policies/managed/b.json": `not json`,
	}
	for name := range files {
		if err := os.WriteFile(filepath.Join(dir, filepath.Base(name)), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	linux := chromeProbe{goos: "linux", getenv: envOf(nil),
		readDir:  func(string) ([]os.DirEntry, error) { return os.ReadDir(dir) },
		readFile: func(p string) ([]byte, error) { return []byte(files[p]), nil },
	}
	if got := findingEffects(chromePolicyFindings(linux)); !slices.Equal(got, []string{"ExtensionInstallBlocklist=extension", "b.json=unknown"}) {
		t.Fatalf("linux: %v", got)
	}

	plist := `<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>DeveloperToolsAvailability</key><integer>2</integer>
<key>NativeMessagingUserLevelHosts</key><false/>
<key>ExtensionInstallBlocklist</key><array><string>*</string></array>
</dict></plist>`
	mac := chromeProbe{goos: "darwin", getenv: envOf(map[string]string{"USER": "someone"}),
		readFile: func(p string) ([]byte, error) {
			switch p {
			case "/Library/Managed Preferences/com.google.Chrome.plist":
				return []byte(plist), nil
			case "/Library/Managed Preferences/someone/com.google.Chrome.plist":
				return []byte("bplist00..."), nil
			}
			return nil, os.ErrNotExist
		}}
	got = findingEffects(chromePolicyFindings(mac))
	want = []string{"DeveloperToolsAvailability=debugger", "ExtensionInstallBlocklist=extension", "NativeMessagingUserLevelHosts=connector", "managed preferences=unknown"}
	if !slices.Equal(got, want) {
		t.Fatalf("mac: %v", got)
	}
	if got := chromePolicyFindings(chromeProbe{goos: "windows", registry: newFakeRegistry()}); len(got) != 0 {
		t.Fatalf("no policies: %v", got)
	}
}

// The id Chrome derives from the manifest key is the one the connector allows.
func TestTheExtensionKeyGivesThePinnedID(t *testing.T) {
	id, err := extensionIDFromKey(extensionPublicKey)
	if err != nil || id != chromeExtensionID {
		t.Fatalf("the key gives %q (%v), the connector allows %q", id, err, chromeExtensionID)
	}
	// The extension is built in its own repository (daedalus-chrome-extension), and the release zip
	// desktop/extension.lock pins carries this key in its manifest; the payload copy refuses any
	// other. This check once read the manifest from a folder beside this tree and skipped when it was
	// missing, so the values its src/protocol.ts declares are written out here instead, the same pair
	// the bridge's tests hold browserd to.
	if chromeExtensionID != "lkhhklicnjlplokpadpccndipomikoaj" || chromeHostName != "com.daedalus.browser" {
		t.Fatalf("the connector names %q for %q, the extension declares another pair", chromeHostName, chromeExtensionID)
	}
}

func writeExtension(t *testing.T, dir, version, key string, extra map[string]string) {
	t.Helper()
	files := map[string]string{
		"manifest.json":    `{"manifest_version":3,"name":"Daedalus","version":"` + version + `","key":"` + key + `"}`,
		"worker.js":        "// worker " + version,
		"popup/popup.html": "<p>" + version + "</p>",
	}
	var sums []string
	for name, body := range files {
		path := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256([]byte(body))
		sums = append(sums, hex.EncodeToString(sum[:])+"  "+name)
	}
	sort.Strings(sums)
	if err := os.WriteFile(filepath.Join(dir, extensionSumsName), []byte(strings.Join(sums, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for name, body := range extra {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// The release's extension is checked, copied into the folder Chrome knows it by, and refreshed in
// place on the next release; a damaged or foreign one is refused and the folder in place is kept.
func TestTheExtensionIsVerifiedAndCopiedIntoLocalState(t *testing.T) {
	root := t.TempDir()
	dst := filepath.Join(root, "local", extensionDirName)
	v1 := filepath.Join(root, "release-1", extensionDirName)
	writeExtension(t, v1, "1.0.0", extensionPublicKey, nil)
	state, err := installExtension(v1, dst)
	if err != nil || !state.Ready || state.Version != "1.0.0" || state.Path != dst {
		t.Fatalf("first copy: %+v %v", state, err)
	}
	v2 := filepath.Join(root, "release-2", extensionDirName)
	writeExtension(t, v2, "1.1.0", extensionPublicKey, nil)
	if state, err := installExtension(v2, dst); err != nil || state.Version != "1.1.0" {
		t.Fatalf("second copy: %+v %v", state, err)
	}
	if body, _ := os.ReadFile(filepath.Join(dst, "worker.js")); string(body) != "// worker 1.1.0" {
		t.Fatalf("not refreshed in place: %q", body)
	}

	bad := []struct {
		name  string
		build func(dir string)
	}{
		{"an unlisted file", func(dir string) { writeExtension(t, dir, "9", extensionPublicKey, map[string]string{"evil.js": "x"}) }},
		{"a changed file", func(dir string) {
			writeExtension(t, dir, "9", extensionPublicKey, nil)
			_ = os.WriteFile(filepath.Join(dir, "worker.js"), []byte("changed"), 0o644)
		}},
		{"another extension's key", func(dir string) { writeExtension(t, dir, "9", "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE", nil) }},
	}
	for i, b := range bad {
		dir := filepath.Join(root, "bad", string(rune('a'+i)), extensionDirName)
		b.build(dir)
		state, err := installExtension(dir, dst)
		if err == nil {
			t.Fatalf("%s was accepted", b.name)
		}
		if !state.Ready || state.Version != "1.1.0" {
			t.Fatalf("%s: the folder in place was not kept: %+v", b.name, state)
		}
	}
	for _, sums := range []string{"../etc/passwd", "/abs", "a/../b"} {
		if _, err := readExtensionSums([]byte(strings.Repeat("0", 64) + "  " + sums + "\n")); err == nil {
			t.Fatalf("a list naming %q was accepted", sums)
		}
	}
}

// The user daemon: its own run and state directories in the sealed local state, the attach backend,
// and the host told where it is.
func TestTheUserBrowserDaemonRunsBesideTheHostOne(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{browserdUserRunDir(paths), browserdUserStateDir(paths)} {
		if !within(dir, paths.Local) || within(dir, paths.Data) || dir == browserdRunDir(paths) || dir == browserdStateDir(paths) {
			t.Fatalf("%s is not a folder of its own in the local state", dir)
		}
	}
	argv := browserdUserArgv("/opt/browserd", paths)
	want := []string{"/opt/browserd", "serve", "--env", "user", "--backend", "attach", "--run-dir", browserdUserRunDir(paths), "--state-dir", browserdUserStateDir(paths), "--listen", browserdListen(browserdUserRunDir(paths))}
	if !slices.Equal(argv, want) {
		t.Fatalf("argv %v", argv)
	}
	env := envMap(supervisorEnv(paths, nil, map[string]string{}))
	if env["BROWSER_USER_DIR"] != browserdUserRunDir(paths) {
		t.Fatalf("BROWSER_USER_DIR = %q", env["BROWSER_USER_DIR"])
	}
	// The connector's wrapper hands the bridge the same directory.
	c := connector{goos: "linux", browserd: "/opt/browserd", runDir: browserdUserRunDir(paths)}
	if !strings.Contains(string(bridgeWrapper(c.goos, c.browserd, c.runDir)), browserdUserRunDir(paths)) {
		t.Fatal("the bridge is not told the user daemon's run directory")
	}
}

// The report the app reads is behind the token even though it only reads: profile names are the
// operator's, and every page they open can reach the loopback port.
func TestTheUserBrowserReportIsBehindTheToken(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{}, ModeNative); err != nil {
		t.Fatal(err)
	}
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	get := func(token string) (int, map[string]any) {
		req, _ := http.NewRequest(http.MethodGet, server.URL()+"api/browser/user", nil)
		if token != "" {
			req.Header.Set(csrfHeader, token)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		var out map[string]any
		_ = json.Unmarshal(body, &out)
		return resp.StatusCode, out
	}
	if code, _ := get(""); code != http.StatusForbidden {
		t.Fatalf("without the token: %d", code)
	}
	code, body := get(server.csrf)
	if code != http.StatusOK || body["host_name"] != chromeHostName || body["extension_id"] != chromeExtensionID || body["user_dir"] != browserdUserRunDir(paths) {
		t.Fatalf("with the token: %d %v", code, body)
	}
	for _, key := range []string{"browsers", "policies", "connector", "extension"} {
		if _, ok := body[key]; !ok {
			t.Fatalf("the report has no %q: %v", key, body)
		}
	}
	if got := post(t, server.URL()+"api/browser/user/register", nil, nil); got != http.StatusForbidden {
		t.Fatalf("register without the token: %d", got)
	}
}
