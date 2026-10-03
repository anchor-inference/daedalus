// Package rpc is the daemon's methods: the JSON-RPC surface over the terminal registry, the event
// log and the process statistics.
package rpc

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"runtime"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/containment"
	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/internal/shellint"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/internal/version"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Daemon is the state every method works on.
type Daemon struct {
	Config            *config.Config
	Instance          string // new at every start, so the host can tell a restarted daemon from its predecessor
	StartedAt         time.Time
	Registry          *term.Registry
	Events            *events.Log
	Log               *slog.Logger
	EmulatorName      string
	Environ           []string                // the environment spawned processes inherit
	Side              *Side                   // the side channels; nil in builds and tests without them
	ShellDir          string                  // where the shell-integration scripts are installed; "" for none
	Sandbox           *sandbox.Prober         // bubblewrap; nil where the daemon offers no sandbox
	Containment       *containment.Controller // cgroup v2, when this daemon has delegated controllers
	ContainmentReason string

	sampler  *procstat.Sampler
	statsMu  sync.Mutex
	lastStat *procstat.Sample
}

// Hello is the params of the notification a client receives after a good handshake.
func (d *Daemon) Hello() any {
	return map[string]any{"version": version.Version, "protocol": version.Protocol, "instance": d.Instance, "env": d.Config.Env}
}

// Register installs every method on srv.
func (d *Daemon) Register(srv *server.Server) {
	d.sampler = procstat.NewSampler()
	d.registerAttach(srv)
	srv.Handle("daemon.info", d.info)
	srv.Handle("events.subscribe", d.subscribe)
	srv.Handle("events.unsubscribe", d.unsubscribe)
	srv.Handle("terminal.create", d.create)
	srv.Handle("terminal.list", d.list)
	srv.Handle("terminal.get", d.get)
	srv.Handle("terminal.write", d.write)
	srv.Handle("terminal.resize", d.resize)
	srv.Handle("terminal.signal", d.signal)
	srv.Handle("terminal.kill", d.kill)
	srv.Handle("terminal.forget", d.forget)
	srv.Handle("terminal.read_output", d.readOutput)
	srv.Handle("terminal.snapshot", d.snapshot)
	srv.Handle("terminal.read_screen", d.readScreen)
	srv.Handle("terminal.wait_for", d.waitFor)
	srv.Handle("terminal.commands", d.commands)
	srv.Handle("terminal.stats", d.stats)
	srv.Handle("attempt.containment", d.containment)
	srv.Handle("attempt.preflight", d.preflightContainment)
	srv.Handle("attempt.kill", d.killAttempt)
	srv.Handle("attempt.release", d.releaseAttempt)
	d.registerSide(srv)
}

// decode reads params strictly: an unknown field is refused, because a misspelt option that is
// silently ignored does something other than what the caller asked.
func decode(params json.RawMessage, v any) error {
	if len(params) == 0 || string(params) == "null" {
		params = json.RawMessage("{}")
	}
	dec := json.NewDecoder(bytes.NewReader(params))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return wire.Errorf(wire.CodeInvalidParams, "params: %v", err)
	}
	return nil
}

// termError maps the registry's and the terminal's errors to protocol codes.
func termError(err error) error {
	var we *wire.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, &we):
		return err
	case errors.Is(err, term.ErrNotFound), errors.Is(err, term.ErrForgotten):
		return wire.Errorf(wire.CodeNotFound, "%v", err)
	case errors.Is(err, term.ErrExited):
		return wire.Errorf(wire.CodeExited, "%v", err)
	case errors.Is(err, term.ErrLimit):
		return wire.Errorf(wire.CodeLimit, "%v", err)
	case errors.Is(err, term.ErrExists), errors.Is(err, term.ErrRunning):
		return wire.Errorf(wire.CodeForbidden, "%v", err)
	case errors.Is(err, term.ErrWriteTimeout), errors.Is(err, context.DeadlineExceeded):
		return wire.Errorf(wire.CodeTimeout, "%v", err)
	case errors.Is(err, term.ErrKeyboardHeld):
		return wire.Errorf(wire.CodeKeyboardHeld, "%v", err)
	case errors.Is(err, ptyproc.ErrUnsupported):
		return wire.Errorf(wire.CodeUnsupported, "%v", err)
	}
	return err
}

func (d *Daemon) info(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	running, exited := d.Registry.Counts()
	stats := "ok"
	if !procstat.Supported {
		stats = "unsupported on " + runtime.GOOS
	}
	sample := d.sample(nil, time.Second)
	contained := containment.Status{Kind: "cgroup_v2", Reason: d.ContainmentReason}
	if d.Containment != nil {
		contained = d.Containment.Probe()
	}
	return map[string]any{
		"version":    version.Version,
		"protocol":   version.Protocol,
		"instance":   d.Instance,
		"env":        d.Config.Env,
		"os":         runtime.GOOS,
		"arch":       runtime.GOARCH,
		"pid":        pidSelf(),
		"started_at": d.StartedAt,
		"uptime_s":   int64(time.Since(d.StartedAt).Seconds()),
		"home":       d.Config.Home,
		"shell":      d.Config.Shell,
		"capabilities": map[string]any{
			"sandbox":           d.sandboxStatus(),
			"shells":            config.Shells(),
			"shell_integration": d.integrations(),
			"emulator":          d.EmulatorName,
			"stats":             stats,
			"containment":       contained,
		},
		"hooks":         d.hooksInfo(),
		"side_channels": d.sideInfo(),
		"limits": map[string]any{
			"max_terminals":   d.Config.Limits.MaxTerminals,
			"ring_bytes":      d.Config.Limits.RingBytes,
			"window_bytes":    config.DefaultWindowBytes,
			"ack_bytes":       config.DefaultAckBytes,
			"min_cols":        config.MinCols,
			"min_rows":        config.MinRows,
			"max_cols":        config.MaxCols,
			"max_rows":        config.MaxRows,
			"input_idle_ms":   d.Config.Limits.InputIdle.Milliseconds(),
			"kill_grace_ms":   d.Config.Limits.KillGrace.Milliseconds(),
			"max_write_bytes": config.MaxWriteBytes,
			// Attachments.
			"resync_backlog_bytes":    config.ResyncBacklogBytes,
			"snapshot_scrollback":     config.DefaultSnapshotScrollback,
			"max_snapshot_scrollback": config.MaxSnapshotScrollback,
			"max_clients":             config.MaxClients,
		},
		"counts":  map[string]any{"running": running, "exited": exited},
		"machine": sample.Machine,
	}, nil
}

// integrations lists the shells whose integration the daemon has installed.
func (d *Daemon) integrations() []string {
	if d.ShellDir == "" {
		return []string{}
	}
	return shellint.KindsOn(runtime.GOOS)
}
