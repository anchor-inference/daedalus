package rpc

import (
	"context"
	"encoding/json"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// What the agent adds to its own browser to test a site or an extension it builds: code run in a
// page now (page.evaluate), page scripts run at the start of every matching document (script.*),
// and unpacked extensions (extension.*), whose files reach the daemon through upload.put as an
// archive. Each acts on the agent's own browser and profile only; on the operator's Chrome every one
// is refused, since the agent works read-only there.
func (d *Daemon) registerInject(s *server.Server) {
	for name, h := range map[string]server.Handler{
		"page.evaluate":       d.evaluate,
		"script.list":         d.scriptList,
		"script.add":          d.scriptAdd,
		"script.remove":       d.scriptRemove,
		"extension.list":      d.extensionList,
		"extension.install":   d.extensionInstall,
		"extension.uninstall": d.extensionUninstall,
		"extension.logs":      d.extensionLogs,
	} {
		s.Handle(name, h)
	}
}

func (d *Daemon) evaluate(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID      string          `json:"tab_id"`
		Expression string          `json:"expression"`
		MaxChars   int             `json:"max_chars,omitempty"`
		TimeoutMs  int64           `json:"timeout_ms,omitempty"`
		Origin     *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.MaxChars < 0 || p.MaxChars > page.EvalMaxChars {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_chars is at most %d", page.EvalMaxChars)
	}
	if p.TimeoutMs < 0 || time.Duration(p.TimeoutMs)*time.Millisecond > page.EvalMaxTimeout {
		return nil, wire.Errorf(wire.CodeInvalidParams, "timeout_ms is at most %d", page.EvalMaxTimeout.Milliseconds())
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Evaluate(ctx, t, p.Expression, p.MaxChars, time.Duration(p.TimeoutMs)*time.Millisecond)
}

func scriptViews(list []browser.PageScript) []any {
	out := make([]any, 0, len(list))
	for _, s := range list {
		out = append(out, s.View())
	}
	return out
}

func (d *Daemon) scriptList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
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
	return map[string]any{"scripts": scriptViews(g.Browser.Scripts())}, nil
}

func (d *Daemon) scriptAdd(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID   string          `json:"group_id"`
		TabID     string          `json:"tab_id,omitempty"`
		Name      string          `json:"name"`
		JS        string          `json:"js,omitempty"`
		CSS       string          `json:"css,omitempty"`
		Matches   []string        `json:"matches,omitempty"`
		AllFrames bool            `json:"all_frames,omitempty"`
		Source    string          `json:"source,omitempty"`
		Origin    *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Source) > 1000 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "source is at most 1000 bytes")
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	// The code is compiled in the page the agent looks at (or the group's active one) first.
	var check *browser.Tab
	if p.TabID != "" {
		if check, err = d.Manager.Tab(p.TabID); err != nil {
			return nil, err
		}
	} else {
		check = g.ActiveTab()
	}
	list, err := d.Manager.AddScript(ctx, g, browser.PageScript{Name: p.Name, JS: p.JS, CSS: p.CSS, Matches: p.Matches,
		AllFrames: p.AllFrames, Source: p.Source}, check)
	if err != nil {
		return nil, err
	}
	return map[string]any{"scripts": scriptViews(list)}, nil
}

func (d *Daemon) scriptRemove(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		Name    string          `json:"name,omitempty"`
		All     bool            `json:"all,omitempty"`
		Origin  *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if (p.Name == "") == !p.All {
		return nil, wire.Errorf(wire.CodeInvalidParams, "give name, or all=true")
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	removed, err := d.Manager.RemoveScript(ctx, g, p.Name)
	if err != nil {
		return nil, err
	}
	if removed == nil {
		removed = []string{}
	}
	return map[string]any{"removed": removed, "scripts": scriptViews(g.Browser.Scripts())}, nil
}

func (d *Daemon) extensionList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
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
	list, err := d.Manager.Extensions(ctx, g)
	if err != nil {
		return nil, err
	}
	return map[string]any{"extensions": list}, nil
}

func (d *Daemon) extensionInstall(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID  string          `json:"group_id"`
		UploadID string          `json:"upload_id"`
		Source   string          `json:"source,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Source) > 1000 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "source is at most 1000 bytes")
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	archive, err := d.Page.UploadPath(g.ID, p.UploadID)
	if err != nil {
		return nil, err
	}
	e, err := d.Manager.InstallExtension(ctx, g, archive, p.Source)
	if err != nil {
		return nil, err
	}
	return map[string]any{"extension": e}, nil
}

func (d *Daemon) extensionUninstall(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		ID      string          `json:"id"`
		Origin  *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	e, err := d.Manager.UninstallExtension(ctx, g, p.ID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"extension": e}, nil
}

func (d *Daemon) extensionLogs(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		ID      string `json:"id"`
		After   int64  `json:"after,omitempty"`
		Level   string `json:"level,omitempty"`
		Limit   int    `json:"limit,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	e, ok := d.Manager.FindExtension(g, p.ID)
	if !ok {
		return nil, wire.Errorf(wire.CodeNotFound, "no extension %q in this browser", p.ID)
	}
	out, err := d.Page.ExtensionLogs(g.Browser, e.ID, p.After, p.Level, p.Limit)
	if err != nil {
		return nil, err
	}
	return map[string]any{"extension": e, "logs": out}, nil
}
