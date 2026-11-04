package rpc

import (
	"context"
	"encoding/base64"
	"encoding/json"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/record"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// registerRecord serves the recording (the contract's Recording) and the limits the host may change
// while the daemon runs. Without a recorder (a test's daemon) the recording is not served.
func (d *Daemon) registerRecord(s handles) {
	s.Handle("limits.set", d.limitsSet)
	if d.Record == nil {
		return
	}
	s.Handle("record.set", d.recordSet)
	s.Handle("record.list", d.recordList)
	s.Handle("record.read", d.recordRead)
	s.Handle("record.delete", d.recordDelete)
	s.Handle("record.groups", d.recordGroups)
}

func (d *Daemon) limitsSet(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p browser.LimitsUpdate
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	return d.Manager.SetLimits(p)
}

func (d *Daemon) recordSet(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		Frames  bool   `json:"frames"`
		Human   bool   `json:"human,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	return d.Record.Set(g, p.Frames, p.Human), nil
}

func groupParam(params json.RawMessage) (string, error) {
	var p struct {
		GroupID string `json:"group_id"`
	}
	if err := decode(params, &p); err != nil {
		return "", err
	}
	if !browser.ValidID(p.GroupID) {
		return "", wire.Errorf(wire.CodeInvalidParams, "group_id must be 1-64 of A-Z a-z 0-9 - _")
	}
	return p.GroupID, nil
}

func (d *Daemon) recordList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		After   int64  `json:"after,omitempty"`
		Limit   int    `json:"limit,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if !browser.ValidID(p.GroupID) {
		return nil, wire.Errorf(wire.CodeInvalidParams, "group_id must be 1-64 of A-Z a-z 0-9 - _")
	}
	if p.Limit == 0 {
		p.Limit = 500
	}
	if p.Limit < 1 || p.Limit > 5000 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "limit must be 1-5000")
	}
	return map[string]any{"recording": d.Record.State(p.GroupID), "frames": d.Record.Store.List(p.GroupID, p.After, p.Limit)}, nil
}

func (d *Daemon) recordRead(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		No      int64  `json:"no"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if !browser.ValidID(p.GroupID) || p.No < 1 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "group_id and a frame number are required")
	}
	f, data, ok := d.Record.Store.Read(p.GroupID, p.No)
	if !ok {
		return nil, record.ErrNoFrame(p.GroupID, p.No)
	}
	return map[string]any{"frame": f, "data_b64": base64.StdEncoding.EncodeToString(data)}, nil
}

func (d *Daemon) recordDelete(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	id, err := groupParam(params)
	if err != nil {
		return nil, err
	}
	if err := d.Record.Store.Delete(id); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

func (d *Daemon) recordGroups(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	groups, total := d.Record.Store.Groups()
	lim := d.Manager.Limits()
	return map[string]any{"groups": groups, "bytes": total, "max_bytes": lim.RecordMaxBytes, "retention_ms": lim.RecordRetentionMs}, nil
}
