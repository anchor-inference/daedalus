package rpc

import (
	"context"
	"encoding/json"

	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The relay methods of a `user` daemon (--backend attach): relay.open is how the operator's Chrome
// arrives, through `browserd bridge` on this machine or the host's WebSocket on a server; relay.status
// and relay.notify are the app's connection card and its "Check connection".

func (d *Daemon) registerRelay(s *server.Server) {
	s.Handle("relay.open", d.relayOpen)
	s.Handle("relay.status", d.relayStatus)
	s.Handle("relay.notify", d.relayNotify)
}

func errNoRelay() *wire.Error {
	e := wire.Errorf(wire.CodeUnsupported, "this daemon drives browsers of its own and takes no relay; the operator's Chrome connects to a daemon started with --backend attach")
	e.Data = map[string]any{"reason": "launch_backend"}
	return e
}

// channelSender is a relay's side of the channel relay.open opened.
type channelSender struct {
	c  *server.Conn
	id uint32
}

func (s *channelSender) Send(payload []byte) error { return s.c.SendChannel(s.id, payload) }
func (s *channelSender) Close()                    { s.c.CloseChannel(s.id) }

func (d *Daemon) relayOpen(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Transport   string `json:"transport"`
		InstallName string `json:"install_name,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if d.Relay == nil {
		return nil, errNoRelay()
	}
	if p.Transport != relay.TransportNative && p.Transport != relay.TransportServer {
		return nil, wire.Errorf(wire.CodeInvalidParams, "transport must be native or server")
	}
	if len(p.InstallName) > 200 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "install_name is at most 200 bytes")
	}
	sender := &channelSender{c: c}
	link := d.Relay.Open(sender, p.Transport, p.InstallName)
	// The id is set before the reply goes out, and the relay's first frame (its hello) comes only
	// after the reply, so the link never sends on a channel it does not know.
	sender.id = c.OpenChannel(link)
	return map[string]any{"channel": sender.id, "proto": relay.Proto}, nil
}

func (d *Daemon) relayStatus(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	if err := decode(params, &struct{}{}); err != nil {
		return nil, err
	}
	if d.Relay == nil {
		return nil, errNoRelay()
	}
	return d.Relay.Status(), nil
}

func (d *Daemon) relayNotify(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		Text string `json:"text,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if d.Relay == nil {
		return nil, errNoRelay()
	}
	if len(p.Text) > 200 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "text is at most 200 bytes")
	}
	rtt, err := d.Manager.Notify(ctx, p.Text)
	if err != nil {
		return nil, err
	}
	return map[string]any{"rtt_ms": rtt.Milliseconds()}, nil
}
