package chrome

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/netwall"
)

// Flags are the switches every browser starts with (the contract's Which Chromium explains each).
// Two of them were found the hard way: without --password-store=basic and
// --disable-field-trial-config a pinned Chromium on a desktop session never sends its first request,
// because its cookie store waits for a keyring that does not answer; and WebRTC's UDP stays off the
// network only with --webrtc-ip-handling-policy (the --force- spelling is not honoured).
var Flags = []string{
	"--headless=new",
	"--remote-debugging-pipe",
	"--no-first-run",
	"--no-default-browser-check",
	"--password-store=basic",
	"--disable-field-trial-config",
	"--disable-background-networking",
	"--disable-component-update",
	"--disable-sync",
	"--disable-default-apps",
	"--disable-extensions",
	"--disable-breakpad",
	"--metrics-recording-only",
	"--no-service-autorun",
	"--mute-audio",
	"--hide-scrollbars",
	"--disable-client-side-phishing-detection",
	"--disable-domain-reliability",
	"--no-pings",
	"--webrtc-ip-handling-policy=disable_non_proxied_udp",
	"--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,PasswordManagerOnboarding",
}

// Options say how to start one browser.
type Options struct {
	Path       string
	ProfileDir string
	// Proxy is the network wall's address; empty starts the browser without one.
	Proxy     string
	Args      []string
	NoSandbox bool
	// Env is the browser's environment; nil inherits the daemon's.
	Env []string
}

// Process is one running browser.
type Process struct {
	Conn *cdp.Conn
	Pid  int

	cmd    *exec.Cmd
	stderr *tail
	done   chan struct{}
	once   sync.Once
	exit   error
}

// Args returns the command line for opts, without the program.
func Args(opts Options) []string {
	a := append([]string{}, Flags...)
	a = append(a, "--user-data-dir="+opts.ProfileDir)
	if opts.Proxy != "" {
		// The wall's own switches: the proxy, loopback and link-local sent through it too, no name
		// resolved by Chromium itself, no QUIC (netwall.ChromiumArgs says why each).
		a = append(a, netwall.ChromiumArgs(opts.Proxy)...)
	}
	if opts.NoSandbox {
		a = append(a, "--no-sandbox")
	}
	a = append(a, opts.Args...)
	return append(a, "about:blank")
}

// Start starts a browser and connects to its pipe. handler receives its events.
func Start(opts Options, handler func(cdp.Event)) (*Process, error) {
	if err := os.MkdirAll(opts.ProfileDir, 0o700); err != nil {
		return nil, err
	}
	if err := WritePreferences(opts.ProfileDir); err != nil {
		return nil, fmt.Errorf("profile preferences: %w", err)
	}
	p := &Process{stderr: newTail(16 << 10), done: make(chan struct{})}
	cmd, conn, err := startPiped(opts, p.stderr, handler)
	if err != nil {
		return nil, err
	}
	p.cmd, p.Conn, p.Pid = cmd, conn, cmd.Process.Pid
	go func() {
		p.exit = cmd.Wait()
		close(p.done)
	}()
	return p, nil
}

// StartError is why a browser that was started never gave its first answer: it exited, or it ran on
// without a word for the whole budget. Either way its stderr is carried, because that is where
// Chromium says what went wrong: a bare "deadline exceeded" could not tell a browser that had died
// from one still paging itself in from a slow disk.
type StartError struct {
	Method string
	// Exited is true when the main process had ended; ExitCode is then its code (-1 for a signal).
	Exited   bool
	ExitCode int
	Pid      int
	Waited   time.Duration
	// Stderr is the last few lines the browser wrote.
	Stderr string
}

func (e *StartError) Error() string {
	var s string
	if e.Exited {
		s = fmt.Sprintf("Chromium exited with code %d before it answered %s", e.ExitCode, e.Method)
	} else {
		s = fmt.Sprintf("Chromium (pid %d) did not answer %s on its pipe within %s", e.Pid, e.Method, e.Waited.Round(time.Millisecond))
	}
	if e.Stderr == "" {
		return s + "; it wrote nothing to stderr"
	}
	return s + "; its stderr ends: " + e.Stderr
}

// FirstAnswer sends the browser its first command and waits at most budget for the reply. It ends
// early when the main process exits, so a browser that could not start is reported at once and in
// its own words, not after the budget as a timeout. An exit is noticed through the process as well
// as the pipe because a helper that inherited the pipe would keep it open after the browser died.
// A cancelled ctx is the caller giving up, returned as it is.
func (p *Process) FirstAnswer(ctx context.Context, budget time.Duration, method string, result any) error {
	begin := time.Now()
	callCtx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	go func() {
		select {
		case <-p.done:
			cancel()
		case <-callCtx.Done():
		}
	}()
	err := p.Conn.Call(callCtx, "", method, nil, result)
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return err
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		// The pipe ended or the process did: the exit status and the rest of stderr arrive a moment
		// after the pipe closes, once the process is reaped.
		select {
		case <-p.done:
		case <-time.After(2 * time.Second):
		}
	}
	se := &StartError{Method: method, Pid: p.Pid, Waited: time.Since(begin), ExitCode: p.ExitCode()}
	select {
	case <-p.done:
		se.Exited = true
	default:
	}
	se.Stderr = lastLines(p.Stderr(), 6, 1500)
	return se
}

// lastLines is the end of s: its last n lines, and of those at most max bytes.
func lastLines(s string, n, max int) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	s = strings.Join(lines, "\n")
	if len(s) > max {
		s = strings.ToValidUTF8("…"+s[len(s)-max:], "")
	}
	return s
}

// Done is closed when the browser's main process has exited.
func (p *Process) Done() <-chan struct{} { return p.done }

// ExitCode is the main process's exit code, -1 while it runs or when it was killed by a signal.
func (p *Process) ExitCode() int {
	select {
	case <-p.done:
	default:
		return -1
	}
	if p.cmd.ProcessState == nil {
		return -1
	}
	return p.cmd.ProcessState.ExitCode()
}

// Stderr is the last 16 KiB the browser wrote to stderr: where it says why it could not start.
func (p *Process) Stderr() string { return p.stderr.String() }

// Close asks the browser to close and kills what is left after grace.
func (p *Process) Close(grace time.Duration) {
	p.once.Do(func() {
		ctx, cancel := context.WithTimeout(context.Background(), grace)
		_ = p.Conn.Call(ctx, "", "Browser.close", nil, nil)
		cancel()
		select {
		case <-p.done:
		case <-time.After(grace):
		}
		killTree(p.cmd)
		<-p.done
	})
}

// Kill ends the browser at once.
func (p *Process) Kill() {
	p.once.Do(func() {
		killTree(p.cmd)
		<-p.done
	})
}

// SandboxProblem reads the browser's stderr for the reason it could not start its sandbox, in
// words an operator can act on; "" when stderr names none.
func SandboxProblem(stderr string) string {
	if !strings.Contains(stderr, "No usable sandbox") {
		return ""
	}
	return "Chromium found no usable sandbox: unprivileged user namespaces are restricted here " +
		"(on Ubuntu 23.10 and later by AppArmor), and no setuid sandbox helper is named in CHROME_DEVEL_SANDBOX; " +
		"in a container, give the service seccomp=unconfined"
}

// preferences are written into every profile before its browser starts. The WebRTC ones say what the
// switch says, a second time, because a profile outlives any one command line.
var preferences = map[string]any{
	"webrtc": map[string]any{
		"ip_handling_policy":      "disable_non_proxied_udp",
		"multiple_routes_enabled": false,
		"nonproxied_udp_enabled":  false,
	},
	"credentials_enable_service": false,
	"password_manager_enabled":   false,
	"autofill": map[string]any{
		"profile_enabled":     false,
		"credit_card_enabled": false,
	},
	"download": map[string]any{"prompt_for_download": false},
	// A browser the daemon killed would otherwise offer to restore its tabs on the next start.
	"profile": map[string]any{"exit_type": "Normal", "exited_cleanly": true, "password_manager_enabled": false},
}

// WritePreferences merges the daemon's preferences into the profile's Default/Preferences, keeping
// everything else Chromium keeps there.
func WritePreferences(profileDir string) error {
	dir := filepath.Join(profileDir, "Default")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	path := filepath.Join(dir, "Preferences")
	current := map[string]any{}
	if data, err := os.ReadFile(path); err == nil {
		// A preferences file Chromium left half-written is replaced rather than trusted.
		_ = json.Unmarshal(data, &current)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	merge(current, preferences)
	data, err := json.Marshal(current)
	if err != nil {
		return err
	}
	tmp := path + ".browserd"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func merge(dst, src map[string]any) {
	for k, v := range src {
		if sub, ok := v.(map[string]any); ok {
			d, ok := dst[k].(map[string]any)
			if !ok {
				d = map[string]any{}
				dst[k] = d
			}
			merge(d, sub)
			continue
		}
		dst[k] = v
	}
}

// tail keeps the last n bytes written to it.
type tail struct {
	mu  sync.Mutex
	n   int
	buf []byte
}

func newTail(n int) *tail { return &tail{n: n} }

func (t *tail) Write(b []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, b...)
	if len(t.buf) > t.n {
		t.buf = append([]byte(nil), t.buf[len(t.buf)-t.n:]...)
	}
	return len(b), nil
}

func (t *tail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
