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

// registerPage adds the methods that read and act on pages.
func (d *Daemon) registerPage(s *server.Server) {
	for name, h := range map[string]server.Handler{
		"page.snapshot":   d.snapshot,
		"page.find":       d.find,
		"page.text":       d.text,
		"page.screenshot": d.screenshot,
		"page.act":        d.act,
		"page.wait":       d.wait,
		"dialog.answer":   d.dialogAnswer,
		"download.list":   d.downloadList,
		"download.read":   d.downloadRead,
		"download.delete": d.downloadDelete,
		"upload.put":      d.uploadPut,
		"page.logs":       d.logs,
		"page.network":    d.network,
		"page.request":    d.request,
		"page.inspect":    d.inspect,
	} {
		s.Handle(name, h)
	}
}

func (d *Daemon) snapshot(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID    string          `json:"tab_id"`
		ScopeRef string          `json:"scope_ref,omitempty"`
		MaxChars int             `json:"max_chars,omitempty"`
		View     string          `json:"view,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Snapshot(ctx, t, p.ScopeRef, p.MaxChars, p.View)
}

func (d *Daemon) find(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID         string          `json:"tab_id"`
		Query         string          `json:"query"`
		Regex         bool            `json:"regex,omitempty"`
		CaseSensitive bool            `json:"case_sensitive,omitempty"`
		Max           int             `json:"max,omitempty"`
		Origin        *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Find(ctx, t, p.Query, p.Regex, p.CaseSensitive, p.Max)
}

func (d *Daemon) text(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID    string          `json:"tab_id"`
		Ref      string          `json:"ref,omitempty"`
		MaxChars int             `json:"max_chars,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Text(ctx, t, p.Ref, p.MaxChars)
}

func (d *Daemon) screenshot(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p page.ScreenshotParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Screenshot(ctx, t, p)
}

func (d *Daemon) act(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p page.ActParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Act(ctx, t, p)
}

func (d *Daemon) wait(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID     string          `json:"tab_id"`
		For       string          `json:"for"`
		Value     string          `json:"value,omitempty"`
		TimeoutMs int64           `json:"timeout_ms"`
		Origin    *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.TimeoutMs < 1 || p.TimeoutMs > 60000 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "timeout_ms must be 1-60000")
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Wait(ctx, t, p.For, p.Value, time.Duration(p.TimeoutMs)*time.Millisecond)
}

func (d *Daemon) dialogAnswer(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID  string          `json:"tab_id"`
		Accept bool            `json:"accept"`
		Text   string          `json:"text,omitempty"`
		Origin *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	if d.Workflow != nil && !p.Origin.IsAgent() {
		d.Workflow.Dialog(t, p.Accept)
	}
	mark := t.AutoDialogMark()
	out, err := d.Page.AnswerDialog(ctx, t, p.Accept, p.Text)
	if err != nil {
		return nil, err
	}
	// An answer can bring the next dialog: an alert after a confirm is answered on its own.
	if auto := t.AutoDialogsSince(mark); len(auto) > 0 {
		out["dialogs_auto"] = auto
	}
	return out, nil
}

func (d *Daemon) downloadList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if _, err := d.Manager.Group(p.GroupID); err != nil {
		return nil, err
	}
	return map[string]any{"downloads": d.Page.Downloads(p.GroupID)}, nil
}

func (d *Daemon) downloadRead(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID     string `json:"id"`
		Offset int64  `json:"offset"`
		Max    int    `json:"max,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.Max < 0 || p.Max > page.ChunkBytes {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max is at most %d", page.ChunkBytes)
	}
	return d.Page.ReadDownload(p.ID, p.Offset, p.Max)
}

func (d *Daemon) downloadDelete(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID string `json:"id"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := d.Page.DeleteDownload(p.ID); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

func (d *Daemon) uploadPut(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		UploadID string `json:"upload_id,omitempty"`
		GroupID  string `json:"group_id"`
		Name     string `json:"name"`
		Offset   int64  `json:"offset"`
		Data     string `json:"data_b64"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	return d.Page.PutUpload(g, p.UploadID, p.Name, p.Offset, p.Data)
}

func (d *Daemon) logs(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID  string          `json:"tab_id"`
		After  int64           `json:"after,omitempty"`
		Level  string          `json:"level,omitempty"`
		Limit  int             `json:"limit,omitempty"`
		Origin *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Logs(t, p.After, p.Level, p.Limit)
}

func (d *Daemon) network(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID    string          `json:"tab_id"`
		After    int64           `json:"after,omitempty"`
		Types    []string        `json:"types,omitempty"`
		Host     string          `json:"host,omitempty"`
		Method   string          `json:"method,omitempty"`
		Contains string          `json:"contains,omitempty"`
		Failed   bool            `json:"failed,omitempty"`
		Limit    int             `json:"limit,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Contains) > 500 || len(p.Host) > 253 || len(p.Method) > 20 || len(p.Types) > 16 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "the filter is too long")
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Network(t, p.After, page.NetworkFilter{Types: p.Types, Host: p.Host, Method: p.Method, Contains: p.Contains, Failed: p.Failed}, p.Limit)
}

func (d *Daemon) request(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID    string          `json:"tab_id"`
		ID       string          `json:"id"`
		Body     bool            `json:"body,omitempty"`
		MaxChars int             `json:"max_chars,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Request(ctx, t, p.ID, p.Body, p.MaxChars)
}

func (d *Daemon) inspect(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		TabID    string          `json:"tab_id"`
		Ref      string          `json:"ref,omitempty"`
		Selector string          `json:"selector,omitempty"`
		MaxChars int             `json:"max_chars,omitempty"`
		Origin   *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	t, err := d.gatedTab(ctx, p.TabID, p.Origin)
	if err != nil {
		return nil, err
	}
	return d.Page.Inspect(ctx, t, p.Ref, p.Selector, p.MaxChars)
}
