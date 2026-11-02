package sidechan

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
)

// Errors every side channel may answer with; the RPC layer gives each its code.
var (
	ErrInvalid   = errors.New("invalid request")
	ErrForbidden = errors.New("forbidden")
	ErrNotFound  = errors.New("not found")
	ErrBusy      = errors.New("too many at once")
)

// Exec runs programs for their output.
type Exec struct {
	allow   map[string]bool
	environ []string
	home    string
	log     *slog.Logger
	slots   chan struct{}
	tether  Tether
}

// Tether ties the programs a runner starts to the life of the process that runs them, for a runner
// that may be killed outright: daedalus-cli under `kill -9` left the commands it had started running
// on, unseen, with the server reporting their outcome as unknown. Hold is told of each program's
// process group (on Unix the leader's pid) as soon as it has started, and the function it returns
// is called once the program has ended.
type Tether interface {
	Hold(pid int) (release func())
}

// Tie makes every program this runner starts from now on die with this process: on Linux the kernel
// kills the group's leader when its parent dies, and the tether is there to end the rest of the
// group. Without a tie, a program outlives a runner that dies, as ptyd's have always done, since a
// daemon restart must not take a terminal's tool calls with it. On Windows each program's job
// object already ends its tree when the runner's handle to it closes, which dying does.
func (e *Exec) Tie(t Tether) { e.tether = t }

// NewExec returns a runner that allows the default programs plus extra, in the environment
// terminals inherit.
func NewExec(extra []string, environ []string, home string, log *slog.Logger) *Exec {
	allow := map[string]bool{}
	for _, name := range append(append([]string{}, DefaultExecAllow...), extra...) {
		allow[name] = true
	}
	return &Exec{allow: allow, environ: environ, home: home, log: log, slots: make(chan struct{}, config.MaxExecConcurrent)}
}

// NewOpenExec returns a runner without a program list, for daedalus-cli: there the operator's own
// guard, on the machine the program runs on, decides what runs, and a list would only be a second
// answer to the same question.
func NewOpenExec(environ []string, home string, log *slog.Logger) *Exec {
	return &Exec{environ: environ, home: home, log: log, slots: make(chan struct{}, config.MaxExecConcurrent)}
}

// Allowed is the allowlist, sorted, for daemon.info; nil for a runner without one.
func (e *Exec) Allowed() []string {
	if e.allow == nil {
		return nil
	}
	out := make([]string, 0, len(e.allow))
	for name := range e.allow {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// ExecRequest is one exec.run.
type ExecRequest struct {
	Argv      []string
	Cwd       string
	Env       map[string]string
	Timeout   time.Duration
	Stdin     []byte
	MaxOutput int
	// Output, when set, also receives the program's output as it is written, stdout and stderr
	// alike, for a caller that shows a long command's progress. It is written from two goroutines
	// and must not block: the program waits on it.
	Output io.Writer
}

// ExecResult is what a program did. ExitCode is -1 when it ended by a signal, named in Signal.
type ExecResult struct {
	ExitCode   int    `json:"exit_code"`
	Signal     string `json:"signal"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	Truncated  bool   `json:"truncated"`
	TimedOut   bool   `json:"timed_out"`
	DurationMs int64  `json:"duration_ms"`
	Path       string `json:"path"`
}

// killGrace is the time between the hangup and the kill of a program that ran out of time.
const killGrace = 2 * time.Second

// Run runs one program, not in a terminal, in its own process group, and returns its output. It
// ends the whole group when the time runs out or ctx ends (the host went away), so an installer's
// children do not outlive the call.
func (e *Exec) Run(ctx context.Context, r ExecRequest) (ExecResult, error) {
	if len(r.Argv) == 0 || r.Argv[0] == "" {
		return ExecResult{}, fmt.Errorf("%w: argv is empty", ErrInvalid)
	}
	if r.Timeout <= 0 {
		r.Timeout = config.DefaultExecTimeout
	}
	if r.Timeout > config.MaxExecTimeout {
		return ExecResult{}, fmt.Errorf("%w: timeout_ms is at most %d", ErrInvalid, config.MaxExecTimeout.Milliseconds())
	}
	if r.MaxOutput <= 0 {
		r.MaxOutput = config.DefaultExecOutput
	}
	if r.MaxOutput > config.MaxExecOutput {
		return ExecResult{}, fmt.Errorf("%w: max_output is at most %d", ErrInvalid, config.MaxExecOutput)
	}
	if len(r.Stdin) > config.MaxExecStdin {
		return ExecResult{}, fmt.Errorf("%w: stdin is at most %d bytes", ErrInvalid, config.MaxExecStdin)
	}
	cwd := r.Cwd
	if cwd == "" {
		cwd = e.home
	}
	if !filepath.IsAbs(cwd) {
		return ExecResult{}, fmt.Errorf("%w: cwd must be absolute", ErrInvalid)
	}
	if st, err := os.Stat(cwd); err != nil || !st.IsDir() {
		return ExecResult{}, fmt.Errorf("%w: cwd %s is not a directory", ErrInvalid, cwd)
	}
	for k := range r.Env {
		if k == "" || strings.ContainsAny(k, "=\x00") {
			return ExecResult{}, fmt.Errorf("%w: environment name %q", ErrInvalid, k)
		}
	}
	extra := map[string]string{"HOME": e.home}
	for k, v := range r.Env {
		extra[k] = v
	}
	procenv.SetPWD(extra, cwd)
	env := dropVar(procenv.BuildEnv(e.environ, nil, extra, ""), "DAEDALUS_TERMINAL_ID")
	path, ok := procenv.LookPath(r.Argv[0], env, cwd)
	if !ok {
		return ExecResult{}, fmt.Errorf("%w: %q is not an executable on PATH", ErrNotFound, r.Argv[0])
	}
	if e.allow != nil && !e.allow[ProgramName(path, runtime.GOOS)] {
		return ExecResult{}, fmt.Errorf("%w: %s is not among the programs exec.run may run", ErrForbidden, filepath.Base(path))
	}

	select {
	case e.slots <- struct{}{}:
		defer func() { <-e.slots }()
	default:
		return ExecResult{}, fmt.Errorf("%w: %d programs are running already", ErrBusy, config.MaxExecConcurrent)
	}

	stdout, stderr := &capped{max: r.MaxOutput}, &capped{max: r.MaxOutput}
	cmd := &exec.Cmd{Path: path, Args: r.Argv, Dir: cwd, Env: env, Stdout: stdout, Stderr: stderr}
	if r.Output != nil {
		cmd.Stdout, cmd.Stderr = io.MultiWriter(stdout, r.Output), io.MultiWriter(stderr, r.Output)
	}
	if r.Stdin != nil {
		cmd.Stdin = bytes.NewReader(r.Stdin)
	}
	group := newGroup(cmd, e.tether != nil)
	defer group.release()
	if e.tether != nil && parentDeathPerThread {
		// The kernel's parent-death signal follows the thread that started the child, not the
		// process: a thread the runtime retired while the program ran would kill it then. Holding
		// this goroutine on its thread until the program has been waited for keeps that thread
		// alive and keeps every other goroutine off it.
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
	}
	// A daemonised grandchild that keeps the pipes open must not keep the call open after the
	// program itself has exited.
	cmd.WaitDelay = killGrace
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return ExecResult{}, fmt.Errorf("starting %s: %w", filepath.Base(path), err)
	}
	group.started()
	if e.tether != nil {
		defer e.tether.Hold(cmd.Process.Pid)()
	}
	waited := make(chan error, 1)
	go func() { waited <- cmd.Wait() }()

	timedOut := false
	timer := time.NewTimer(r.Timeout)
	defer timer.Stop()
	var werr error
	select {
	case werr = <-waited:
	case <-timer.C:
		timedOut = true
		werr = endGroup(group, waited)
	case <-ctx.Done():
		werr = endGroup(group, waited)
	}
	res := ExecResult{ExitCode: -1, Stdout: stdout.String(), Stderr: stderr.String(),
		Truncated: stdout.cut || stderr.cut, TimedOut: timedOut, DurationMs: time.Since(start).Milliseconds(), Path: path}
	if cmd.ProcessState != nil {
		res.ExitCode = cmd.ProcessState.ExitCode()
		res.Signal = signalOf(cmd.ProcessState)
	}
	if werr != nil && cmd.ProcessState == nil {
		return ExecResult{}, fmt.Errorf("running %s: %w", filepath.Base(path), werr)
	}
	if fitReply(&res.Stdout, &res.Stderr, config.MaxReplyData) {
		res.Truncated = true
	}
	e.log.Info("exec.run", "program", filepath.Base(path), "args", len(r.Argv)-1, "cwd", cwd,
		"exit_code", res.ExitCode, "signal", res.Signal, "timed_out", timedOut, "ms", res.DurationMs)
	return res, nil
}

// endGroup hangs up the program's group, kills it after the grace, and waits for it.
func endGroup(group *procGroup, waited <-chan error) error {
	group.signal(false)
	select {
	case err := <-waited:
		return err
	case <-time.After(killGrace):
	}
	group.signal(true)
	return <-waited
}

// ProgramName is the name a program is allowed by: the base name of its path, and on Windows
// without the extension that makes it runnable and in lower case, so "claude.cmd" and "Git.exe"
// are the "claude" and "git" of the list.
func ProgramName(path, goos string) string {
	if goos != "windows" {
		return filepath.Base(path)
	}
	name := path[strings.LastIndexAny(path, `/\`)+1:]
	lower := strings.ToLower(name)
	for _, ext := range []string{".exe", ".cmd", ".bat", ".com"} {
		if strings.HasSuffix(lower, ext) {
			return lower[:len(lower)-len(ext)]
		}
	}
	return lower
}

// capped keeps the first max bytes written to it and counts the rest away, so a program never
// blocks on a pipe nobody reads.
type capped struct {
	buf bytes.Buffer
	max int
	cut bool
}

func (c *capped) Write(p []byte) (int, error) {
	if room := c.max - c.buf.Len(); room > 0 {
		c.buf.Write(p[:min(room, len(p))])
	}
	if c.buf.Len()+len(p) > c.max {
		c.cut = true
	}
	return len(p), nil
}

// String is the output as text; bytes that are not UTF-8 become U+FFFD, since the reply is JSON.
func (c *capped) String() string { return strings.ToValidUTF8(c.buf.String(), "�") }

// fitReply shortens a and b until both, JSON-encoded, fit in budget bytes, taking from the longer
// first. It reports whether it cut anything. JSON escaping can make a control character six bytes,
// so the budget is measured encoded rather than assumed.
func fitReply(a, b *string, budget int) bool {
	cut := false
	for encodedLen(*a)+encodedLen(*b) > budget {
		longer := a
		if len(*b) > len(*a) {
			longer = b
		}
		*longer = cutUTF8(*longer, len(*longer)/2)
		cut = true
	}
	return cut
}

func encodedLen(s string) int {
	b, _ := json.Marshal(s)
	return len(b)
}

func cutUTF8(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && (s[n]&0xC0) == 0x80 {
		n--
	}
	return s[:n]
}

func dropVar(env []string, name string) []string {
	out := env[:0]
	for _, kv := range env {
		if !strings.HasPrefix(kv, name+"=") {
			out = append(out, kv)
		}
	}
	return out
}
