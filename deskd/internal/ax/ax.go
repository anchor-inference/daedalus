// Package ax runs agent-desktop, the third-party tool that reads a window's accessibility tree and
// acts on its elements, as a child process with JSON output. It knows the tool's command line and
// its answers and nothing of leases or scopes: package desk decides whether a call may happen, and
// this package only makes sure that what runs is the pinned version, with arguments built here, in a
// state root of its own, and never in the tool's real-input mode.
package ax

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// The reasons the tool is unavailable, as the contract's 1209 names them.
const (
	ReasonOff         = "off"
	ReasonMissing     = "missing"
	ReasonVersion     = "version"
	ReasonFailed      = "failed"
	ReasonCrashed     = "crashed"
	ReasonUnsupported = "unsupported"
	ReasonNoWindow    = "no_window"
)

// The version the daemon speaks to. The tool breaks its payloads between minor versions (its
// envelope 2.5 removed the success flags of window and pointer commands), so the daemon runs only the
// release it was checked against, the one the desktop release ships; a newer tool waits for the
// daemon to be checked against it. The envelope's major must match and its minor be at least 2.5,
// so even a pin moved without reading the tool's changes fails closed on a changed payload.
//
// The binary's bytes are not hashed here: the release's checksums and the operator's signature
// cover them before install, macOS signs the binary after the launcher is built (so no hash is known
// then), and a hash kept beside the binary stops no one who can replace both.
const PinnedVersion = "1.0.2"

var (
	envelopeMajor      = 2
	minEnvelopeMinor   = 5
	probeTimeout       = 10 * time.Second
	killGrace          = 5 * time.Second
	maxOutput          = 8 << 20
	waitDelayAfterKill = 2 * time.Second
)

// Config is where the tool is and how to run it.
type Config struct {
	// Path is the binary.
	Path string
	// Prefix comes before the tool's own arguments: the built-in fake is the daemon's own executable
	// with "axfake" in front.
	Prefix []string
	// StateDir holds one state root per lease. It is emptied at start: the tool's reference maps
	// keep the text of the screen, which must not outlive the daemon.
	StateDir string
	// Env is added to the child's environment (the fake's scene).
	Env []string
	// Before runs before every call, After after it; the fake desktop uses them to hand the fake
	// tool its scene and to collect what the fake tool recorded.
	Before func() error
	After  func()
}

// Status is whether the tool is served, as info reports it.
type Status struct {
	Available bool   `json:"available"`
	Reason    string `json:"reason,omitempty"`
	Version   string `json:"version,omitempty"`
}

// Tool is one agent-desktop binary, checked once at start. Calls run one at a time: the tool takes
// a machine-wide lock of its own for actions anyway, and one child at a time keeps a burst of host
// calls from becoming a burst of processes walking the same tree.
type Tool struct {
	cfg    Config
	status Status
	mu     sync.Mutex
}

// Off is a tool that is not served, for the reason given.
func Off(reason string) *Tool { return &Tool{status: Status{Reason: reason}} }

// Open checks the binary (it exists, it hashes to the pinned value, its version is one this package
// speaks) and returns the tool, available or not. It never fails: a missing tool leaves the rest of
// the daemon as it was.
func Open(ctx context.Context, cfg Config) *Tool {
	if cfg.StateDir != "" {
		// Absolute, so the helper search compares like with like.
		if abs, err := filepath.Abs(cfg.StateDir); err == nil {
			cfg.StateDir = abs
		}
	}
	t := &Tool{cfg: cfg}
	t.status = t.probe(ctx)
	return t
}

// Status is the outcome of the check at start.
func (t *Tool) Status() Status { return t.status }

func (t *Tool) probe(ctx context.Context) Status {
	st, err := os.Stat(t.cfg.Path)
	if err != nil || !st.Mode().IsRegular() {
		return Status{Reason: ReasonMissing}
	}
	if t.cfg.StateDir != "" {
		// A daemon that died without its Close leaves helpers and reference maps behind.
		stopHelpers(t.cfg.StateDir)
		if err := resetDir(t.cfg.StateDir); err != nil {
			return Status{Reason: ReasonFailed}
		}
	}
	// version needs no permission and reads no window: it is the cheapest proof that the binary
	// loads here at all (a glibc older than the tool's floor fails before main).
	env, err := t.run(ctx, "", probeTimeout, "version")
	if err != nil {
		return Status{Reason: ReasonFailed}
	}
	var v struct {
		Version string `json:"version"`
	}
	if !env.OK || json.Unmarshal(env.Data, &v) != nil {
		return Status{Reason: ReasonFailed}
	}
	if !envelopeSupported(env.Version) || v.Version != PinnedVersion {
		return Status{Reason: ReasonVersion, Version: v.Version}
	}
	return Status{Available: true, Version: v.Version}
}

// resetDir empties dir and makes it again, private to the user: the tool refuses a state root that
// others can read, and it would be right to.
func resetDir(dir string) error {
	if err := os.RemoveAll(dir); err != nil {
		return err
	}
	return mkPrivate(dir)
}

func mkPrivate(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	// MkdirAll is subject to the umask, and an existing directory keeps its mode.
	return os.Chmod(dir, 0o700)
}

func envelopeSupported(v string) bool {
	major, minor, ok := strings.Cut(v, ".")
	if !ok {
		return false
	}
	ma, err1 := strconv.Atoi(major)
	mi, err2 := strconv.Atoi(minor)
	return err1 == nil && err2 == nil && ma == envelopeMajor && mi >= minEnvelopeMinor
}

// Envelope is the tool's answer: one JSON object on stdout.
type Envelope struct {
	Version string          `json:"version"`
	OK      bool            `json:"ok"`
	Command string          `json:"command"`
	Data    json.RawMessage `json:"data"`
	Error   *EnvelopeError  `json:"error"`
}

// EnvelopeError is the tool's structured error.
type EnvelopeError struct {
	Code        string          `json:"code"`
	Message     string          `json:"message"`
	Details     json.RawMessage `json:"details,omitempty"`
	Disposition Disposition     `json:"disposition"`
}

// Disposition is what the tool says about an action's delivery.
type Disposition struct {
	Delivery string `json:"delivery"`
	Retry    string `json:"retry"`
}

// The tool's delivery words.
const (
	ToolNotDelivered        = "not_delivered"
	ToolDeliveredVerified   = "delivered_verified"
	ToolDeliveredUnverified = "delivered_unverified"
)

// ToolError is a call the tool answered with an error.
type ToolError struct {
	Code     string
	Message  string
	Delivery string
	Details  json.RawMessage
}

func (e *ToolError) Error() string { return fmt.Sprintf("agent-desktop: %s: %s", e.Code, e.Message) }

// Unavailable is a call that could not reach a working tool.
type Unavailable struct {
	Reason string
	Detail string
}

func (e *Unavailable) Error() string {
	if e.Detail != "" {
		return "the accessibility tool is unavailable (" + e.Reason + "): " + e.Detail
	}
	return "the accessibility tool is unavailable (" + e.Reason + ")"
}

// ErrTooLarge is an answer larger than the daemon reads.
var ErrTooLarge = errors.New("the accessibility tool's answer is too large")

// ErrDeadline is a child that outlived its deadline and was killed.
var ErrDeadline = errors.New("the accessibility tool did not answer in time and was stopped")

// Run runs one command of the tool for a lease and returns its data, or its error as a ToolError.
// The tool's own deadline is timeout; the child is killed killGrace after it, and at once when ctx
// ends (a stop, the lease's end, the operator's hand). A killed child is ctx's error.
func (t *Tool) Run(ctx context.Context, lease string, timeout time.Duration, args ...string) (json.RawMessage, error) {
	if !t.status.Available {
		return nil, &Unavailable{Reason: t.status.Reason}
	}
	env, err := t.run(ctx, lease, timeout+killGrace, args...)
	if err != nil {
		return nil, err
	}
	if !env.OK {
		if env.Error == nil {
			return nil, &Unavailable{Reason: ReasonCrashed, Detail: "an error with no description"}
		}
		return nil, &ToolError{Code: env.Error.Code, Message: env.Error.Message, Delivery: env.Error.Disposition.Delivery,
			Details: env.Error.Details}
	}
	return env.Data, nil
}

// EndLease deletes the state root of a lease that ended. It waits for a child still running there.
func (t *Tool) EndLease(lease string) {
	if t.cfg.StateDir == "" || lease == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	root := filepath.Join(t.cfg.StateDir, lease)
	stopHelpers(root)
	_ = os.RemoveAll(root)
}

// Close stops every helper the tool left and deletes its state, when the daemon exits.
func (t *Tool) Close() {
	if t.cfg.StateDir == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	stopHelpers(t.cfg.StateDir)
	_ = os.RemoveAll(t.cfg.StateDir)
}

func (t *Tool) run(ctx context.Context, lease string, limit time.Duration, args ...string) (Envelope, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	home := t.cfg.StateDir
	if lease != "" && home != "" {
		home = filepath.Join(home, lease)
		if err := mkPrivate(home); err != nil {
			return Envelope{}, &Unavailable{Reason: ReasonFailed, Detail: err.Error()}
		}
	}
	if t.cfg.Before != nil {
		if err := t.cfg.Before(); err != nil {
			return Envelope{}, &Unavailable{Reason: ReasonFailed, Detail: err.Error()}
		}
	}
	if t.cfg.After != nil {
		defer t.cfg.After()
	}
	cctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	cmd := exec.CommandContext(cctx, t.cfg.Path, append(append([]string(nil), t.cfg.Prefix...), args...)...)
	cmd.Env = childEnv(os.Environ(), home, t.cfg.Env)
	cmd.Stdin = nil
	out := &capped{max: maxOutput}
	cmd.Stdout = out
	// The tool's stderr is its tracing, which can carry the text of the screen: it is dropped, never
	// logged.
	cmd.Stderr = io.Discard
	setGroup(cmd)
	cmd.Cancel = func() error { return killGroup(cmd) }
	cmd.WaitDelay = waitDelayAfterKill
	err := cmd.Run()
	if ctx.Err() != nil {
		return Envelope{}, ctx.Err()
	}
	if cctx.Err() != nil {
		return Envelope{}, ErrDeadline
	}
	if out.over {
		return Envelope{}, ErrTooLarge
	}
	// Exit 1 is a structured error and 2 an argument error; both print an envelope. Anything else,
	// or no JSON at all, is a tool that crashed.
	var exit *exec.ExitError
	if err != nil && (!errors.As(err, &exit) || (exit.ExitCode() != 1 && exit.ExitCode() != 2)) {
		return Envelope{}, &Unavailable{Reason: ReasonCrashed, Detail: err.Error()}
	}
	var env Envelope
	if jerr := json.Unmarshal(bytes.TrimSpace(out.buf.Bytes()), &env); jerr != nil {
		return Envelope{}, &Unavailable{Reason: ReasonCrashed, Detail: "an answer that is not JSON"}
	}
	return env, nil
}

// childEnv is the daemon's environment without any of the tool's own variables, which could name
// another state root, another session or an internal mode, plus the lease's state root.
func childEnv(parent []string, home string, extra []string) []string {
	out := make([]string, 0, len(parent)+len(extra)+1)
	for _, kv := range parent {
		name, _, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(strings.ToUpper(name), "AGENT_DESKTOP_") {
			continue
		}
		out = append(out, kv)
	}
	if home != "" {
		out = append(out, "AGENT_DESKTOP_HOME="+home)
	}
	return append(out, extra...)
}

// capped keeps up to max bytes and notes that more came.
type capped struct {
	buf  bytes.Buffer
	max  int
	over bool
}

// Write never fails: a writer that failed would leave the child blocked on a full pipe until its
// deadline, so what is past the cap is read and dropped instead.
func (c *capped) Write(p []byte) (int, error) {
	if c.over || c.buf.Len()+len(p) > c.max {
		c.over = true
		return len(p), nil
	}
	return c.buf.Write(p)
}

// BinaryName is the tool's file name on this system.
func BinaryName() string {
	if runtime.GOOS == "windows" {
		return "agent-desktop.exe"
	}
	return "agent-desktop"
}
