package rpc

import (
	"context"
	"encoding/json"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// registerWorkflow serves the recording of a person's steps (the contract's Recording a person's
// steps). Without a recorder (a test's daemon) it is not served.
func (d *Daemon) registerWorkflow(s *server.Server) {
	if d.Workflow == nil {
		return
	}
	s.Handle("workflow.start", d.workflowStart)
	s.Handle("workflow.stop", d.workflowStop)
	s.Handle("workflow.get", d.workflowGet)
	s.Handle("workflow.mark", d.workflowMark)
}

func (d *Daemon) workflowStart(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		Values  string `json:"values,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	return d.Workflow.Start(g, p.Values)
}

func (d *Daemon) workflowStop(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	id, err := groupParam(params)
	if err != nil {
		return nil, err
	}
	return d.Workflow.Stop(id, "operator")
}

func (d *Daemon) workflowGet(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	id, err := groupParam(params)
	if err != nil {
		return nil, err
	}
	return map[string]any{"workflow": d.Workflow.Get(id)}, nil
}

func (d *Daemon) workflowMark(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		TabID   string `json:"tab_id,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	t := g.ActiveTab()
	if p.TabID != "" {
		if t, err = d.Manager.Tab(p.TabID); err != nil {
			return nil, err
		}
		if t.Group != g {
			return nil, wire.Errorf(browser.CodeNoSuchTab, "tab %s is not in group %s", p.TabID, p.GroupID)
		}
	}
	if t == nil {
		return nil, wire.Errorf(browser.CodeNoSuchTab, "group %s has no tab", p.GroupID)
	}
	step, err := d.Workflow.Mark(t)
	if err != nil {
		return nil, err
	}
	return map[string]any{"step": step}, nil
}
