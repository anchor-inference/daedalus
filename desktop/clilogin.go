package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// cliRunner runs one command and returns its standard output and exit code. err is non-nil only
// when the command could not be run at all — not found, or it timed out — which is different from a
// command that ran and said no.
type cliRunner func(ctx context.Context, name string, args ...string) (stdout string, exit int, err error)

// CLILogin is whether one agent CLI is signed in on this machine. How is "cli" when the CLI itself
// answered, "file" when its credential file decided, and empty when neither was there to ask.
type CLILogin struct {
	ID       string `json:"id"`
	SignedIn bool   `json:"signed_in"`
	How      string `json:"how"`
}

// cliProbeTimeout bounds one CLI. A CLI that phones home to answer must not hold the setup page
// open: the file fallback below is a good enough answer for a login the key proxy will serve.
const cliProbeTimeout = 6 * time.Second

// cliLoginProbe describes how to ask one CLI and where its credentials live. The file is the one the
// key proxy reads, so a readable file is a login the agent can use even when the CLI is absent from
// this machine's PATH.
type cliLoginProbe struct {
	id      string
	program string
	args    []string
	file    []string
	// parse turns the CLI's answer into yes, no or unknown. Unknown falls back to the file.
	parse func(stdout string, exit int) string
}

var cliLoginProbes = []cliLoginProbe{
	{
		id: "codex", program: "codex", args: []string{"login", "status"},
		file: []string{".codex", "auth.json"},
		parse: func(_ string, exit int) string {
			if exit == 0 {
				return "yes"
			}
			return "no"
		},
	},
	{
		id: "claude", program: "claude", args: []string{"auth", "status", "--json"},
		file: []string{".claude", ".credentials.json"},
		parse: func(stdout string, _ int) string {
			var data map[string]any
			if err := json.Unmarshal([]byte(stdout), &data); err != nil || data == nil {
				return "unknown"
			}
			if loggedIn, _ := data["loggedIn"].(bool); loggedIn {
				return "yes"
			}
			return "no"
		},
	},
	{
		id: "grok", program: "grok", args: []string{"models"},
		file: []string{".grok", "auth.json"},
		parse: func(stdout string, _ int) string {
			first := ""
			for _, line := range strings.Split(stdout, "\n") {
				if strings.TrimSpace(line) != "" {
					first = strings.TrimSpace(line)
					break
				}
			}
			switch {
			case strings.Contains(first, "not authenticated"):
				return "no"
			case strings.Contains(first, "logged in with"), strings.Contains(first, "XAI_API_KEY"):
				return "yes"
			}
			return "unknown"
		},
	},
}

// DetectCLILogins answers for the three CLIs in a fixed order. The probes run side by side because
// each may take seconds, and each CLI is run at most once.
func DetectCLILogins(ctx context.Context, home string, run cliRunner) []CLILogin {
	out := make([]CLILogin, len(cliLoginProbes))
	var wg sync.WaitGroup
	for i, probe := range cliLoginProbes {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out[i] = detectOneCLILogin(ctx, home, run, probe)
		}()
	}
	wg.Wait()
	return out
}

func detectOneCLILogin(ctx context.Context, home string, run cliRunner, probe cliLoginProbe) CLILogin {
	result := CLILogin{ID: probe.id}
	if run != nil {
		stdout, exit, err := run(ctx, probe.program, probe.args...)
		if err == nil {
			switch probe.parse(stdout, exit) {
			case "yes":
				result.SignedIn, result.How = true, "cli"
				return result
			case "no":
				result.How = "cli"
				return result
			}
		}
	}
	if home != "" && readableFile(filepath.Join(append([]string{home}, probe.file...)...)) {
		result.SignedIn, result.How = true, "file"
	}
	return result
}

// readableFile is true for a regular, non-empty file this process can open. Opening it, not just
// statting it, is what the key proxy will do.
func readableFile(path string) bool {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 {
		return false
	}
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	f.Close()
	return true
}

// systemCLIRunner runs the real programs. No shell is involved, so nothing the operator's profile
// prints can be mistaken for an answer, and stdin is closed so a CLI that wants to prompt fails
// instead of waiting for a person who is looking at the setup page.
func systemCLIRunner() cliRunner {
	return func(ctx context.Context, name string, args ...string) (string, int, error) {
		path, err := exec.LookPath(name)
		if err != nil {
			return "", -1, err
		}
		ctx, cancel := context.WithTimeout(ctx, cliProbeTimeout)
		defer cancel()
		cmd := exec.CommandContext(ctx, path, args...)
		cmd.Stdin = nil
		var stdout bytes.Buffer
		cmd.Stdout = &stdout
		cmd.WaitDelay = time.Second
		err = cmd.Run()
		if ctx.Err() != nil {
			return stdout.String(), -1, ctx.Err()
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return stdout.String(), exitErr.ExitCode(), nil
		}
		if err != nil {
			return "", -1, err
		}
		return stdout.String(), 0, nil
	}
}
