package sidechan

import (
	"fmt"
	"io"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// StreamRequest is one exec.stream: a program kept running with its standard streams bridged to the
// host for as long as the host holds it. An MCP server the operator configured to run on this machine
// is the one use: the host speaks the protocol over the program's stdin and stdout.
type StreamRequest struct {
	Argv []string
	Cwd  string
	Env  map[string]string
}

// StreamOutput is where a streamed program's output goes. Out and Err are each called from one
// goroutine of their own and may block: a host that reads slowly slows the program, as a full pipe
// would. An error from either drops what that stream writes from then on, so a program whose host
// went away is never stuck on a pipe nobody reads while it is being ended.
type StreamOutput struct {
	Out func(p []byte) error
	Err func(p []byte) error
}

// Stream is one program exec.stream started. It runs in its own process group (a job object on
// Windows), tied to the daemon where the system allows it, and nothing of it outlives End: the group
// is swept once the program's own process has exited, so a child it left behind is ended with it.
type Stream struct {
	Pid     int
	Path    string
	Started time.Time

	stdin   io.WriteCloser
	group   *procGroup
	done    chan struct{}
	result  ExecResult
	closing sync.Once
}

// Done is closed once the program and everything in its group have ended and its output is written.
func (s *Stream) Done() <-chan struct{} { return s.done }

// Result is how the program ended: ExitCode, Signal and DurationMs; valid once Done is closed.
func (s *Stream) Result() ExecResult {
	<-s.done
	return s.result
}

// Write gives the program bytes on its stdin. It blocks while the program is not reading, so the
// caller writes from a goroutine of its own, never from a connection's reader.
func (s *Stream) Write(p []byte) error {
	_, err := s.stdin.Write(p)
	return err
}

// streamGrace is how long a program is given to exit by itself once its stdin is closed, before its
// group is hung up. The MCP lifecycle asks a client to close the server's input first and wait: a
// server that saves state on the way out must get the chance.
const streamGrace = 2 * time.Second

// End ends the program the way the MCP lifecycle says a client should: its stdin is closed, then its
// group is hung up after a grace, then killed after another. It returns once the program has ended,
// and may be called any number of times, from any goroutine.
func (s *Stream) End() {
	s.closing.Do(func() { _ = s.stdin.Close() })
	select {
	case <-s.done:
		return
	case <-time.After(streamGrace):
	}
	s.group.signal(false)
	select {
	case <-s.done:
		return
	case <-time.After(killGrace):
	}
	s.group.signal(true)
	<-s.done
}

// Stream starts a program for exec.stream. The checks are exec.run's (the program list, the working
// directory, the environment); what differs is that nothing ends it but End, the program's own exit,
// or the daemon's: there is no timeout, since a server runs for as long as the host uses it.
func (e *Exec) Stream(r StreamRequest, out StreamOutput) (*Stream, error) {
	path, cwd, env, err := e.resolve(r.Argv, r.Cwd, r.Env, "exec.stream")
	if err != nil {
		return nil, err
	}
	select {
	case e.streams <- struct{}{}:
	default:
		return nil, fmt.Errorf("%w: %d streamed programs are running already", ErrBusy, config.MaxExecStreams)
	}
	held := true
	defer func() {
		if held {
			<-e.streams
		}
	}()

	cmd := &exec.Cmd{Path: path, Args: r.Argv, Dir: cwd, Env: env,
		Stdout: &droppingWriter{send: out.Out}, Stderr: &droppingWriter{send: out.Err}}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("starting %s: %w", filepath.Base(path), err)
	}
	// Tied, unlike exec.run's programs: a terminal's tool call may outlive a daemon restart, but a
	// streamed program has nobody left to talk to once the daemon that carried its streams is gone.
	group := newGroup(cmd, true)
	// A daemonised grandchild that keeps stdout open must not keep the stream open after the
	// program itself has exited; the sweep below then ends that grandchild.
	cmd.WaitDelay = killGrace
	s := &Stream{Path: path, stdin: stdin, group: group, done: make(chan struct{})}
	started := make(chan error, 1)
	// ready holds the end back until the caller has put the program in its job and its table: a
	// program that exits at once must not be let go of before it was ever held.
	ready := make(chan struct{})
	go func() {
		if parentDeathPerThread {
			// As in run: the parent-death signal follows the thread that started the child, so that
			// thread is held until the program has been waited for.
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()
		}
		if err := cmd.Start(); err != nil {
			started <- err
			return
		}
		s.Started, s.Pid = time.Now(), cmd.Process.Pid
		started <- nil
		_ = cmd.Wait()
		<-ready
		e.ended(s, cmd)
	}()
	if err := <-started; err != nil {
		group.release()
		return nil, fmt.Errorf("starting %s: %w", filepath.Base(path), err)
	}
	group.started()
	held = false
	e.mu.Lock()
	e.running[s] = struct{}{}
	e.mu.Unlock()
	close(ready)
	e.log.Info("exec.stream", "program", filepath.Base(path), "args", len(r.Argv)-1, "cwd", cwd, "pid", s.Pid)
	return s, nil
}

// ended is what follows a streamed program's exit: what is left of its group is killed, the job let
// go of, the slot given back, and Done closed.
func (e *Exec) ended(s *Stream, cmd *exec.Cmd) {
	// The leader is gone; a child it started and left (a server's own helper, a daemonised
	// grandchild) is still in the group, and is ended here rather than left running unseen.
	s.group.signal(true)
	s.group.release()
	res := ExecResult{ExitCode: -1, Path: s.Path, DurationMs: time.Since(s.Started).Milliseconds(), Pid: s.Pid}
	if cmd.ProcessState != nil {
		res.ExitCode = cmd.ProcessState.ExitCode()
		res.Signal = signalOf(cmd.ProcessState)
	}
	s.result = res
	e.mu.Lock()
	delete(e.running, s)
	e.mu.Unlock()
	<-e.streams
	e.log.Info("exec.stream ended", "program", filepath.Base(s.Path), "pid", s.Pid, "exit_code", res.ExitCode, "signal", res.Signal, "ms", res.DurationMs)
	close(s.done)
}

// EndStreams ends every streamed program at once and returns when all have ended: the daemon's
// shutdown, which must not leave one behind on a system where nothing ties it to the daemon.
func (e *Exec) EndStreams() {
	e.mu.Lock()
	all := make([]*Stream, 0, len(e.running))
	for s := range e.running {
		all = append(all, s)
	}
	e.mu.Unlock()
	var wg sync.WaitGroup
	for _, s := range all {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.End()
		}()
	}
	wg.Wait()
}

// Streams is the number of streamed programs alive now, for daemon.info and the tests.
func (e *Exec) Streams() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.running)
}

// droppingWriter passes a program's output on until passing it fails, and then reads it away, so
// the program is never blocked on a pipe whose reader has gone.
type droppingWriter struct {
	send   func([]byte) error
	failed bool
}

func (w *droppingWriter) Write(p []byte) (int, error) {
	if !w.failed && w.send != nil {
		if err := w.send(p); err != nil {
			w.failed = true
		}
	}
	return len(p), nil
}
