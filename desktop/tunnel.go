package main

// The public address of a native installation: which tunnel, whether it is on, and the process holding it.
//
// The operator opens one from the app's settings, and only from there: the agent may not raise a tunnel,
// and the host's rule says so. The request reaches this file over the launcher's bridge. What the
// operator chose is kept here, and a token goes in through the bridge once and never comes back: nothing
// here returns one, logs one or puts one in an environment or on a command line. tunneld reads it from a
// file of 0600 in the local state folder.
//
// What guards that file from the agent is not one thing. The policy refuses a command that names the
// folder, and that matches words, so a command building the path gets past it. On Linux with the sandbox on
// (the default) the folder is not in a command's filesystem at all. On a Mac, on Windows, or with the
// sandbox off, nothing stops a command that builds the path: the agent is the same user. The settings say so.
//
// tunneld is started only while the address is on, it stands apart from the rest (a missing tunneld takes
// the address away and nothing else), and it holds a pipe from this process, so a launcher that dies
// closes the address. The status comes from its file while it runs, and is not believed once it has died.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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
	// NgrokPath and NgrokHash are the program the operator said is theirs, and what it was when they did.
	NgrokPath string `json:"ngrok_path,omitempty"`
	NgrokHash string `json:"ngrok_hash,omitempty"`
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
	// TrustNgrok is the operator looking at the path the view shows and saying that is their ngrok.
	TrustNgrok bool `json:"trust_ngrok"`
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
	Ports     []int     `json:"ports"`
	Wakes     int       `json:"wakes"`
	// VisitorUnknown says this provider named no visitor, so all of them are one address to every limit.
	VisitorUnknown bool `json:"visitor_unknown"`
}

// tunnelProviderView is what the settings need of one provider. It says whether a token is kept, and
// nothing about the token.
type tunnelProviderView struct {
	Client bool `json:"client"`
	Token  bool `json:"token"`
	// Found is the program a provider would run, shown to the operator; Trusted says they accepted it.
	Found    string `json:"found,omitempty"`
	Trusted  bool   `json:"trusted,omitempty"`
	SignedIn bool   `json:"signed_in,omitempty"`
	Server   string `json:"server,omitempty"`
	Domain   string `json:"domain,omitempty"`
}

// tunnelView is the answer to GET /api/tunnel.
type tunnelView struct {
	// Tunneld says this build carries the program at all.
	Tunneld   bool       `json:"tunneld"`
	Enabled   bool       `json:"enabled"`
	Provider  string     `json:"provider"`
	State     string     `json:"state"`
	Address   string     `json:"address"`
	Confirmed bool       `json:"confirmed"`
	SignIn    string     `json:"sign_in,omitempty"`
	Code      string     `json:"code,omitempty"`
	Detail    string     `json:"detail,omitempty"`
	Since     *time.Time `json:"since,omitempty"`
	PIDs      []int      `json:"pids"`
	Ports     []int      `json:"ports"`
	Wakes     int        `json:"wakes"`
	// VisitorUnknown: see tunnelStatus.
	VisitorUnknown bool                          `json:"visitor_unknown"`
	Providers      map[string]tunnelProviderView `json:"providers"`
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
	// The folder and not the file: the file is renamed in from a scratch file, which takes the
	// folder's entries when it is made. Docker mode never makes the local state's folder private
	// (EnsureNativeDirs), so this folder is made so itself.
	if err := restrictToUser(filepath.Dir(path)); err != nil {
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
	return []string{"/usr/local/bin/ngrok", "/usr/bin/ngrok", "/snap/bin/ngrok"}
}

func exeName(name string) string {
	if runtime.GOOS == "windows" {
		return name + ".exe"
	}
	return name
}

// ngrokFound is the ngrok program there is to offer the operator, or "". ngrok cannot be pinned, since it
// is theirs and updates itself, and on a Mac and on Windows the usual installs land in folders this user
// can write. So nothing here decides it is safe: the view shows this path, the operator accepts it once,
// and from then on it runs only while it is that same file.
func ngrokFound() string {
	for _, place := range ngrokPlaces(runtime.GOOS, os.Getenv) {
		if exists(place) {
			return place
		}
	}
	if found, err := exec.LookPath("ngrok"); err == nil {
		if resolved, err := filepath.Abs(found); err == nil {
			return resolved
		}
	}
	return ""
}

func hashFile(path string) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return ""
	}
	return hex.EncodeToString(digest.Sum(nil))
}

// tunnelClient is the program a provider runs: for cloudflared and frpc the copy fetched into this
// installation's own folder and nothing else, and for ngrok the file the operator accepted. Whether it is
// still what it should be is checked before it runs (ensureClient).
func tunnelClient(p Paths, provider string) string {
	switch provider {
	case tunnelCloudflare:
		return filepath.Join(tunnelToolsDir(p), exeName("cloudflared"))
	case tunnelFrp:
		return filepath.Join(tunnelToolsDir(p), exeName("frpc"))
	case tunnelNgrok:
		return readTunnelChoice(p).NgrokPath
	}
	return ""
}

// clientPin is what a fetched client's download and program should be.
func clientPin(provider string) (download, string, bool) {
	var table map[string]download
	switch provider {
	case tunnelCloudflare:
		table = cloudflaredDownloads
	case tunnelFrp:
		table = frpDownloads
	default:
		return download{}, "", false
	}
	d, err := pick(table, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return download{}, "", false
	}
	return d, clientHashes[provider][platformKey(runtime.GOOS, runtime.GOARCH)], true
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
	// find, fetch and pin are variables so the tests can run with no tunneld and no network.
	find  func() string
	fetch func(ctx context.Context, d download) error
	pin   func(provider string) (download, string, bool)

	mu         sync.Mutex
	ctx        context.Context
	process    *Process
	generation int
	installing bool
	failure    string
}

func newTunnels(p Paths, log func(string, ...any)) *tunnels {
	t := &tunnels{paths: p, log: log, pin: clientPin}
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
	if choice.Provider == tunnelNgrok {
		// Said at once, and nothing is started: a program the operator has not accepted does not run.
		if failure := ngrokFailure(choice); failure != "" {
			t.failure = failure
			return
		}
	}
	_, _, pinned := t.pin(choice.Provider)
	t.installing = pinned
	go func() {
		if err := t.ensureClient(ctx, choice.Provider); err != nil {
			t.log("the tunnel's client could not be had: %v", err)
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

// ngrokFailure is what stands between an accepted ngrok and running it: nothing accepted, or a file that is
// no longer the one accepted. A real update of ngrok asks again, which is the price of that check.
func ngrokFailure(choice tunnelChoice) string {
	if choice.NgrokPath == "" {
		return "client_unconfirmed"
	}
	if hashFile(choice.NgrokPath) != choice.NgrokHash {
		return "client_changed"
	}
	return ""
}

// ensureClient leaves a fetched client exactly as pinned, whatever was there. An exists() here ran a replaced
// frpc as trusted, and kept an old one through a raised pin for ever. Hashed at each start: 40 MB takes a
// fraction of a second, and this is the file that holds the token and all the public traffic.
func (t *tunnels) ensureClient(ctx context.Context, provider string) error {
	d, want, pinned := t.pin(provider)
	if !pinned {
		if provider == tunnelCloudflare || provider == tunnelFrp {
			return fmt.Errorf("no pinned build of %s for this machine", provider)
		}
		return nil
	}
	path := tunnelClient(t.paths, provider)
	if hashFile(path) == want {
		return nil
	}
	if exists(path) {
		t.log("%s is not the pinned program; replacing it", d.name)
	}
	// The stamp says the archive was unpacked, which is no longer the question.
	_ = os.Remove(t.paths.stamp(d.name))
	if err := t.fetch(ctx, d); err != nil {
		return err
	}
	if got := hashFile(path); got != want {
		return fmt.Errorf("%s came out as %s, not the pinned %s", d.name, got, want)
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
	if change.TrustNgrok {
		// The path is found here and now, never taken from the request: this is the one the operator was
		// shown a moment ago, and the app has no say in which program gets the token.
		if found := ngrokFound(); found != "" {
			choice.NgrokPath, choice.NgrokHash = found, hashFile(found)
		}
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
		Tunneld: t.find() != "", Enabled: choice.Enabled, Provider: choice.Provider, State: "off", PIDs: []int{}, Ports: []int{},
		Providers: map[string]tunnelProviderView{
			tunnelNgrok:      {Client: ngrokFailure(choice) == "", Found: ngrokFound(), Trusted: ngrokFailure(choice) == "", Token: secrets.NgrokToken != ""},
			tunnelTailscale:  {Client: true, SignedIn: exists(filepath.Join(tunnelDir(t.paths), "tailscale", "tailscaled.state"))},
			tunnelCloudflare: {Client: exists(tunnelClient(t.paths, tunnelCloudflare))},
			tunnelFrp:        {Client: exists(tunnelClient(t.paths, tunnelFrp)), Token: secrets.FrpToken != "", Server: choice.FrpServer, Domain: choice.FrpDomain},
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
	view.PIDs, view.Ports, view.VisitorUnknown = status.PIDs, status.Ports, status.VisitorUnknown
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
