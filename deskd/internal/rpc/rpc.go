// Package rpc is the daemon's methods: each decodes its parameters strictly, checks the caller's
// role, and asks the desk. The role comes from the token the connection presented, never from a
// request, so the host can never grant itself the desktop.
package rpc

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"runtime"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/deskd/internal/version"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The roles. The run directory's token is the host's; the operator's token is handed to the
// launcher alone, which speaks for the shell's Stop button, its consent dialog and its permissions
// page.
const (
	RoleHost     = "host"
	RoleOperator = "operator"
)

// role names a connection's role: the run directory's token grants the empty role, which is the
// host's.
func role(c *server.Conn) string {
	if c.Role() == RoleOperator {
		return RoleOperator
	}
	return RoleHost
}

// Daemon holds what the methods need.
type Daemon struct {
	Desk      *desk.Desk
	Events    *events.Log
	Log       *slog.Logger
	Instance  string
	StartedAt time.Time
	// Fake is the in-memory desktop when the daemon serves one; it adds fake.simulate.
	Fake *fake.Fake
}

// Hello is the handshake's notification, which tells a client its role.
func (d *Daemon) Hello(r string) any {
	if r != RoleOperator {
		r = RoleHost
	}
	p := d.Desk.Backend().Platform()
	return map[string]any{"version": version.Version, "protocol": version.Protocol, "instance": d.Instance,
		"role": r, "platform": p}
}

type handler func(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error)

// only lets the listed roles call a method.
func only(h handler, roles ...string) server.Handler {
	return func(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
		r := role(c)
		for _, allowed := range roles {
			if r == allowed {
				return h(ctx, c, params)
			}
		}
		return nil, &wire.Error{Code: desk.CodeOperatorOnly, Message: "only the operator may do this", Data: map[string]any{"role": r}}
	}
}

// Register adds every method to the server.
func (d *Daemon) Register(s *server.Server) {
	both := []string{RoleHost, RoleOperator}
	s.RoleHello = d.Hello
	for name, h := range map[string]server.Handler{
		"info":                only(d.info, both...),
		"screenshot":          only(d.screenshot, RoleHost),
		"act":                 only(d.act, RoleHost),
		"cursor":              only(d.cursor, RoleHost),
		"apps.list":           only(d.apps, both...),
		"apps.focus":          only(d.focus, RoleHost),
		"lease.grant":         only(d.grant, RoleOperator),
		"lease.revoke":        only(d.revoke, both...),
		"lease.status":        only(d.status, both...),
		"stop":                only(d.stop, both...),
		"resume":              only(d.resume, RoleOperator),
		"permissions.request": only(d.requestPermission, RoleOperator),
		"events.subscribe":    only(d.subscribe, both...),
		"events.unsubscribe":  only(d.unsubscribe, both...),
	} {
		s.Handle(name, h)
	}
	if d.Fake != nil {
		s.Handle("fake.simulate", only(d.simulate, RoleOperator))
	}
}

// decode reads params strictly: an unknown field is an error, so a misspelt parameter is never
// silently ignored.
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

func (d *Daemon) info(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	info, err := d.Desk.Info(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"version": version.Version, "protocol": version.Protocol, "instance": d.Instance, "role": role(c),
		"os": runtime.GOOS, "arch": runtime.GOARCH, "pid": os.Getpid(), "started_at": d.StartedAt,
		"uptime_s": int64(time.Since(d.StartedAt).Seconds()),
		"platform": info.Platform, "backend": info.Backend, "displays": info.Displays, "permissions": info.Permissions,
		"secure": info.Secure, "lease": info.Lease, "human": info.Human, "limits": info.Limits,
	}, nil
}

func (d *Daemon) screenshot(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p desk.ScreenshotParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return d.Desk.Screenshot(ctx, p)
}

func (d *Daemon) act(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p desk.ActParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return d.Desk.Act(ctx, p)
}

func (d *Daemon) cursor(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Session      string `json:"session"`
		ScreenshotID string `json:"screenshot_id,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return d.Desk.Cursor(ctx, p.Session, p.ScreenshotID)
}

func (d *Daemon) apps(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Session string `json:"session,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return d.Desk.Apps(ctx, p.Session, role(c) == RoleHost)
}

func (d *Daemon) focus(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p desk.FocusParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	w, err := d.Desk.Focus(ctx, p)
	if err != nil {
		return nil, err
	}
	return map[string]any{"window": w}, nil
}

func (d *Daemon) grant(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p desk.GrantParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	l, err := d.Desk.Grant(ctx, p)
	if err != nil {
		return nil, err
	}
	return map[string]any{"lease": l}, nil
}

// revoke ends the lease. The host may end its own (giving the desktop back is never a risk); the
// operator may end any.
func (d *Daemon) revoke(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Session string `json:"session,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	r := role(c)
	if r == RoleHost && p.Session == "" {
		return nil, wire.Errorf(wire.CodeInvalidParams, "the host revokes its own session's lease: give session")
	}
	ended, err := d.Desk.Revoke(p.Session, r)
	if err != nil {
		return nil, err
	}
	return map[string]any{"revoked": ended}, nil
}

func (d *Daemon) status(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	return map[string]any{"lease": d.Desk.LeaseStatus(), "human": d.Desk.HumanState()}, nil
}

// stop is the kill switch. Either role may pull it: stopping can only take power away.
func (d *Daemon) stop(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	d.Desk.Stop(role(c))
	return map[string]any{"stopped": true}, nil
}

func (d *Daemon) resume(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	return map[string]any{"resumed": d.Desk.Resume(RoleOperator)}, nil
}

func (d *Daemon) requestPermission(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Which string `json:"which"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	switch p.Which {
	case backend.PermScreen, backend.PermAccessibility, backend.PermInputMonitoring:
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "which must be screen, accessibility or input_monitoring")
	}
	be := d.Desk.Backend()
	if err := be.RequestPermission(ctx, p.Which); err != nil {
		if errors.Is(err, backend.ErrUnsupported) {
			return nil, wire.Errorf(wire.CodeUnsupported, "%v", err)
		}
		return nil, err
	}
	perms := be.Permissions(ctx)
	return map[string]any{"which": p.Which, "state": perms.Get(p.Which), "permissions": perms,
		"hint": desk.PermissionHint(p.Which)}, nil
}

func (d *Daemon) simulate(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p fake.Simulation
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := d.Fake.Simulate(p); err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "%v", err)
	}
	return map[string]any{"inputs": d.Fake.Inputs()}, nil
}

type subscriptionKey struct{}

func (d *Daemon) subscribe(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		AfterSeq int64 `json:"after_seq"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	d.stopSubscription(c)
	cursor, resync := d.Events.Start(p.AfterSeq)
	subCtx, cancel := context.WithCancel(c.Context())
	c.SetValue(subscriptionKey{}, cancel)
	result := map[string]any{"instance": d.Instance, "from_seq": cursor + 1, "resync": resync}
	return server.Then{Result: result, After: func() { go d.pump(subCtx, c, cursor) }}, nil
}

func (d *Daemon) unsubscribe(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	d.stopSubscription(c)
	return map[string]any{}, nil
}

func (d *Daemon) stopSubscription(c *server.Conn) {
	if cancel, ok := c.Value(subscriptionKey{}).(context.CancelFunc); ok {
		cancel()
		c.SetValue(subscriptionKey{}, nil)
	}
}

// pump delivers events after cursor, in order, as ptyd and browserd do: a subscriber that falls off
// the log is told with events.resync and continues from the oldest event held.
func (d *Daemon) pump(ctx context.Context, c *server.Conn, cursor int64) {
	for {
		wake := d.Events.Wait()
		evs, lost := d.Events.After(cursor, 256)
		if lost {
			if err := c.Notify("events.resync", map[string]any{"from_seq": d.Events.Oldest()}); err != nil {
				return
			}
		}
		for _, e := range evs {
			if ctx.Err() != nil {
				return
			}
			if err := c.Notify("event", e); err != nil {
				return
			}
			cursor = e.Seq
		}
		if len(evs) > 0 {
			continue
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return
		}
	}
}
