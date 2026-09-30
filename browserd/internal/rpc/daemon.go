// Package rpc is the daemon's methods: each decodes its parameters strictly, asks the model, and
// answers in the contract's shapes.
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

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/record"
	"github.com/ascorblack/daedalus/browserd/internal/version"
	"github.com/ascorblack/daedalus/browserd/internal/view"
	"github.com/ascorblack/daedalus/browserd/internal/workflow"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Daemon holds what the methods need.
type Daemon struct {
	Config    *config.Config
	Instance  string
	StartedAt time.Time
	Manager   *browser.Manager
	Hub       *view.Hub
	Page      *page.Model
	Events    *events.Log
	Log       *slog.Logger
	// Net is the network wall; nil serves no net.* methods.
	Net *netwall.Browsers
	// Record keeps keyframes; nil serves no record.* methods.
	Record *record.Recorder
	// Workflow records a person's steps; nil serves no workflow.* methods.
	Workflow *workflow.Recorder

	sampler *procstat.Sampler
}

// Hello is the handshake's notification.
func (d *Daemon) Hello() any {
	return map[string]any{"version": version.Version, "protocol": version.Protocol, "instance": d.Instance, "env": d.Config.Env}
}

// Register adds every method to the server.
func (d *Daemon) Register(s *server.Server) {
	d.sampler = procstat.NewSampler()
	for name, h := range map[string]server.Handler{
		"daemon.info":        d.info,
		"browser.open":       d.open,
		"browser.list":       d.browserList,
		"browser.close":      d.browserClose,
		"browser.stats":      d.stats,
		"group.list":         d.groupList,
		"group.close":        d.groupClose,
		"group.resize":       d.groupResize,
		"profile.list":       d.profileList,
		"profile.clear":      d.profileClear,
		"profile.delete":     d.profileDelete,
		"tab.list":           d.tabList,
		"tab.new":            d.tabNew,
		"tab.select":         d.tabSelect,
		"tab.close":          d.tabClose,
		"page.navigate":      d.navigate,
		"page.back":          d.history(-1),
		"page.forward":       d.history(1),
		"page.reload":        d.history(0),
		"control.set":        d.controlSet,
		"view.attach":        d.viewAttach,
		"view.detach":        d.viewDetach,
		"events.subscribe":   d.subscribe,
		"events.unsubscribe": d.unsubscribe,
	} {
		s.Handle(name, h)
	}
	d.registerPage(s)
	d.registerNet(s)
	d.registerRecord(s)
	d.registerWorkflow(s)
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
	found, ok := d.Manager.Chromium()
	chromium := map[string]any{"path": found.Path, "version": nil, "kind": found.Kind}
	if !ok {
		chromium["kind"] = "none"
		chromium["error"] = "no Chromium found"
	}
	if v := d.Manager.ChromiumVersion(); v != "" {
		chromium["version"] = v
	}
	counts := d.Manager.Counts()
	counts["viewers"] = d.Hub.Count()
	lim := d.Manager.Limits()
	st := d.Manager.Sample(d.sampler, time.Now())
	return map[string]any{
		"version": version.Version, "protocol": version.Protocol, "instance": d.Instance, "env": d.Config.Env,
		"os": runtime.GOOS, "arch": runtime.GOARCH, "pid": os.Getpid(), "started_at": d.StartedAt,
		"uptime_s": int64(time.Since(d.StartedAt).Seconds()), "chromium": chromium,
		"capabilities": map[string]any{"sandbox": d.Manager.Sandbox(), "headed": false, "screencast": true},
		"limits":       lim, "counts": counts, "machine": st.Machine,
	}, nil
}

func (d *Daemon) open(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p browser.OpenParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, t, created, err := d.Manager.Open(ctx, p)
	if err != nil {
		return nil, err
	}
	var tab any
	if t != nil {
		tab = t.View()
	}
	return map[string]any{"group": g.View(), "tab": tab, "created": created}, nil
}

func (d *Daemon) browserList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	out := []any{}
	for _, b := range d.Manager.Browsers() {
		out = append(out, b.View())
	}
	return map[string]any{"browsers": out}, nil
}

func (d *Daemon) browserClose(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		BrowserID string `json:"browser_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	groups, err := d.Manager.CloseBrowser(p.BrowserID, "closed")
	if err != nil {
		return nil, err
	}
	if groups == nil {
		groups = []string{}
	}
	return map[string]any{"groups": groups}, nil
}

func (d *Daemon) stats(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	return d.Manager.Sample(d.sampler, time.Now()), nil
}

func (d *Daemon) groupList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		BrowserID string `json:"browser_id,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	out := []any{}
	for _, g := range d.Manager.Groups(p.BrowserID) {
		out = append(out, g.View())
	}
	return map[string]any{"groups": out}, nil
}

func (d *Daemon) groupResize(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID  string           `json:"group_id"`
		Viewport browser.Viewport `json:"viewport"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	vp, err := d.Manager.Resize(ctx, p.GroupID, p.Viewport)
	if err != nil {
		return nil, err
	}
	return map[string]any{"viewport": vp}, nil
}

func (d *Daemon) groupClose(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	n, err := d.Manager.CloseGroup(ctx, p.GroupID)
	if err != nil {
		return nil, err
	}
	d.Page.GroupClosed(p.GroupID)
	if d.Record != nil {
		d.Record.Forget(p.GroupID)
	}
	if d.Workflow != nil {
		d.Workflow.Forget(p.GroupID)
	}
	return map[string]any{"tabs": n}, nil
}

func (d *Daemon) profileList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	out := d.Manager.Profiles()
	if out == nil {
		out = []browser.ProfileInfo{}
	}
	return map[string]any{"profiles": out}, nil
}

func (d *Daemon) profileChange(remove bool) server.Handler {
	return func(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
		var p struct {
			Profile string `json:"profile"`
		}
		if err := decode(params, &p); err != nil {
			return nil, err
		}
		if err := d.Manager.ClearProfile(p.Profile, remove); err != nil {
			return nil, err
		}
		return map[string]any{}, nil
	}
}

func (d *Daemon) profileClear(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	return d.profileChange(false)(ctx, c, params)
}

func (d *Daemon) profileDelete(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	return d.profileChange(true)(ctx, c, params)
}

// tabParams is what every method on one tab takes.
type tabParams struct {
	TabID  string          `json:"tab_id"`
	Origin *browser.Origin `json:"origin,omitempty"`
}

// gatedTab resolves a tab and lets the call through its group's control.
func (d *Daemon) gatedTab(ctx context.Context, id string, o *browser.Origin) (*browser.Tab, error) {
	if o != nil && o.WaitMs != nil && (*o.WaitMs < 0 || time.Duration(*o.WaitMs)*time.Millisecond > config.MaxControlWait) {
		return nil, wire.Errorf(wire.CodeInvalidParams, "origin.wait_ms must be 0-60000")
	}
	t, err := d.Manager.Tab(id)
	if err != nil {
		return nil, err
	}
	if err := t.Group.Gate(ctx, o, config.ControlWait); err != nil {
		return nil, err
	}
	return t, nil
}

func (d *Daemon) tabList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	d.Manager.RefreshTargets(ctx, g.Browser)
	out := []any{}
	for _, t := range g.Tabs() {
		out = append(out, t.View())
	}
	var active any
	if a := g.ActiveTab(); a != nil {
		active = a.ID
	}
	return map[string]any{"tabs": out, "active_tab": active}, nil
}

func (d *Daemon) tabNew(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		URL     string          `json:"url,omitempty"`
		Origin  *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	if p.URL != "" {
		if err := browser.CheckURL(p.URL, actor(p.Origin)); err != nil {
			return nil, err
		}
	}
	if err := g.Gate(ctx, p.Origin, config.ControlWait); err != nil {
		return nil, err
	}
	t, err := d.Manager.NewTab(ctx, g, p.URL, true)
	if err != nil {
		return nil, err
	}
	return t.View(), nil
}

func actor(o *browser.Origin) string {
	if o.IsAgent() {
		return browser.ActorAgent
	}
	return browser.ActorOperator
}

func (d *Daemon) tabSelect(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p tabParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	if err := d.Manager.SelectTab(ctx, t); err != nil {
		return nil, err
	}
	return t.View(), nil
}

func (d *Daemon) tabClose(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p tabParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	if err := d.Manager.CloseTab(ctx, t); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

func (d *Daemon) navigate(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID     string          `json:"tab_id"`
		URL       string          `json:"url"`
		Origin    *browser.Origin `json:"origin,omitempty"`
		TimeoutMs int64           `json:"timeout_ms,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := browser.CheckURL(p.URL, actor(p.Origin)); err != nil {
		return nil, err
	}
	timeout, err := timeoutOf(p.TimeoutMs, 30000, 60000)
	if err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Manager.Navigate(ctx, t, p.URL, timeout)
}

func timeoutOf(ms, def, most int64) (time.Duration, error) {
	if ms == 0 {
		ms = def
	}
	if ms < 0 || ms > most {
		return 0, wire.Errorf(wire.CodeInvalidParams, "timeout_ms must be 1-%d", most)
	}
	return time.Duration(ms) * time.Millisecond, nil
}

func (d *Daemon) history(delta int) server.Handler {
	return func(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
		var p tabParams
		if err := decode(params, &p); err != nil {
			return nil, err
		}
		t, err := d.gatedTab(ctx, p.TabID, p.Origin)
		if err != nil {
			return nil, err
		}
		return d.Manager.History(ctx, t, delta, 30*time.Second)
	}
}

func (d *Daemon) controlSet(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID  string `json:"group_id"`
		Owner    string `json:"owner"`
		ClientID string `json:"client_id,omitempty"`
		TTLMs    int64  `json:"ttl_ms,omitempty"`
		Reason   string `json:"reason,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	if p.TTLMs < 0 || p.TTLMs > 86_400_000 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "ttl_ms must be 0-86400000")
	}
	if len(p.Reason) > 500 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "reason is at most 500 bytes")
	}
	ttl := time.Duration(p.TTLMs) * time.Millisecond
	if p.Owner == browser.OwnerHuman {
		cl := d.Hub.Client(p.ClientID)
		if cl == nil || cl.Group() != g {
			return nil, wire.Errorf(wire.CodeNotFound, "no live view %q on this group", p.ClientID)
		}
		if cl.ReadOnly() {
			return nil, wire.Errorf(wire.CodeForbidden, "a read-only view cannot take control")
		}
		if ttl == 0 {
			ttl = config.HumanControlTTL
		}
	}
	ctl, err := g.SetControl(p.Owner, p.ClientID, ttl, p.Reason)
	if err != nil {
		return nil, err
	}
	return ctl.View(""), nil
}

func (d *Daemon) viewAttach(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		Client  view.ClientInfo `json:"client"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	ch, id, err := d.Hub.Attach(c, g, p.Client)
	if err != nil {
		return nil, err
	}
	return map[string]any{"channel": ch, "client_id": id}, nil
}

func (d *Daemon) viewDetach(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Channel uint32 `json:"channel"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := d.Hub.Detach(c, p.Channel); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

// subscriptionKey holds a connection's subscription; a new subscribe replaces it.
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

// pump delivers events after cursor, in order, as ptyd does: a subscriber that falls off the log
// is told with events.resync and continues from the oldest event held.
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
			err := c.Notify("event", e)
			if errors.Is(err, wire.ErrFrameTooLarge) {
				err = c.Notify("event", events.Event{Seq: e.Seq, At: e.At, Type: e.Type, Data: map[string]any{"too_large": true}})
			}
			if err != nil {
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
