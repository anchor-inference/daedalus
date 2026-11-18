package rpc

import (
	"context"
	"encoding/json"

	"github.com/ascorblack/daedalus/ptyd/internal/memcap"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

func (d *Daemon) memoryCapVersion() int {
	if d.MemoryCap == nil {
		return 0
	}
	return 1
}

// memoryStatus answers what memory cap the host's programs are held to, as the kernel has it.
func (d *Daemon) memoryStatus(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request struct{}
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.MemoryCap == nil {
		return memcap.Status{Reason: "this daemon runs no host commands under a cap"}, nil
	}
	return d.MemoryCap.Status(), nil
}

// memorySetCap sets the cap. A system that cannot take it is not an error of the call: the answer
// is the status, with the reason, so the app can show it beside the setting.
func (d *Daemon) memorySetCap(_ context.Context, _ *server.Conn, params json.RawMessage) (any, error) {
	var request memcap.Request
	if err := decode(params, &request); err != nil {
		return nil, err
	}
	if d.MemoryCap == nil {
		return map[string]any{"applied": false, "error": "this daemon runs no host commands under a cap",
			"status": memcap.Status{Reason: "this daemon runs no host commands under a cap"}}, nil
	}
	if _, _, err := memcap.Resolve(request, 1<<40); err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "%v", err)
	}
	status, err := d.MemoryCap.Apply(request)
	if err != nil {
		d.Log.Warn("memory cap not applied", "error", err.Error())
		return map[string]any{"applied": false, "error": err.Error(), "status": status}, nil
	}
	d.Log.Info("memory cap", "mode", status.Mode, "max_bytes", memcap.Bytes(status.MaxBytes), "high_bytes", memcap.Bytes(status.HighBytes))
	return map[string]any{"applied": true, "status": status}, nil
}
