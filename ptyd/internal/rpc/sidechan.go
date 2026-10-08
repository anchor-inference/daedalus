package rpc

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/internal/sidechan"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Side is the daemon's side channels: what the harness adapters reach besides terminals.
type Side struct {
	Exec     *sidechan.Exec
	FS       *sidechan.FS
	Dialer   *sidechan.Dialer
	Launches *hooks.Registry
	Listen   string // the hook listener's address
	StateDir string
}

func (d *Daemon) registerSide(srv *server.Server) {
	if d.Side == nil {
		return
	}
	srv.Handle("exec.run", d.execRun)
	srv.Handle("fs.stat", d.fsStat)
	srv.Handle("fs.list", d.fsList)
	srv.Handle("fs.read", d.fsRead)
	srv.Handle("fs.tail", d.fsTail)
	srv.Handle("fs.set_roots", d.fsSetRoots)
	srv.Handle("fs.mkdir", d.fsMkdir)
	srv.Handle("fs.write", d.fsWrite)
	srv.Handle("net.dial", d.netDial)
	srv.Handle("net.allow", d.netAllow)
	srv.Handle("hooks.register_launch", d.registerLaunch)
	srv.Handle("hooks.unregister_launch", d.unregisterLaunch)
	srv.Handle("hooks.reply", d.hookReply)
	srv.Handle("hooks.put_file", d.putFile)
}

// hooksInfo and sideInfo are the side channels' part of daemon.info.
func (d *Daemon) hooksInfo() map[string]any {
	if d.Side == nil {
		return map[string]any{"listen": ""}
	}
	return map[string]any{"listen": d.Side.Listen, "launches": d.Side.Launches.Count(), "held": d.Side.Launches.Held()}
}

func (d *Daemon) sideInfo() map[string]any {
	if d.Side == nil {
		return map[string]any{}
	}
	return map[string]any{"exec_allow": d.Side.Exec.Allowed(), "fs_roots": d.Side.FS.Roots(), "state_dir": d.Side.StateDir}
}

// sideError gives the side channels' errors their protocol codes.
func sideError(err error) error {
	var we *wire.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, &we):
		return err
	case errors.Is(err, sidechan.ErrStaleLaunch), errors.Is(err, hooks.ErrNoLaunch):
		return wire.Errorf(wire.CodeStaleLaunch, "%v", err)
	case errors.Is(err, sidechan.ErrInvalid), errors.Is(err, hooks.ErrInvalid):
		return wire.Errorf(wire.CodeInvalidParams, "%v", err)
	case errors.Is(err, sidechan.ErrForbidden), errors.Is(err, hooks.ErrLaunchExists):
		return wire.Errorf(wire.CodeForbidden, "%v", err)
	case errors.Is(err, sidechan.ErrNotFound), errors.Is(err, hooks.ErrNoReply):
		return wire.Errorf(wire.CodeNotFound, "%v", err)
	case errors.Is(err, sidechan.ErrBusy), errors.Is(err, hooks.ErrTooMany), errors.Is(err, hooks.ErrStreams):
		return wire.Errorf(wire.CodeLimit, "%v", err)
	}
	return err
}

func (d *Daemon) execRun(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Argv      []string          `json:"argv"`
		Cwd       string            `json:"cwd"`
		Env       map[string]string `json:"env"`
		TimeoutMs int64             `json:"timeout_ms"`
		Stdin     []byte            `json:"stdin_b64"`
		MaxOutput int               `json:"max_output"`
		Resources *struct {
			Scope            json.RawMessage `json:"scope"`
			ExpectedInstance string          `json:"expected_instance"`
		} `json:"resources"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Argv) > maxArgs || len(p.Env) > maxEnvVars {
		return nil, wire.Errorf(wire.CodeInvalidParams, "too many arguments or environment variables")
	}
	if p.TimeoutMs < 0 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "timeout_ms must not be negative")
	}
	if p.Resources != nil {
		// A side-channel program would see the daemon's writable cgroupfs without the terminal
		// sandbox. Refuse it rather than let a worker process move itself out of the attempt group.
		return nil, wire.Errorf(wire.CodeForbidden, "strict attempts cannot run unsandboxed exec side channels")
	}
	res, err := d.Side.Exec.Run(ctx, sidechan.ExecRequest{Argv: p.Argv, Cwd: p.Cwd, Env: p.Env,
		Timeout: time.Duration(p.TimeoutMs) * time.Millisecond, Stdin: p.Stdin, MaxOutput: p.MaxOutput})
	if err != nil {
		return nil, sideError(err)
	}
	return res, nil
}

func (d *Daemon) fsStat(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path   string `json:"path"`
		AsRoot bool   `json:"as_root"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	stat := d.Side.FS.Stat
	if p.AsRoot {
		stat = d.Side.FS.StatRoot
	}
	st, err := stat(p.Path)
	if err != nil {
		d.logRefusal("fs.stat", p.Path, err)
		return nil, sideError(err)
	}
	return st, nil
}

func (d *Daemon) fsList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path  string `json:"path"`
		Glob  string `json:"glob"`
		Sort  string `json:"sort"`
		Limit int    `json:"limit"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	entries, truncated, err := d.Side.FS.List(p.Path, p.Glob, p.Sort, p.Limit)
	if err != nil {
		d.logRefusal("fs.list", p.Path, err)
		return nil, sideError(err)
	}
	if entries == nil {
		entries = []sidechan.Entry{}
	}
	return map[string]any{"entries": entries, "truncated": truncated}, nil
}

func (d *Daemon) fsRead(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path   string `json:"path"`
		Offset int64  `json:"offset"`
		Max    int    `json:"max"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	r, err := d.Side.FS.Read(p.Path, p.Offset, p.Max)
	if err != nil {
		d.logRefusal("fs.read", p.Path, err)
		return nil, sideError(err)
	}
	return r, nil
}

func (d *Daemon) fsTail(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path       string `json:"path"`
		FromOffset int64  `json:"from_offset"`
		Max        int    `json:"max"`
		FollowMs   int64  `json:"follow_ms"`
		FileID     string `json:"file_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.Side.FS.Tail(ctx, p.Path, p.FromOffset, p.Max, time.Duration(p.FollowMs)*time.Millisecond, p.FileID)
	if err != nil {
		d.logRefusal("fs.tail", p.Path, err)
		return nil, sideError(err)
	}
	return t, nil
}

func (d *Daemon) fsMkdir(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path string `json:"path"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	st, created, err := d.Side.FS.MkdirRoot(p.Path)
	if err != nil {
		d.logRefusal("fs.mkdir", p.Path, err)
		return nil, sideError(err)
	}
	d.Log.Info("fs.mkdir", "path", p.Path, "created", created)
	return struct {
		sidechan.Stat
		Created bool `json:"created"`
	}{st, created}, nil
}

// fsWrite is a file handed to a staff member, written into its inbox under a root: logged, as
// every write the side channels make is.
func (d *Daemon) fsWrite(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Path   string `json:"path"`
		Offset int64  `json:"offset"`
		Data   []byte `json:"data_b64"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	w, err := d.Side.FS.Write(p.Path, p.Offset, p.Data)
	if err != nil {
		d.logRefusal("fs.write", p.Path, err)
		return nil, sideError(err)
	}
	d.Log.Info("fs.write", "path", p.Path, "offset", p.Offset, "bytes", len(p.Data), "size", w.Size)
	return w, nil
}

func (d *Daemon) fsSetRoots(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Roots []string `json:"roots"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	accepted, refused, err := d.Side.FS.SetRoots(p.Roots)
	if err != nil {
		return nil, sideError(err)
	}
	for _, r := range refused {
		d.Log.Warn("fs root refused", "root", r.Root, "reason", r.Reason)
	}
	if accepted == nil {
		accepted = []string{}
	}
	if refused == nil {
		refused = []sidechan.Refusal{}
	}
	return map[string]any{"roots": d.Side.FS.Roots(), "accepted": accepted, "refused": refused}, nil
}

// logRefusal writes a refused read into the daemon's log: a read outside the roots or of a denied
// file is either a bug in an adapter or someone probing, and either is worth finding afterwards.
func (d *Daemon) logRefusal(method, path string, err error) {
	if errors.Is(err, sidechan.ErrForbidden) {
		d.Log.Warn("side channel refused", "method", method, "path", path, "reason", err.Error())
	}
}

func (d *Daemon) netAllow(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		LaunchID string `json:"launch_id"`
		Port     int    `json:"port"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := d.Side.Launches.Allow(p.LaunchID, p.Port); err != nil {
		return nil, sideError(err)
	}
	return map[string]any{}, nil
}

func (d *Daemon) netDial(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Target   string `json:"target"`
		LaunchID string `json:"launch_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	nc, _, release, err := d.Side.Dialer.Dial(p.Target, p.LaunchID)
	if err != nil {
		d.Log.Info("net.dial refused", "launch", p.LaunchID, "target", p.Target, "reason", err.Error())
		return nil, sideError(err)
	}
	s := &dialStream{c: c, nc: nc, release: release, queue: make(chan []byte, dialQueueFrames), done: make(chan struct{})}
	s.id = c.OpenChannel(s)
	d.Log.Info("net.dial", "launch", p.LaunchID, "target", p.Target, "channel", s.id)
	// The reader starts only after the reply, so no byte of the stream reaches the host before the
	// channel it belongs to has been named.
	return server.Then{Result: map[string]any{"channel": s.id}, After: func() {
		go s.write()
		go s.read()
	}}, nil
}

// dialQueueFrames bounds the frames a stream holds besides its byte bound, so a flood of tiny
// frames cannot grow the queue either.
const dialQueueFrames = 4096

// dialStream carries one net.dial connection over a channel. Frames from the host are queued (the
// connection's reader must never wait on one slow socket) and written in order; bytes from the
// socket are sent as they come, and a slow host only slows the socket, since sending waits.
type dialStream struct {
	c       *server.Conn
	id      uint32
	nc      net.Conn
	release func()
	queue   chan []byte
	queued  atomic.Int64
	done    chan struct{}
	stop    sync.Once
	peer    atomic.Bool // the host closed the channel
}

// Frame is a frame from the host: bytes for the socket.
func (s *dialStream) Frame(p []byte) {
	if s.queued.Add(int64(len(p))) > config.DialQueueBytes {
		// The program reads slower than the host writes. Closing tells the host at once; waiting
		// would hold up every other channel on the connection.
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

// Closed is the host closing the channel: what it sent before is still written, then the socket is
// closed.
func (s *dialStream) Closed() {
	s.peer.Store(true)
	select {
	case s.queue <- nil:
	default:
		s.end()
	}
}

func (s *dialStream) end() {
	s.stop.Do(func() {
		close(s.done)
		s.nc.Close()
	})
}

func (s *dialStream) write() {
	for {
		select {
		case p := <-s.queue:
			if p == nil {
				s.end()
				return
			}
			s.queued.Add(-int64(len(p)))
			if _, err := s.nc.Write(p); err != nil {
				s.end()
				return
			}
		case <-s.done:
			return
		}
	}
}

func (s *dialStream) read() {
	buf := make([]byte, 32<<10)
	for {
		n, err := s.nc.Read(buf)
		if n > 0 {
			// SendChannel fails once either side closed the channel, so no byte follows a close.
			if s.c.SendChannel(s.id, buf[:n]) != nil {
				break
			}
		}
		if err != nil {
			break
		}
	}
	s.end()
	s.release()
	if !s.peer.Load() {
		s.c.CloseChannel(s.id)
	}
}

func (d *Daemon) registerLaunch(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		LaunchID   string            `json:"launch_id"`
		TerminalID string            `json:"terminal_id"`
		Files      map[string][]byte `json:"files"`
		Ports      []int             `json:"ports"`
		HoldMaxMs  int64             `json:"hold_max_ms"`
		TTLs       int64             `json:"ttl_s"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.HoldMaxMs < 0 || time.Duration(p.HoldMaxMs)*time.Millisecond > config.MaxHookHold {
		return nil, wire.Errorf(wire.CodeInvalidParams, "hold_max_ms is 0..%d", config.MaxHookHold.Milliseconds())
	}
	if p.TTLs < 0 || time.Duration(p.TTLs)*time.Second > config.MaxLaunchTTL {
		return nil, wire.Errorf(wire.CodeInvalidParams, "ttl_s is 0..%d", int64(config.MaxLaunchTTL.Seconds()))
	}
	if len(p.Ports) > 64 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "at most 64 ports")
	}
	r, err := d.Side.Launches.Register(hooks.Spec{ID: p.LaunchID, TerminalID: p.TerminalID, Files: p.Files, Ports: p.Ports,
		HoldMax: time.Duration(p.HoldMaxMs) * time.Millisecond, TTL: time.Duration(p.TTLs) * time.Second})
	if err != nil {
		return nil, sideError(err)
	}
	return r, nil
}

func (d *Daemon) putFile(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		LaunchID string `json:"launch_id"`
		Name     string `json:"name"`
		Data     []byte `json:"data"`
		Replace  bool   `json:"replace"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	path, err := d.Side.Launches.PutFile(p.LaunchID, p.Name, p.Data, p.Replace)
	if err != nil {
		return nil, sideError(err)
	}
	return map[string]any{"path": path}, nil
}

func (d *Daemon) unregisterLaunch(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		LaunchID string `json:"launch_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return map[string]any{"removed": d.Side.Launches.Unregister(p.LaunchID, "unregistered")}, nil
}

func (d *Daemon) hookReply(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		LaunchID    string          `json:"launch_id"`
		ReplyID     string          `json:"reply_id"`
		Status      int             `json:"status"`
		Body        json.RawMessage `json:"body"`
		ContentType string          `json:"content_type"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.Status == 0 {
		p.Status = 200
	}
	if p.Status < 200 || p.Status > 599 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "status is 200..599")
	}
	reply := hooks.Reply{Status: p.Status, ContentType: p.ContentType}
	// A JSON string is the body's text; anything else JSON is the body as JSON.
	switch body := bytes.TrimSpace(p.Body); {
	case len(body) == 0 || string(body) == "null":
	case body[0] == '"':
		var text string
		if err := json.Unmarshal(body, &text); err != nil {
			return nil, wire.Errorf(wire.CodeInvalidParams, "body: %v", err)
		}
		reply.Body = []byte(text)
		if reply.ContentType == "" {
			reply.ContentType = "text/plain; charset=utf-8"
		}
	default:
		reply.Body = body
		if reply.ContentType == "" {
			reply.ContentType = "application/json"
		}
	}
	if len(reply.Body) > config.HookBodyBytes {
		return nil, wire.Errorf(wire.CodeInvalidParams, "the body is larger than a megabyte")
	}
	if err := d.Side.Launches.Reply(p.LaunchID, p.ReplyID, reply); err != nil {
		return nil, sideError(err)
	}
	return map[string]any{"delivered": true}, nil
}

// launchEnv adds a launch's variables to a terminal's environment. Without side channels (a build
// or test that has none) the launch id is only a label, as it was before them.
func (d *Daemon) launchEnv(launchID string, extra map[string]string) error {
	if d.Side == nil || launchID == "" {
		return nil
	}
	env, ok := d.Side.Launches.Env(launchID)
	if !ok {
		return wire.Errorf(wire.CodeStaleLaunch, "launch %s is not registered or has ended", launchID)
	}
	for k, v := range env {
		extra[k] = v
	}
	return nil
}

// launchStarted binds a terminal that started with a launch, and follows it to its exit, which
// starts the launch's grace. Binding only once the program runs means a start that fails leaves
// the launch as it was, for the host to try again.
func (d *Daemon) launchStarted(t *term.Terminal, launchID, terminalID string) {
	if d.Side == nil || launchID == "" || t == nil {
		return
	}
	if !d.Side.Launches.Bind(launchID, terminalID) {
		// Unregistered in the moment the program started: its posts are refused as a stale
		// launch's are, which is what the host asked for by ending it.
		return
	}
	go func() {
		<-t.Done()
		d.Side.Launches.TerminalExited(launchID, terminalID)
	}()
}
