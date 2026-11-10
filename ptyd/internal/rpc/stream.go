package rpc

import (
	"bytes"
	"context"
	"encoding/json"
	"sync"
	"sync/atomic"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
	"github.com/ascorblack/daedalus/ptyd/sidechan"
)

// StreamVersion is the version of exec.stream this daemon speaks, in daemon.info's capabilities
// (exec_stream); 0, or the field missing, is a daemon without it. The host reads it before it starts
// an MCP server on this machine, so an old daemon is named as old instead of failing as an unknown
// method halfway through a connect.
const StreamVersion = 1

// The tags of the frames an exec.stream channel carries from the daemon: the first byte of every
// frame says what the rest is. The host sends untagged bytes, all of them the program's stdin.
const (
	streamStdout byte = 'o'
	streamStderr byte = 'e'
	// streamExit is the last frame before the daemon closes the channel: the program's end as JSON
	// ({exit_code, signal, duration_ms}), after the last of its output.
	streamExit byte = 'x'
)

// execStream starts a program with its stdin, stdout and stderr on one channel of this connection,
// for a program the host talks to for as long as it runs: an MCP server on the operator's machine.
// Closing the channel from the host ends the program (stdin closed, then its group hung up, then
// killed), and so does the connection ending, so a host that restarts or dies leaves none behind.
func (d *Daemon) execStream(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Argv []string          `json:"argv"`
		Cwd  string            `json:"cwd"`
		Env  map[string]string `json:"env"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Argv) > maxArgs || len(p.Env) > maxEnvVars {
		return nil, wire.Errorf(wire.CodeInvalidParams, "too many arguments or environment variables")
	}
	s := &procStream{c: c, queue: make(chan []byte, dialQueueFrames), done: make(chan struct{}), named: make(chan struct{})}
	s.id = c.OpenChannel(s)
	proc, err := d.Side.Exec.Stream(sidechan.StreamRequest{Argv: p.Argv, Cwd: p.Cwd, Env: p.Env},
		sidechan.StreamOutput{Out: s.output(streamStdout), Err: s.output(streamStderr)})
	if err != nil {
		c.DiscardChannel(s.id)
		return nil, sideError(err)
	}
	s.setProc(proc)
	return server.Then{Result: map[string]any{"channel": s.id, "pid": proc.Pid, "path": proc.Path}, After: func() {
		// The output waits for this: no byte of the stream reaches the host before the channel it
		// belongs to has been named.
		close(s.named)
		go s.write()
		go s.finish()
	}}, nil
}

// procStream carries one exec.stream over a channel. Frames from the host are queued (the
// connection's reader must never wait on a program that is slow to read its input) and written to
// the program's stdin in order.
type procStream struct {
	c      *server.Conn
	id     uint32
	queue  chan []byte
	queued atomic.Int64
	done   chan struct{}
	named  chan struct{}
	stop   sync.Once
	peer   atomic.Bool // the host closed the channel

	mu   sync.Mutex
	proc *sidechan.Stream
	gone bool // the host closed the channel before the program had started
}

func (s *procStream) setProc(proc *sidechan.Stream) {
	s.mu.Lock()
	s.proc = proc
	gone := s.gone
	s.mu.Unlock()
	if gone {
		go proc.End()
	}
}

// output is a stream's writer for one tag: each piece of output becomes one tagged frame.
func (s *procStream) output(tag byte) func([]byte) error {
	return func(p []byte) error {
		select {
		case <-s.named:
		case <-s.done:
			return server.ErrChannelClosed
		}
		for len(p) > 0 {
			n := min(len(p), wire.MaxPayload-1)
			frame := make([]byte, 0, n+1)
			frame = append(append(frame, tag), p[:n]...)
			if err := s.c.SendChannel(s.id, frame); err != nil {
				return err
			}
			p = p[n:]
		}
		return nil
	}
}

// Frame is bytes from the host for the program's stdin.
func (s *procStream) Frame(p []byte) {
	if s.queued.Add(int64(len(p))) > config.StreamStdinBytes {
		// The program stopped reading its input. Ending it tells the host at once; holding more
		// would let one stuck server grow the daemon without bound.
		s.end()
		return
	}
	select {
	case s.queue <- bytes.Clone(p):
	case <-s.done:
	default:
		s.end()
	}
}

// Closed is the host closing the channel, or the connection ending: either way the program ends.
func (s *procStream) Closed() {
	s.peer.Store(true)
	s.end()
}

// end stops the stdin writer and ends the program, in the background: it is called from the
// connection's reader, which must not wait the grace a program is given.
func (s *procStream) end() {
	s.stop.Do(func() {
		close(s.done)
		s.mu.Lock()
		proc := s.proc
		if proc == nil {
			s.gone = true
		}
		s.mu.Unlock()
		if proc != nil {
			go proc.End()
		}
	})
}

func (s *procStream) write() {
	for {
		select {
		case p := <-s.queue:
			s.queued.Add(-int64(len(p)))
			if err := s.proc.Write(p); err != nil {
				// The program closed its stdin or exited; its exit, not this, ends the channel.
				return
			}
		case <-s.done:
			return
		}
	}
}

// finish waits for the program's end and tells the host how it ended, after the last of its output,
// then closes the channel unless the host closed it first.
func (s *procStream) finish() {
	res := s.proc.Result()
	if !s.peer.Load() {
		body, _ := json.Marshal(map[string]any{"exit_code": res.ExitCode, "signal": res.Signal, "duration_ms": res.DurationMs})
		_ = s.c.SendChannel(s.id, append([]byte{streamExit}, body...))
		s.c.CloseChannel(s.id)
	}
	s.stop.Do(func() { close(s.done) })
}
