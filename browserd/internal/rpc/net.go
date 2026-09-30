package rpc

import (
	"context"
	"encoding/json"

	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// registerNet serves the network wall's methods (the contract's The network wall): the host's
// rules, and its answers to the wall's asks. Without a wall (a test's daemon) they are not served.
func (d *Daemon) registerNet(s *server.Server) {
	if d.Net == nil {
		return
	}
	s.Handle("net.configure", d.netConfigure)
	s.Handle("net.grant", d.netGrant)
	s.Handle("net.revoke", d.netRevoke)
}

func (d *Daemon) netConfigure(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	cfg, err := netwall.DecodeConfig(params)
	if err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "params: %v", err)
	}
	if err := d.Net.Wall.Configure(cfg); err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "params: %v", err)
	}
	// Open pages start or stop judging their own navigations with the allowlist.
	d.Manager.SyncGuard(ctx)
	d.Log.Info("network wall configured", "sealed_ports", len(cfg.SealedPorts), "services_ranges", len(cfg.ServicesPorts),
		"loopback_rewrite", cfg.LoopbackRewrite, "local_sites", cfg.LocalSites, "host_addrs", len(cfg.HostAddrs), "lan_allow", len(cfg.LANAllow), "allowlist", cfg.EgressAllow != nil)
	return map[string]any{}, nil
}

// netGrant opens an asked destination for the browser that holds the group: the operator said yes.
func (d *Daemon) netGrant(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	g, err := netwall.DecodeGrant(params)
	if err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "params: %v", err)
	}
	group, err := d.Manager.Group(g.GroupID)
	if err != nil {
		return nil, err
	}
	if !d.Net.Grant(group.Browser.ID, g.Host, g.Port, g.TTL()) {
		return nil, wire.Errorf(wire.CodeNotFound, "the browser of group %q has no network wall running", g.GroupID)
	}
	return map[string]any{}, nil
}

func (d *Daemon) netRevoke(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		Host    string `json:"host"`
		Port    int    `json:"port"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	group, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	d.Net.Revoke(group.Browser.ID, p.Host, p.Port)
	return map[string]any{}, nil
}
