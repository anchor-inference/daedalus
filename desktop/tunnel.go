package main

// The public address of a native installation: which tunnel, whether it is on, and the process holding it.
//
// The operator opens one from the app's settings, and only from there: the agent may not raise a tunnel,
// and the host's rule says so. The request reaches this file over the launcher's bridge. What the
// operator chose is kept here, in the local state folder the agent's policy seals whole, and a token goes
// in through the bridge once and never comes back: nothing here returns one, logs one or puts one in an
// environment or on a command line. tunneld reads it from a file of 0600.
//
// tunneld is started only while the address is on, it stands apart from the rest (a missing tunneld takes
// the address away and nothing else), and it holds a pipe from this process, so a launcher that dies
// closes the address. The status comes from its file while it runs, and is not believed once it has died.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	tunnelNgrok      = "ngrok"
	tunnelTailscale  = "tailscale"
	tunnelCloudflare = "cloudflare"
	tunnelFrp        = "frp"
)

// tunnelChoice is what the operator chose, in tunnel.json. Off until they say otherwise.
type tunnelChoice struct {
	Enabled   bool   `json:"enabled"`
	Provider  string `json:"provider,omitempty"`
	FrpServer string `json:"frp_server,omitempty"`
	FrpDomain string `json:"frp_domain,omitempty"`
}

// tunnelSecrets is the file tunneld reads, and only tunneld and this file ever read it.
type tunnelSecrets struct {
	NgrokToken string `json:"ngrok_token,omitempty"`
	FrpToken   string `json:"frp_token,omitempty"`
}

// tunnelChange is one request from the settings. A nil field leaves that setting as it is.
type tunnelChange struct {
	Enabled    *bool   `json:"enabled"`
	Provider   *string `json:"provider"`
	NgrokToken *string `json:"ngrok_token"`
	FrpToken   *string `json:"frp_token"`
	FrpServer  *string `json:"frp_server"`
	FrpDomain  *string `json:"frp_domain"`
	// SignOut forgets the Tailscale node, so the next start asks for a sign-in again.
	SignOut bool `json:"sign_out"`
}

// tunnelStatus is tunneld's status.json, the fields read here.
type tunnelStatus struct {
	State     string    `json:"state"`
	Address   string    `json:"address"`
	SignIn    string    `json:"sign_in"`
	Code      string    `json:"code"`
	Detail    string    `json:"detail"`
	Confirmed bool      `json:"confirmed"`
	Since     time.Time `json:"since"`
	PIDs      []int     `json:"pids"`
	Wakes     int       `json:"wakes"`
}

// tunnelProviderView is what the settings need of one provider. It says whether a token is kept, and
// nothing about the token.
type tunnelProviderView struct {
	Client   bool   `json:"client"`
	Token    bool   `json:"token"`
	SignedIn bool   `json:"signed_in,omitempty"`
	Server   string `json:"server,omitempty"`
	Domain   string `json:"domain,omitempty"`
}

// tunnelView is the answer to GET /api/tunnel.
type tunnelView struct {
	// Tunneld says this build carries the program at all.
	Tunneld   bool                          `json:"tunneld"`
	Enabled   bool                          `json:"enabled"`
	Provider  string                        `json:"provider"`
	State     string                        `json:"state"`
	Address   string                        `json:"address"`
	Confirmed bool                          `json:"confirmed"`
	SignIn    string                        `json:"sign_in,omitempty"`
	Code      string                        `json:"code,omitempty"`
	Detail    string                        `json:"detail,omitempty"`
	Since     *time.Time                    `json:"since,omitempty"`
	PIDs      []int                         `json:"pids"`
	Wakes     int                           `json:"wakes"`
	Providers map[string]tunnelProviderView `json:"providers"`
}

func tunnelDir(p Paths) string      { return filepath.Join(p.Local, "tunnel") }
func tunnelToolsDir(p Paths) string { return filepath.Join(tunnelDir(p), "bin") }
func tunnelChoicePath(p Paths) string {
	return filepath.Join(p.Local, "tunnel.json")
}

func readTunnelChoice(p Paths) tunnelChoice {
	choice := tunnelChoice{Provider: tunnelCloudflare}
	if body, err := os.ReadFile(tunnelChoicePath(p)); err == nil {
		_ = json.Unmarshal(body, &choice)
	}
	return choice
}

func writeTunnelJSON(path string, value any) error {
	body, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	scratch := path + ".tmp"
	if err := os.WriteFile(scratch, body, 0o600); err != nil {
		return err
	}
	return os.Rename(scratch, path)
}

func readTunnelSecrets(p Paths) tunnelSecrets {
	var secrets tunnelSecrets
	if body, err := os.ReadFile(filepath.Join(tunnelDir(p), "secrets.json")); err == nil {
		_ = json.Unmarshal(body, &secrets)
	}
	return secrets
}

// tunneldBinary is the program this launcher runs: DAEDALUS_TUNNELD when it is set, else the one beside
// the launcher. "" when this build has none: a source build, or one older than the address.
func tunneldBinary(exe string, getenv func(string) string, exists func(string) bool) string {
	if p := getenv("DAEDALUS_TUNNELD"); p != "" {
		return p
	}
	if exe == "" {
		return ""
	}
	name := "tunneld"
	if runtime.GOOS == "windows" {
		name = "tunneld.exe"
	}
	if p := filepath.Join(filepath.Dir(exe), name); exists(p) {
		return p
	}
	return ""
}

// ngrokPlaces are where an install ends up that a launcher's PATH often misses: a GUI app on a Mac starts
// with a bare PATH, and neither Homebrew's folder nor winget's is on it.
func ngrokPlaces(goos string, getenv func(string) string) []string {
	switch goos {
	case "darwin":
		return []string{"/opt/homebrew/bin/ngrok", "/usr/local/bin/ngrok"}
	case "windows":
		if local := getenv("LOCALAPPDATA"); local != "" {
			return []string{filepath.Join(local, "Microsoft", "WinGet", "Links", "ngrok.exe"), filepath.Join(local, "ngrok", "ngrok.exe")}
		}
		return nil
	}
	return []string{"/usr/local/bin/ngrok", "/snap/bin/ngrok"}
}

func exeName(name string) string {
	if runtime.GOOS == "windows" {
		return name + ".exe"
	}
	return name
}

// tunnelClient is the program a provider runs: the operator's own if they have one, then the copy
// fetched for them. "" when neither exists yet.
func tunnelClient(p Paths, provider string) string {
	program := map[string]string{tunnelNgrok: "ngrok", tunnelCloudflare: "cloudflared", tunnelFrp: "frpc"}[provider]
	if program == "" {
		return ""
	}
	if found, err := exec.LookPath(program); err == nil {
		return found
	}
	if provider == tunnelNgrok {
		for _, place := range ngrokPlaces(runtime.GOOS, os.Getenv) {
			if exists(place) {
				return place
			}
		}
		return ""
	}
	if fetched := filepath.Join(tunnelToolsDir(p), exeName(program)); exists(fetched) {
		return fetched
	}
	return ""
}

func tunnelDownloadFor(provider string) (download, bool) {
	var table map[string]download
	switch provider {
	case tunnelCloudflare:
		table = cloudflaredDownloads
	case tunnelFrp:
		table = frpDownloads
	default:
		return download{}, false
	}
	d, err := pick(table, runtime.GOOS, runtime.GOARCH)
	return d, err == nil
}

// tunnelEnv is tunneld's whole environment, built from nothing like the guest door's: nothing it runs
// has any use for what the operator's shell holds.
func tunnelEnv() []string {
	env := []string{"TS_NO_LOGS_NO_SUPPORT=true"}
	for _, key := range []string{"PATH", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "SYSTEMROOT", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} {
		if value := os.Getenv(key); value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}

// tunnelConfig is config.json for tunneld.
type tunnelConfig struct {
	Provider     string `json:"provider"`
	DoorPort     string `json:"door_port"`
	ServicesPort string `json:"services_port"`
	Binary       string `json:"binary,omitempty"`
	FrpServer    string `json:"frp_server,omitempty"`
	FrpDomain    string `json:"frp_domain,omitempty"`
}

// tunnels owns the choice and the process. One per launcher.
type tunnels struct {
	paths Paths
	log   func(string, ...any)
	// find and fetch are variables so the tests can run with no tunneld and no network.
	find  func() string
	fetch func(ctx context.Context, d download) error

	mu         sync.Mutex
	ctx        context.Context
	process    *Process
	generation int
	installing bool
	failure    string
}

func newTunnels(p Paths, log func(string, ...any)) *tunnels {
	t := &tunnels{paths: p, log: log}
	t.find = func() string {
		exe, _ := os.Executable()
		return tunneldBinary(exe, os.Getenv, exists)
	}
	t.fetch = func(ctx context.Context, d download) error {
		_, err := installTool(ctx, p, d, tunnelToolsDir(p), log)
		return err
	}
	return t
}

// Start brings the address back if the operator left it on. It does not wait for anything: an address
// that cannot come up is a state the settings show, not a reason for a start to fail.
func (t *tunnels) Start(ctx context.Context) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.ctx = ctx
	if readTunnelChoice(t.paths).Enabled && t.process == nil {
		t.raise()
	}
}

// Stop closes the address. The first thing a stopping installation does, so that nothing outside is told
// a door is there while the doors behind it are going.
func (t *tunnels) Stop(ctx context.Context) {
	t.mu.Lock()
	process := t.process
	t.process = nil
	t.generation++
	t.installing = false
	t.mu.Unlock()
	if process != nil {
		process.Stop(ctx)
	}
	_ = os.Remove(filepath.Join(tunnelDir(t.paths), "status.json"))
}

// raise starts tunneld, in the background because a client may have to be fetched first. Called with the
// lock held.
func (t *tunnels) raise() {
	t.generation++
	generation := t.generation
	ctx := t.ctx
	if ctx == nil {
		ctx = context.Background()
	}
	t.failure = ""
	choice := readTunnelChoice(t.paths)
	binary := t.find()
	if binary == "" {
		t.failure = "no_tunneld"
		return
	}
	t.installing = choice.Provider != tunnelTailscale && choice.Provider != tunnelNgrok && tunnelClient(t.paths, choice.Provider) == ""
	go func() {
		if err := t.ensureClient(ctx, choice.Provider); err != nil {
			t.log("the tunnel's client could not be fetched: %v", err)
			t.mu.Lock()
			if generation == t.generation {
				t.installing, t.failure = false, "download_failed"
			}
			t.mu.Unlock()
			return
		}
		t.mu.Lock()
		defer t.mu.Unlock()
		if generation != t.generation {
			return
		}
		t.installing = false
		if err := t.startProcess(ctx, binary, choice); err != nil {
			t.log("the tunnel could not be started: %v", err)
			t.failure = "other"
		}
	}()
}

func (t *tunnels) ensureClient(ctx context.Context, provider string) error {
	if provider == tunnelTailscale || provider == tunnelNgrok || tunnelClient(t.paths, provider) != "" {
		return nil
	}
	d, ok := tunnelDownloadFor(provider)
	if !ok {
		return fmt.Errorf("no pinned build of %s for this machine", provider)
	}
	if err := t.fetch(ctx, d); err != nil {
		return err
	}
	return nil
}

func (t *tunnels) startProcess(ctx context.Context, binary string, choice tunnelChoice) error {
	settings := readEnv(readFile(t.paths.Env))
	dir := tunnelDir(t.paths)
	config := tunnelConfig{
		Provider:     choice.Provider,
		DoorPort:     parsePort(settings["GUEST_DOOR_PORT"], portDefaults["GUEST_DOOR_PORT"]),
		ServicesPort: parsePort(settings["SERVICES_DOOR_PORT"], portDefaults["SERVICES_DOOR_PORT"]),
		Binary:       tunnelClient(t.paths, choice.Provider),
		FrpServer:    choice.FrpServer,
		FrpDomain:    choice.FrpDomain,
	}
	if err := writeTunnelJSON(filepath.Join(dir, "config.json"), config); err != nil {
		return err
	}
	_ = os.Remove(filepath.Join(dir, "status.json"))
	t.process = &Process{
		Name:      "tunnel",
		Argv:      []string{binary, "serve", dir},
		Dir:       dir,
		Env:       tunnelEnv(),
		LogPath:   filepath.Join(t.paths.RuntimeLogs, "tunneld.log"),
		Log:       t.log,
		PidFile:   filepath.Join(pidsDir(t.paths), "tunneld.json"),
		HoldStdin: true,
	}
	t.process.Start(ctx)
	return nil
}

var (
	tunnelToken  = regexp.MustCompile(`^[\x21-\x7e]{1,512}$`)
	tunnelDomain = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$`)
)

func (c tunnelChange) check() error {
	if c.Provider != nil {
		switch *c.Provider {
		case tunnelNgrok, tunnelTailscale, tunnelCloudflare, tunnelFrp:
		default:
			return errors.New("no such provider")
		}
	}
	for _, token := range []*string{c.NgrokToken, c.FrpToken} {
		if token != nil && *token != "" && !tunnelToken.MatchString(*token) {
			return errors.New("that does not look like a token")
		}
	}
	if c.FrpDomain != nil && *c.FrpDomain != "" && !tunnelDomain.MatchString(*c.FrpDomain) {
		return errors.New("that does not look like a domain")
	}
	if c.FrpServer != nil && *c.FrpServer != "" {
		host, port, err := net.SplitHostPort(*c.FrpServer)
		if err != nil {
			host, port = *c.FrpServer, "7000"
		}
		if !tunnelDomain.MatchString(host) || parsePort(port, "") == "" {
			return errors.New("the server is a host and a port, such as vps.example.com:7000")
		}
	}
	return nil
}

// Change applies one request from the settings and restarts the tunnel if anything it depends on moved.
func (t *tunnels) Change(ctx context.Context, change tunnelChange) error {
	if err := change.check(); err != nil {
		return err
	}
	t.Stop(ctx)
	t.mu.Lock()
	defer t.mu.Unlock()
	choice := readTunnelChoice(t.paths)
	secrets := readTunnelSecrets(t.paths)
	if change.Provider != nil {
		choice.Provider = *change.Provider
	}
	if change.Enabled != nil {
		choice.Enabled = *change.Enabled
	}
	if change.FrpServer != nil {
		choice.FrpServer = strings.TrimSpace(*change.FrpServer)
	}
	if change.FrpDomain != nil {
		choice.FrpDomain = strings.TrimSpace(*change.FrpDomain)
	}
	if change.NgrokToken != nil {
		secrets.NgrokToken = *change.NgrokToken
	}
	if change.FrpToken != nil {
		secrets.FrpToken = *change.FrpToken
	}
	if change.SignOut {
		_ = os.RemoveAll(filepath.Join(tunnelDir(t.paths), "tailscale"))
	}
	if err := writeTunnelJSON(filepath.Join(tunnelDir(t.paths), "secrets.json"), secrets); err != nil {
		return err
	}
	if err := writeTunnelJSON(tunnelChoicePath(t.paths), choice); err != nil {
		return err
	}
	t.failure = ""
	if choice.Enabled {
		t.raise()
	}
	return nil
}

// View is the truth as the launcher knows it. What tunneld wrote counts only while tunneld runs.
func (t *tunnels) View() tunnelView {
	choice := readTunnelChoice(t.paths)
	secrets := readTunnelSecrets(t.paths)
	view := tunnelView{
		Tunneld: t.find() != "", Enabled: choice.Enabled, Provider: choice.Provider, State: "off", PIDs: []int{},
		Providers: map[string]tunnelProviderView{
			tunnelNgrok:      {Client: tunnelClient(t.paths, tunnelNgrok) != "", Token: secrets.NgrokToken != ""},
			tunnelTailscale:  {Client: true, SignedIn: exists(filepath.Join(tunnelDir(t.paths), "tailscale", "tailscaled.state"))},
			tunnelCloudflare: {Client: tunnelClient(t.paths, tunnelCloudflare) != ""},
			tunnelFrp:        {Client: tunnelClient(t.paths, tunnelFrp) != "", Token: secrets.FrpToken != "", Server: choice.FrpServer, Domain: choice.FrpDomain},
		},
	}
	if !choice.Enabled {
		return view
	}
	t.mu.Lock()
	process, installing, failure := t.process, t.installing, t.failure
	t.mu.Unlock()
	switch {
	case installing:
		view.State = "installing"
		return view
	case failure != "":
		view.State, view.Code = "failed", failure
		return view
	case process == nil:
		view.State = "starting"
		return view
	}
	if !process.Running() {
		view.State = "reconnecting"
		return view
	}
	var status tunnelStatus
	body, err := os.ReadFile(filepath.Join(tunnelDir(t.paths), "status.json"))
	if err != nil || json.Unmarshal(body, &status) != nil {
		view.State = "starting"
		return view
	}
	view.State, view.Address, view.Confirmed, view.SignIn, view.Code, view.Detail, view.Wakes = status.State, status.Address, status.Confirmed, status.SignIn, status.Code, status.Detail, status.Wakes
	view.PIDs = status.PIDs
	if !status.Since.IsZero() {
		view.Since = &status.Since
	}
	return view
}

// handleTunnel is the app's way to the address, over the bridge. Both methods carry the token: the answer
// names what the operator has exposed.
func (s *Server) handleTunnel(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET, POST")
		writeComputerError(w, http.StatusMethodNotAllowed, "this route answers GET and POST")
		return
	}
	if !s.computerGuard(w, r, r.Method) {
		return
	}
	address := s.app.native.tunnels()
	if r.Method == http.MethodGet {
		writeComputerJSON(w, http.StatusOK, address.View())
		return
	}
	var change tunnelChange
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&change); err != nil {
		writeComputerError(w, http.StatusBadRequest, "that is not a change of the address")
		return
	}
	if err := address.Change(r.Context(), change); err != nil {
		writeComputerError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeComputerJSON(w, http.StatusOK, address.View())
}
