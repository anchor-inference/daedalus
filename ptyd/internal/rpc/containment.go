package rpc

import (
	"context"
	"encoding/json"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/containment"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

type containmentParams struct {
	Scope containment.Scope `json:"scope"`
}

func (d *Daemon) preflightContainment(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request struct {
		Limits           containment.Limits `json:"limits"`
		ExpectedInstance string             `json:"expected_instance"`
	}
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.Containment == nil || request.ExpectedInstance != d.Instance {
		return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment daemon changed or is unavailable")
	}
	if d.sandboxStatus() != "ok" {
		return nil, wire.Errorf(wire.CodeUnsupported, "the sandbox cannot deny cgroupfs writes")
	}
	if status := d.Containment.Probe(); !status.Available {
		return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment is unavailable: %s", status.Reason)
	}
	if err := d.Containment.Preflight(request.Limits); err != nil {
		return nil, wire.Errorf(wire.CodeUnsupported, "the requested ceilings cannot be installed: %v", err)
	}
	return map[string]any{"available": true, "instance": d.Instance}, nil
}

func (d *Daemon) containment(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request containmentParams
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.Containment == nil {
		return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment is unavailable")
	}
	group, err := d.Containment.Lookup(request.Scope)
	if err != nil {
		return nil, wire.Errorf(wire.CodeNotFound, "this daemon does not own that attempt containment")
	}
	defer group.Close()
	view, err := group.Observe()
	if err != nil {
		return nil, wire.Errorf(wire.CodeInternal, "observing attempt containment: %v", err)
	}
	return view, nil
}

func (d *Daemon) killAttempt(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request containmentParams
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.Containment == nil {
		return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment is unavailable")
	}
	group, err := d.Containment.Lookup(request.Scope)
	if err != nil {
		return nil, wire.Errorf(wire.CodeNotFound, "this daemon does not own that attempt containment")
	}
	defer group.Close()
	view, err := group.Kill(d.Config.Limits.KillGrace + 2*time.Second)
	if err != nil {
		return map[string]any{"evidence": view, "complete": false, "reason": err.Error()}, nil
	}
	return map[string]any{"evidence": view, "complete": !view.Populated}, nil
}

func (d *Daemon) releaseAttempt(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request containmentParams
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.Containment == nil || d.Containment.Release(request.Scope) != nil {
		return nil, wire.Errorf(wire.CodeNotFound, "no empty containment owned by this daemon generation")
	}
	return map[string]any{"released": true}, nil
}
