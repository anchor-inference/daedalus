package main

import (
	"os"
	"path/filepath"
)

// Paths is the folder layout the launcher owns. It is the layout deploy/compose.yaml already
// expects on a server — the compose file mounts "..", "../../protocore-exp" and
// "../../daedalus-secrets/..." relative to deploy/ — so compose runs unchanged against it.
//
//	<data>/daedalus                       the bot checkout (compose lives in its deploy/)
//	<data>/protocore-exp                  the core checkout
//	<data>/daedalus-secrets/keyproxy.env  provider keys, never inside a mounted checkout
//	<data>/daedalus-secrets/ssh           hosts the agent may reach (may stay empty)
//	<data>/daedalus-host-terminals        a server's host terminal socket; compose mounts it always
//	<data>/.env                           the values compose interpolates
//	<data>/compose.desktop.yaml           the override that points at the published images
//	<data>/mode                           docker or native, chosen once and remembered
//	<data>/lang                           en or ru, the language the launcher's own pages speak
//
// Native mode adds the pieces a container would otherwise have held. The database and the
// workspaces are files under the same folder rather than Docker volumes:
//
//	<data>/state/  <data>/workspaces/     what the volumes hold in Docker mode
//
// What can be rebuilt is kept out of the data folder (local.go says where, and why):
//
//	<runtime>/uv/uv                       the installer for everything below it
//	<runtime>/python/                     the CPython uv manages
//	<runtime>/envs/<digest>/              the app's environment, one per dependency lock
//	<runtime>/cache/                      uv's cache, which the environments hard-link from
//	<runtime>/bin/rg                      the one binary Search needs
//	<runtime>/git/                        MinGit, on Windows only
//	<runtime>/node/  <runtime>/browsers/  extras, on demand
//	<local>/logs/                         the supervisor's output, rotated by the launcher
//	<local>/pids/                         the records of running children
//	<local>/ptyd/  <local>/browserd/      the daemons' endpoints, tokens, terminal logs, profiles
//	<local>/supervisor.sock               the supervisor's socket
//
// A data folder from before this split still has <data>/runtime; the launcher moves it out once
// (migrateLegacyRuntime), and the upgrade's writer fence refuses a data folder that holds one.
type Paths struct {
	Data        string
	Bot         string
	Core        string
	Secrets     string
	KeyproxyEnv string
	SSH         string
	Env         string
	BotEnv      string
	Compose     string
	Override    string
	Mode        string
	Lang        string

	// HostTerminals is where a host terminal daemon keeps its socket on a server. The compose file
	// mounts it whether or not one runs, and a missing source would be created by Docker as root.
	HostTerminals string

	Runtime         string
	RuntimeEnvs     string
	RuntimeCache    string
	RuntimeUV       string
	RuntimePython   string
	RuntimeVenv     string
	RuntimeBin      string
	RuntimeGit      string
	RuntimeNode     string
	RuntimeBrowsers string
	RuntimeStamps   string
	RuntimeLogs     string
	// Local is this machine's state for the installation: logs, child records, the daemons'
	// endpoints and profiles, the supervisor's socket. Not data, not cache.
	Local            string
	SupervisorSocket string
	// LegacyRuntime is where the runtime lived before it moved out of the data folder.
	LegacyRuntime string
	State         string
	Workspaces    string
}

// NewPaths resolves the data directory: --data when given, otherwise the per-user folder
// DefaultDataDir names (relocate.go).
func NewPaths(dataDir string) (Paths, error) {
	if dataDir == "" {
		exe, err := os.Executable()
		if err != nil {
			exe = ""
		}
		dataDir = DefaultDataDir(exe)
	}
	abs, err := filepath.Abs(dataDir)
	if err != nil {
		return Paths{}, err
	}
	bot := filepath.Join(abs, "daedalus")
	secrets := filepath.Join(abs, "daedalus-secrets")
	runtimeDir, local, err := localRoots(abs)
	if err != nil {
		return Paths{}, err
	}
	p := Paths{
		Data:        abs,
		Bot:         bot,
		Core:        filepath.Join(abs, "protocore-exp"),
		Secrets:     secrets,
		KeyproxyEnv: filepath.Join(secrets, "keyproxy.env"),
		SSH:         filepath.Join(secrets, "ssh"),
		Env:         filepath.Join(abs, ".env"),
		BotEnv:      filepath.Join(bot, ".env"),
		Compose:     filepath.Join(bot, "deploy", "compose.yaml"),
		Override:    filepath.Join(abs, "compose.desktop.yaml"),
		Mode:        filepath.Join(abs, "mode"),
		Lang:        filepath.Join(abs, "lang"),

		HostTerminals: filepath.Join(abs, "daedalus-host-terminals"),

		Runtime:          runtimeDir,
		RuntimeEnvs:      filepath.Join(runtimeDir, "envs"),
		RuntimeCache:     filepath.Join(runtimeDir, "cache"),
		RuntimeUV:        filepath.Join(runtimeDir, "uv"),
		RuntimePython:    filepath.Join(runtimeDir, "python"),
		RuntimeVenv:      filepath.Join(runtimeDir, "envs", "new"),
		RuntimeBin:       filepath.Join(runtimeDir, "bin"),
		RuntimeGit:       filepath.Join(runtimeDir, "git"),
		RuntimeNode:      filepath.Join(runtimeDir, "node"),
		RuntimeBrowsers:  filepath.Join(runtimeDir, "browsers"),
		RuntimeStamps:    filepath.Join(runtimeDir, "installed"),
		RuntimeLogs:      filepath.Join(local, "logs"),
		Local:            local,
		SupervisorSocket: filepath.Join(local, "supervisor.sock"),
		LegacyRuntime:    filepath.Join(abs, "runtime"),
		State:            filepath.Join(abs, "state"),
		Workspaces:       filepath.Join(abs, "workspaces"),
	}
	p.selectEnv()
	return p, nil
}

// supervisorOverTCP says the supervisor listens on loopback TCP instead of its socket: on Windows,
// and wherever the socket's path would not fit in sun_path — 104 bytes on macOS, 108 on Linux — which
// a long home folder under ~/Library/Application Support/Daedalus/State reaches. The rule is ptyd's
// and browserd's, and the limit the same 100 bytes, below both.
func supervisorOverTCP(p Paths, goos string) bool {
	return goos == "windows" || len(p.SupervisorSocket) > 100
}

// selectEnv points RuntimeVenv at the environment for the checkout's current dependency lock. One
// environment per lock is what lets an upgrade prepare the next version's beside the running one,
// and a rollback find the old one untouched.
func (p *Paths) selectEnv() {
	if digest, err := dependencyDigest(p.Bot); err == nil && exists(p.Bot) {
		p.RuntimeVenv = envDir(*p, digest)
	}
}

func envDir(p Paths, digest string) string { return filepath.Join(p.RuntimeEnvs, digest[:16]) }

// bundleRoot reports the .app directory an executable is running out of, and whether it is running
// out of one at all. The layout macOS requires is <Something>.app/Contents/MacOS/<executable>.
func bundleRoot(exe string) (string, bool) {
	if exe == "" {
		return "", false
	}
	macos := filepath.Dir(exe)      // .../Contents/MacOS
	contents := filepath.Dir(macos) // .../Contents
	app := filepath.Dir(contents)   // .../Something.app
	if filepath.Base(macos) != "MacOS" || filepath.Base(contents) != "Contents" || filepath.Ext(app) != ".app" {
		return "", false
	}
	return app, true
}

// Bundled reports whether this process is the executable inside a .app — which is also to say that
// it was most likely started from Finder, with no terminal to print to.
func Bundled() bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	_, ok := bundleRoot(exe)
	return ok
}

// EnsureDirs creates the folders that must exist before anything is written into them. The secrets
// directory is 0700: it holds the file the key proxy reads.
func (p Paths) EnsureDirs() error {
	if err := os.MkdirAll(p.Data, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(p.Secrets, 0o700); err != nil {
		return err
	}
	if err := os.MkdirAll(p.HostTerminals, 0o700); err != nil {
		return err
	}
	return os.MkdirAll(p.SSH, 0o700)
}

// EnsureNativeDirs creates what native mode writes into and Docker mode keeps in volumes. The state
// directory is 0700: it holds the database, the sessions and the pairing links.
func (p Paths) EnsureNativeDirs() error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	// The local state holds tokens that open a shell and drive logged-in browsers: 0700, made first
	// so that nothing below it creates it with a looser mode, and set explicitly if it was.
	if err := os.MkdirAll(p.Local, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(p.Local, 0o700); err != nil {
		return err
	}
	for _, dir := range []string{p.Runtime, p.RuntimeEnvs, p.RuntimeBin, p.RuntimeStamps, p.RuntimeLogs, p.Workspaces} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	return os.MkdirAll(p.State, 0o700)
}

// Configured reports whether a previous run already wrote the environment. A missing .env is the
// signal to ask the questions again; an existing one means start straight away. The checkouts are a
// separate matter: they are cloned on the first start, with or without answers.
func (p Paths) Configured() bool { return exists(p.Env) }

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
