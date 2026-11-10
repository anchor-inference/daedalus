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
	"sync"
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
	streams chan struct{}
	tether  Tether

	mu      sync.Mutex
	running map[*Stream]struct{}
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
	return &Exec{allow: allow, environ: environ, home: home, log: log, slots: make(chan struct{}, config.MaxExecConcurrent),
		streams: make(chan struct{}, config.MaxExecStreams), running: map[*Stream]struct{}{}}
}

// NewOpenExec returns a runner without a program list, for daedalus-cli: there the operator's own
// guard, on the machine the program runs on, decides what runs, and a list would only be a second
// answer to the same question.
func NewOpenExec(environ []string, home string, log *slog.Logger) *Exec {
	return &Exec{environ: environ, home: home, log: log, slots: make(chan struct{}, config.MaxExecConcurrent),
		streams: make(chan struct{}, config.MaxExecStreams), running: map[*Stream]struct{}{}}
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
	// HandedOver says the program was still running when its hand-over time came (RunOrHandOver)
	// and goes on as a Job: ExitCode is -1 and the output is what it had written by then.
	HandedOver bool `json:"handed_over,omitempty"`
	Pid        int  `json:"pid,omitempty"`
}

// HandOver is how RunOrHandOver keeps a program instead of waiting for it.
type HandOver struct {
	// After is how long the call waits for the program before it hands it over; zero or less hands
	// it over as soon as it has started, which is how a background job is started.
	After time.Duration
	// Log receives what the program wrote before the hand-over and everything it writes after. It is
	// written under a lock from the program's output goroutines and must not block for long; a
	// failed write is dropped, never passed back to the program.
	Log io.Writer
}

// Job is a program RunOrHandOver handed over: it runs on after the call that started it returned,
// still in its own group and still tied to the runner, and its output goes to the HandOver's Log.
type Job struct {
	Pid     int
	Started time.Time

	group  *procGroup
	done   chan struct{}
	result ExecResult
}

// Done is closed once the program has ended and its output has been written.
func (j *Job) Done() <-chan struct{} { return j.done }

// Result is how the program ended: ExitCode, Signal and DurationMs; valid once Done is closed.
func (j *Job) Result() ExecResult {
	<-j.done
	return j.result
}

// Stop ends the program's group the way a timeout does, the hangup and the kill after a grace, and
// returns once it has ended.
func (j *Job) Stop() {
	select {
	case <-j.done:
		return
	default:
	}
	j.group.signal(false)
	select {
	case <-j.done:
		return
	case <-time.After(killGrace):
	}
	j.group.signal(true)
	<-j.done
}

// killGrace is the time between the hangup and the kill of a program that ran out of time.
const killGrace = 2 * time.Second

// Run runs one program, not in a terminal, in its own process group, and returns its output. It
// ends the whole group when the time runs out or ctx ends (the host went away), so an installer's
// children do not outlive the call.
func (e *Exec) Run(ctx context.Context, r ExecRequest) (ExecResult, error) {
	res, _, err := e.run(ctx, r, nil)
	return res, err
}

// RunOrHandOver runs one program as Run does but never ends it for time: a program still running
// after h.After is handed over, and the call returns at that moment with the output so far,
// HandedOver set and the Job that goes on. r.Timeout is not used. Cancelling ctx before the
// hand-over ends the program, as it does in Run; after it, only Job.Stop does. A terminal client
// keeps the long build the agent started in the foreground this way, instead of killing it at the
// end of the call and leaving it to be run again from the start.
func (e *Exec) RunOrHandOver(ctx context.Context, r ExecRequest, h HandOver) (ExecResult, *Job, error) {
	if h.Log == nil {
		return ExecResult{}, nil, fmt.Errorf("%w: a hand-over needs a log", ErrInvalid)
	}
	return e.run(ctx, r, &h)
}

func (e *Exec) run(ctx context.Context, r ExecRequest, h *HandOver) (ExecResult, *Job, error) {
	if len(r.Argv) == 0 || r.Argv[0] == "" {
		return ExecResult{}, nil, fmt.Errorf("%w: argv is empty", ErrInvalid)
	}
	if r.Timeout <= 0 {
		r.Timeout = config.DefaultExecTimeout
	}
	if r.Timeout > config.MaxExecTimeout {
		return ExecResult{}, nil, fmt.Errorf("%w: timeout_ms is at most %d", ErrInvalid, config.MaxExecTimeout.Milliseconds())
	}
	if r.MaxOutput <= 0 {
		r.MaxOutput = config.DefaultExecOutput
	}
	if r.MaxOutput > config.MaxExecOutput {
		return ExecResult{}, nil, fmt.Errorf("%w: max_output is at most %d", ErrInvalid, config.MaxExecOutput)
	}
	if len(r.Stdin) > config.MaxExecStdin {
		return ExecResult{}, nil, fmt.Errorf("%w: stdin is at most %d bytes", ErrInvalid, config.MaxExecStdin)
	}
	path, cwd, env, err := e.resolve(r.Argv, r.Cwd, r.Env, "exec.run")
	if err != nil {
		return ExecResult{}, nil, err
	}

	// The slot is held for the call, not for a job it hands over: a job counts against nothing here,
	// as a background job never did, and holding the slot would leave a client with a few kept
	// builds unable to run a single command.
	select {
	case e.slots <- struct{}{}:
		defer func() { <-e.slots }()
	default:
		return ExecResult{}, nil, fmt.Errorf("%w: %d programs are running already", ErrBusy, config.MaxExecConcurrent)
	}

	stdout, stderr := &capped{max: r.MaxOutput}, &capped{max: r.MaxOutput}
	cmd := &exec.Cmd{Path: path, Args: r.Argv, Dir: cwd, Env: env, Stdout: stdout, Stderr: stderr}
	if r.Output != nil {
		cmd.Stdout, cmd.Stderr = io.MultiWriter(stdout, r.Output), io.MultiWriter(stderr, r.Output)
	}
	var streams *handOverStreams
	if h != nil {
		streams = &handOverStreams{}
		cmd.Stdout, cmd.Stderr = streams.writer(cmd.Stdout), streams.writer(cmd.Stderr)
	}
	if r.Stdin != nil {
		cmd.Stdin = bytes.NewReader(r.Stdin)
	}
	group := newGroup(cmd, e.tether != nil)
	kept := false
	defer func() {
		if !kept {
			group.release()
		}
	}()
	// A daemonised grandchild that keeps the pipes open must not keep the call open after the
	// program itself has exited.
	cmd.WaitDelay = killGrace
	start := time.Now()
	started := make(chan error, 1)
	waited := make(chan error, 1)
	go func() {
		if e.tether != nil && parentDeathPerThread {
			// The kernel's parent-death signal follows the thread that started the child, not the
			// process: a thread the runtime retired while the program ran would kill it then. The
			// goroutine that starts the program holds its thread until the program has been waited
			// for, which keeps that thread alive and every other goroutine off it, and lasts as long
			// as a job the call handed over does, not only as long as the call.
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()
		}
		if err := cmd.Start(); err != nil {
			started <- err
			return
		}
		started <- nil
		waited <- cmd.Wait()
	}()
	if err := <-started; err != nil {
		return ExecResult{}, nil, fmt.Errorf("starting %s: %w", filepath.Base(path), err)
	}
	group.started()
	unhold := func() {}
	if e.tether != nil {
		unhold = e.tether.Hold(cmd.Process.Pid)
	}
	defer func() {
		if !kept {
			unhold()
		}
	}()

	timedOut := false
	var werr error
	if h != nil && h.After <= 0 {
		kept = true
		return e.handOver(h, streams, stdout, stderr, cmd, path, group, unhold, waited, start)
	}
	var deadline, handOverAt <-chan time.Time
	if h != nil {
		timer := time.NewTimer(h.After)
		defer timer.Stop()
		handOverAt = timer.C
	} else {
		timer := time.NewTimer(r.Timeout)
		defer timer.Stop()
		deadline = timer.C
	}
	select {
	case werr = <-waited:
	case <-deadline:
		timedOut = true
		werr = endGroup(group, waited)
	case <-ctx.Done():
		werr = endGroup(group, waited)
	case <-handOverAt:
		kept = true
		return e.handOver(h, streams, stdout, stderr, cmd, path, group, unhold, waited, start)
	}
	if streams != nil {
		streams.mu.Lock()
		defer streams.mu.Unlock()
	}
	res := ExecResult{ExitCode: -1, Stdout: stdout.String(), Stderr: stderr.String(),
		Truncated: stdout.cut || stderr.cut, TimedOut: timedOut, DurationMs: time.Since(start).Milliseconds(), Path: path}
	if cmd.ProcessState != nil {
		res.ExitCode = cmd.ProcessState.ExitCode()
		res.Signal = signalOf(cmd.ProcessState)
	}
	if werr != nil && cmd.ProcessState == nil {
		return ExecResult{}, nil, fmt.Errorf("running %s: %w", filepath.Base(path), werr)
	}
	if fitReply(&res.Stdout, &res.Stderr, config.MaxReplyData) {
		res.Truncated = true
	}
	e.log.Info("exec.run", "program", filepath.Base(path), "args", len(r.Argv)-1, "cwd", cwd,
		"exit_code", res.ExitCode, "signal", res.Signal, "timed_out", timedOut, "ms", res.DurationMs)
	return res, nil, nil
}

// resolve checks what a program is to run with, for exec.run and exec.stream alike: an absolute
// working directory that exists, environment names that are names, and a program PATH finds and the
// list allows. It returns the program's path, its working directory and its whole environment.
func (e *Exec) resolve(argv []string, cwd string, vars map[string]string, method string) (string, string, []string, error) {
	if len(argv) == 0 || argv[0] == "" {
		return "", "", nil, fmt.Errorf("%w: argv is empty", ErrInvalid)
	}
	if cwd == "" {
		cwd = e.home
	}
	if !filepath.IsAbs(cwd) {
		return "", "", nil, fmt.Errorf("%w: cwd must be absolute", ErrInvalid)
	}
	if st, err := os.Stat(cwd); err != nil || !st.IsDir() {
		return "", "", nil, fmt.Errorf("%w: cwd %s is not a directory", ErrInvalid, cwd)
	}
	for k := range vars {
		if k == "" || strings.ContainsAny(k, "=\x00") {
			return "", "", nil, fmt.Errorf("%w: environment name %q", ErrInvalid, k)
		}
	}
	extra := map[string]string{"HOME": e.home}
	for k, v := range vars {
		extra[k] = v
	}
	procenv.SetPWD(extra, cwd)
	env := dropVar(procenv.BuildEnv(e.environ, nil, extra, ""), "DAEDALUS_TERMINAL_ID")
	path, ok := procenv.LookPath(argv[0], env, cwd)
	if !ok {
		return "", "", nil, fmt.Errorf("%w: %q is not an executable on PATH", ErrNotFound, argv[0])
	}
	if e.allow != nil && !e.allow[ProgramName(path, runtime.GOOS)] {
		return "", "", nil, fmt.Errorf("%w: %s is not among the programs %s may run", ErrForbidden, filepath.Base(path), method)
	}
	return path, cwd, env, nil
}

// handOver turns a running program into a Job: what it wrote so far goes to the log and into the
// answer, its streams go to the log from here on, and a goroutine of the job waits for its end and
// only then lets go of its group and its tether.
func (e *Exec) handOver(h *HandOver, streams *handOverStreams, stdout, stderr *capped, cmd *exec.Cmd, path string,
	group *procGroup, unhold func(), waited <-chan error, start time.Time) (ExecResult, *Job, error) {
	streams.mu.Lock()
	out, errOut, cut := stdout.String(), stderr.String(), stdout.cut || stderr.cut
	_, _ = io.WriteString(h.Log, out)
	_, _ = io.WriteString(h.Log, errOut)
	if cut {
		_, _ = io.WriteString(h.Log, "\n[... output past the first part was not kept before this went to the background ...]\n")
	}
	streams.log = h.Log
	streams.mu.Unlock()

	job := &Job{Pid: cmd.Process.Pid, Started: start, group: group, done: make(chan struct{})}
	res := ExecResult{ExitCode: -1, Stdout: out, Stderr: errOut, Truncated: cut,
		DurationMs: time.Since(start).Milliseconds(), Path: path, HandedOver: true, Pid: job.Pid}
	if fitReply(&res.Stdout, &res.Stderr, config.MaxReplyData) {
		res.Truncated = true
	}
	e.log.Info("exec.run handed over", "program", filepath.Base(path), "pid", job.Pid, "ms", res.DurationMs)
	go func() {
		<-waited
		end := ExecResult{ExitCode: -1, DurationMs: time.Since(start).Milliseconds(), Path: path}
		if cmd.ProcessState != nil {
			end.ExitCode = cmd.ProcessState.ExitCode()
			end.Signal = signalOf(cmd.ProcessState)
		}
		group.release()
		unhold()
		job.result = end
		e.log.Info("exec.run job ended", "program", filepath.Base(path), "pid", job.Pid, "exit_code", end.ExitCode, "signal", end.Signal, "ms", end.DurationMs)
		close(job.done)
	}()
	return res, job, nil
}

// handOverStreams carries a program's stdout and stderr to where they go before a hand-over (the
// answer's buffers) and, from the moment of it, to the job's log. One lock serves both streams, so
// a byte is either in the answer and copied into the log, or written to the log after it: none is
// lost in between and none is written twice.
type handOverStreams struct {
	mu  sync.Mutex
	log io.Writer
}

func (s *handOverStreams) writer(before io.Writer) io.Writer {
	return handOverStream{s: s, before: before}
}

type handOverStream struct {
	s      *handOverStreams
	before io.Writer
}

func (w handOverStream) Write(p []byte) (int, error) {
	w.s.mu.Lock()
	defer w.s.mu.Unlock()
	if w.s.log != nil {
		_, _ = w.s.log.Write(p)
		return len(p), nil
	}
	return w.before.Write(p)
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
