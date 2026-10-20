package main

// The connector: the native-messaging host registration through which Chrome starts
// `browserd bridge` when the extension calls chrome.runtime.connectNative. It is two things on
// disk — a manifest Chrome reads, and a small wrapper the manifest names — plus, on Windows, the
// registry value that tells Chrome where the manifest is.
//
// Why a wrapper and not browserd itself: Chrome runs the manifest's path with the caller's origin as
// the only argument (and on Windows a --parent-window after it), so nothing in the manifest can tell
// the bridge which daemon to reach. The wrapper adds `bridge --run-dir <user daemon's run dir>` in
// front of what Chrome passes. On Windows it is a batch file: Chrome starts every native host
// through `cmd.exe /d /s /c` with the window hidden, so a batch file flashes no console, and neither
// does the console program it runs, which inherits that hidden console.
//
// The manifest allows exactly one origin, the pinned extension's. Removing the connector removes
// our manifest, our wrapper and our registry key, and nothing beside them: other programs' hosts
// live in the same folder and under the same key.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// connectorDirName is the folder in the local state that holds the wrapper, and on Windows the
// manifest the registry points at.
const connectorDirName = "browser-connector"

// ConnectorPlace is one place Chrome reads the registration from, and what is there.
type ConnectorPlace struct {
	Channel string `json:"channel"` // "all" on Windows, where every channel reads the one key
	Where   string `json:"where"`
	State   string `json:"state"` // registered, stale, absent
}

// ConnectorState is the registration as a whole.
type ConnectorState struct {
	// Registered is true when at least one place holds our current manifest and none holds an
	// outdated one: the button then reads "registered", and Chrome finds the bridge we would write.
	Registered bool `json:"registered"`
	// Stale is true when a place holds a manifest of ours that names another wrapper or origin —
	// an upgrade moved browserd, or someone edited it. The launcher rewrites it on its next start.
	Stale bool `json:"stale"`
	// Available is false when this build carries no browserd: there is nothing to register.
	Available bool             `json:"available"`
	Reason    string           `json:"reason,omitempty"`
	Wrapper   string           `json:"wrapper"`
	Places    []ConnectorPlace `json:"places"`
	// RestartChrome says Chrome must be restarted for a new registration to be seen: it reads host
	// manifests when it starts, so a Chrome already running does not find a host registered after.
	RestartChrome bool `json:"restart_chrome,omitempty"`
}

// connector writes, reads and removes the registration on one operating system.
type connector struct {
	goos     string
	local    string // the installation's local state folder
	browserd string // the browserd executable the wrapper runs; "" when the build has none
	runDir   string // the user daemon's run directory, handed to the bridge
	places   []chromePlaces
	// present reports whether a channel is on this machine, so a registration is written into the
	// folders of the channels the operator has and not into every one Chrome could ever have.
	present  func(chromePlaces) bool
	registry chromeRegistry
}

func (c connector) dir() string { return filepath.Join(c.local, connectorDirName) }

func (c connector) wrapperPath() string {
	if c.goos == "windows" {
		return filepath.Join(c.dir(), "daedalus-bridge.bat")
	}
	return filepath.Join(c.dir(), "daedalus-bridge")
}

// windowsManifestPath is where the manifest lives on Windows, where Chrome finds it through the
// registry and the file itself may be anywhere.
func (c connector) windowsManifestPath() string {
	return filepath.Join(c.dir(), chromeHostName+".json")
}

func (c connector) registryKey() string { return chromeHostsKey + `\` + chromeHostName }

// hostManifest is the manifest Chrome reads, exactly. Its path is absolute, as Chrome requires on
// macOS and Linux and as a registry-located manifest on Windows needs to stay unambiguous.
func hostManifest(wrapper string) []byte {
	body, _ := json.MarshalIndent(map[string]any{
		"name":            chromeHostName,
		"description":     "Daedalus: connects the Daedalus extension to Daedalus on this computer",
		"path":            wrapper,
		"type":            "stdio",
		"allowed_origins": []string{chromeOrigin()},
	}, "", "  ")
	return append(body, '\n')
}

// bridgeWrapper is the wrapper's text for goos.
func bridgeWrapper(goos, browserd, runDir string) []byte {
	if goos == "windows" {
		// A percent sign in a batch file starts a variable; doubled it is a literal one. Inside
		// double quotes nothing else in a Windows path is special, and a path cannot hold a quote.
		esc := func(s string) string { return strings.ReplaceAll(s, "%", "%%") }
		return []byte("@echo off\r\nrem Written by the Daedalus launcher: Chrome starts this to reach Daedalus on this computer.\r\n\"" +
			esc(browserd) + "\" bridge " + bridgeRunDirFlag + " \"" + esc(runDir) + "\" %*\r\n")
	}
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }
	return []byte("#!/bin/sh\n# Written by the Daedalus launcher: Chrome starts this to reach Daedalus on this computer.\nexec " +
		quote(browserd) + " bridge " + bridgeRunDirFlag + " " + quote(runDir) + " \"$@\"\n")
}

// targets are the places a registration is written to. On Windows that is the one registry key.
// Elsewhere it is the hosts folder of every channel this machine has, and stable's in any case:
// the operator may install Chrome after pressing the button, and stable is what they would install.
func (c connector) targets() []chromePlaces {
	if c.goos == "windows" {
		return nil
	}
	var out []chromePlaces
	for _, p := range c.places {
		if p.HostsDir == "" {
			continue
		}
		if p.Channel.ID == "stable" || (c.present != nil && c.present(p)) {
			out = append(out, p)
		}
	}
	return out
}

// State reads what is registered without changing anything.
func (c connector) State() ConnectorState {
	state := ConnectorState{Available: c.browserd != "", Wrapper: c.wrapperPath(), Places: []ConnectorPlace{}}
	if !state.Available {
		state.Reason = noBrowserdReason
	}
	want := hostManifest(c.wrapperPath())
	wrapperOK := c.browserd != "" && fileHolds(c.wrapperPath(), bridgeWrapper(c.goos, c.browserd, c.runDir))
	judge := func(manifest string) string {
		body, err := os.ReadFile(manifest)
		if err != nil {
			return "absent"
		}
		if string(body) == string(want) && wrapperOK {
			return "registered"
		}
		return "stale"
	}
	if c.goos == "windows" {
		where := regCurrentUser.String() + `\` + c.registryKey()
		place := ConnectorPlace{Channel: "all", Where: where, State: "absent"}
		if c.registry != nil {
			if value, ok := c.registry.String(regCurrentUser, c.registryKey(), ""); ok {
				place.State = "stale"
				if strings.EqualFold(value, c.windowsManifestPath()) {
					place.State = judge(value)
					if place.State == "absent" {
						place.State = "stale"
					}
				}
			}
		}
		state.Places = append(state.Places, place)
	} else {
		for _, p := range c.places {
			if p.HostsDir == "" {
				continue
			}
			manifest := filepath.Join(filepath.FromSlash(p.HostsDir), chromeHostName+".json")
			s := judge(manifest)
			if s == "absent" && !c.isTarget(p) {
				continue
			}
			state.Places = append(state.Places, ConnectorPlace{Channel: p.Channel.ID, Where: manifest, State: s})
		}
	}
	registered := false
	for _, p := range state.Places {
		switch p.State {
		case "registered":
			registered = true
		case "stale":
			state.Stale = true
		}
	}
	state.Registered = registered && !state.Stale
	return state
}

func (c connector) isTarget(p chromePlaces) bool {
	for _, t := range c.targets() {
		if t.Channel.ID == p.Channel.ID {
			return true
		}
	}
	return false
}

// channelState is one channel's share of the state, for the install list.
func (s ConnectorState) channelState(channel string) string {
	for _, p := range s.Places {
		if p.Channel == channel || p.Channel == "all" {
			return p.State
		}
	}
	return "absent"
}

// Register writes the wrapper, the manifest and, on Windows, the registry value. Writing it again
// over an existing registration is how a stale one is brought up to date.
func (c connector) Register() error {
	if c.browserd == "" {
		return errors.New(noBrowserdReason)
	}
	if err := os.MkdirAll(c.dir(), 0o700); err != nil {
		return err
	}
	if err := writeFileMode(c.wrapperPath(), bridgeWrapper(c.goos, c.browserd, c.runDir), 0o755); err != nil {
		return fmt.Errorf("writing the connector's wrapper: %w", err)
	}
	manifest := hostManifest(c.wrapperPath())
	if c.goos == "windows" {
		if c.registry == nil {
			return errors.New("the registry is not available")
		}
		if err := writeFileMode(c.windowsManifestPath(), manifest, 0o644); err != nil {
			return fmt.Errorf("writing the connector's manifest: %w", err)
		}
		if err := c.registry.SetDefault(c.registryKey(), c.windowsManifestPath()); err != nil {
			return fmt.Errorf("registering the connector with Chrome: %w", err)
		}
		return nil
	}
	for _, p := range c.places {
		if p.HostsDir == "" {
			continue
		}
		dir := filepath.FromSlash(p.HostsDir)
		file := filepath.Join(dir, chromeHostName+".json")
		// A channel's folder that already holds ours is rewritten too, or a manifest left by an
		// earlier release in a channel since removed would read as stale for ever.
		if !c.isTarget(p) && !exists(file) {
			continue
		}
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("registering the connector with %s: %w", p.Channel.Name, err)
		}
		if err := writeFileMode(file, manifest, 0o644); err != nil {
			return fmt.Errorf("registering the connector with %s: %w", p.Channel.Name, err)
		}
	}
	return nil
}

// Unregister removes our manifest from every place Chrome reads one, our registry key, and the
// wrapper — and nothing else in those folders or under that key.
func (c connector) Unregister() error {
	var errs []error
	if c.goos == "windows" {
		if c.registry != nil {
			if err := c.registry.DeleteKey(c.registryKey()); err != nil {
				errs = append(errs, fmt.Errorf("removing the connector's registry key: %w", err))
			}
		}
		errs = append(errs, removeIfThere(c.windowsManifestPath()))
	} else {
		for _, p := range c.places {
			if p.HostsDir == "" {
				continue
			}
			errs = append(errs, removeIfThere(filepath.Join(filepath.FromSlash(p.HostsDir), chromeHostName+".json")))
		}
	}
	errs = append(errs, removeIfThere(c.wrapperPath()))
	// The folder only if it is empty now: it is ours, but a removal that deletes a folder whole is
	// the one that takes something along it did not mean to.
	_ = os.Remove(c.dir())
	return errors.Join(errs...)
}

// Refresh rewrites a registration that exists, so the manifest always names the browserd of the
// release that is running. It never creates one: registering is the operator's decision.
func (c connector) Refresh() (bool, error) {
	if c.browserd == "" || !c.State().Stale || !c.Owns() {
		return false, nil
	}
	return true, c.Register()
}

// Owns reports whether a registration on this machine is this installation's: a manifest naming
// this installation's wrapper, or on Windows the registry naming this installation's manifest.
// Only such a registration is rewritten by a start or removed by an uninstall — another
// installation's, or one a test's temporary installation finds on the developer's own machine,
// is left to whoever made it.
func (c connector) Owns() bool {
	if c.goos == "windows" {
		if c.registry == nil {
			return false
		}
		value, ok := c.registry.String(regCurrentUser, c.registryKey(), "")
		return ok && strings.EqualFold(value, c.windowsManifestPath())
	}
	for _, p := range c.places {
		if p.HostsDir == "" {
			continue
		}
		body, err := os.ReadFile(filepath.Join(filepath.FromSlash(p.HostsDir), chromeHostName+".json"))
		if err != nil {
			continue
		}
		var manifest struct {
			Path string `json:"path"`
		}
		if json.Unmarshal(body, &manifest) == nil && manifest.Path == c.wrapperPath() {
			return true
		}
	}
	return false
}

func removeIfThere(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func fileHolds(path string, want []byte) bool {
	body, err := os.ReadFile(path)
	return err == nil && string(body) == string(want)
}

// writeFileMode writes beside the target and renames, so Chrome starting a host in the same
// instant reads the old manifest or the new one and never half of one.
func writeFileMode(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".daedalus-connector-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	_, werr := tmp.Write(data)
	cerr := tmp.Close()
	if err := errors.Join(werr, cerr); err != nil {
		os.Remove(name)
		return err
	}
	if err := os.Chmod(name, mode); err != nil {
		os.Remove(name)
		return err
	}
	if err := os.Rename(name, path); err != nil {
		os.Remove(name)
		return err
	}
	return nil
}
