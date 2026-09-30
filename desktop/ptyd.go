package main

// The terminal daemon in native mode: the host terminals of an installation that runs on the
// operator's own machine. The launcher starts it as a child of its own, beside the supervisor and
// not under it, for the same reason a server runs it as a unit of its own: the supervisor restarts
// the agent whenever a change is applied, and a terminal must outlive that. Quitting the launcher
// stops it, and with it every host terminal, as quitting stops the agent.

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// unavailableFile is what the launcher leaves in the daemon's run directory when this build has no
// daemon to start. The agent reads it as the reason host terminals are unavailable, which says more
// than an empty directory would.
const unavailableFile = "unavailable"

// noPtydReason is that reason.
const noPtydReason = "this build carries no ptyd; host terminals are unavailable"

// ptydRunDir holds the daemon's endpoint and token, and ptydStateDir its logs, launches and shell
// scripts. Both are inside the local state folder, which the agent's policy seals whole: the token
// opens a shell as the operator.
func ptydRunDir(p Paths) string   { return filepath.Join(p.Local, "ptyd", "run") }
func ptydStateDir(p Paths) string { return filepath.Join(p.Local, "ptyd", "state") }

// ptydBinary is the daemon this launcher runs: DAEDALUS_PTYD when it is set (a daemon built by
// hand), else the ptyd packaged beside the launcher's own executable — which inside the macOS bundle
// is Contents/MacOS, where the bundle keeps every executable. "" when there is none, which is a
// source build of the launcher more often than not.
func ptydBinary(exe string, getenv func(string) string, exists func(string) bool) string {
	if p := getenv("DAEDALUS_PTYD"); p != "" {
		return p
	}
	if exe == "" {
		return ""
	}
	name := "ptyd"
	if runtime.GOOS == "windows" {
		name = "ptyd.exe"
	}
	if p := filepath.Join(filepath.Dir(exe), name); exists(p) {
		return p
	}
	return ""
}

// ptydListen is where the daemon listens: its socket, or loopback TCP where the socket's path would
// be longer than a unix socket allows (104 bytes on macOS, and the folder the operator dropped the
// app into can be deep) and on Windows, where the daemon has no other choice.
func ptydListen(runDir string) string {
	if runtime.GOOS == "windows" || len(filepath.Join(runDir, "ptyd.sock")) > 100 {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}

// ptydArgv is the daemon's command line.
func ptydArgv(binary string, p Paths) []string {
	run := ptydRunDir(p)
	return []string{binary, "serve", "--env", "host", "--run-dir", run, "--state-dir", ptydStateDir(p), "--listen", ptydListen(run)}
}

// ptydEnv is what the daemon, and so every host terminal, runs with: the operator's own
// environment, with the runtime's tools after the operator's PATH rather than before it. A host
// terminal is the operator's shell, where their own git and node come first; the runtime's are there
// for a machine that has none (Windows without Git, most often). None of the variables that keep uv
// inside the runtime are passed on: a uv the operator runs in a terminal is their own.
func ptydEnv(base []string, fallback []string) []string {
	env := make([]string, 0, len(base)+1)
	path := ""
	for _, kv := range base {
		key, value, _ := strings.Cut(kv, "=")
		if strings.EqualFold(key, "PATH") {
			path = value
			continue
		}
		env = append(env, kv)
	}
	dirs := []string{}
	if path != "" {
		dirs = append(dirs, path)
	}
	dirs = append(dirs, fallback...)
	return append(env, "PATH="+strings.Join(dirs, string(os.PathListSeparator)))
}

// ptydAnswers reports whether the daemon in runDir is serving: its endpoint file names a socket or a
// port and something accepts there. The file alone would count a daemon that was killed.
func ptydAnswers(runDir string) bool {
	body, err := os.ReadFile(filepath.Join(runDir, "endpoint"))
	if err != nil {
		return false
	}
	network, address, ok := parseEndpoint(strings.TrimSpace(string(body)), runDir)
	if !ok {
		return false
	}
	conn, err := net.DialTimeout(network, address, time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// waitForPtyd waits until the daemon answers, for at most limit.
func waitForPtyd(ctx context.Context, runDir string, limit time.Duration) bool {
	deadline := time.Now().Add(limit)
	for {
		if ptydAnswers(runDir) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// parseEndpoint reads "unix:<name>" (relative to the run directory) or "tcp:127.0.0.1:<port>".
func parseEndpoint(text, runDir string) (network, address string, ok bool) {
	kind, rest, _ := strings.Cut(text, ":")
	switch {
	case kind == "unix" && rest != "":
		if !filepath.IsAbs(rest) {
			rest = filepath.Join(runDir, rest)
		}
		return "unix", rest, true
	case kind == "tcp" && strings.HasPrefix(rest, "127.0.0.1:"):
		return "tcp", rest, true
	}
	return "", "", false
}

// markPtyd leaves the run directory saying why no daemon runs there, or clears that note before a
// daemon starts.
func markPtyd(runDir, reason string) error {
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		return err
	}
	note := filepath.Join(runDir, unavailableFile)
	if reason == "" {
		if err := os.Remove(note); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	return os.WriteFile(note, []byte(reason+"\n"), 0o600)
}
