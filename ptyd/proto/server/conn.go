package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// handshakeTimeout is how long a new connection has to present the token.
const handshakeTimeout = 5 * time.Second

// badTokenDelay is how long a wrong token waits before the connection closes, which makes guessing
// slow without costing a legitimate client anything.
const badTokenDelay = time.Second

// outQueue bounds the frames waiting to be written to one connection. A producer that finds it
// full waits: RPC replies are bounded by the requests in flight, and event and output producers
// read from their own logs by offset, so waiting loses nothing.
const outQueue = 256

// ChannelHandler receives the frames of one channel other than 0. An empty payload is the other
// side closing it.
type ChannelHandler interface {
	Frame(payload []byte)
	Closed()
}

// Conn is one authenticated client connection.
type Conn struct {
	srv *Server
	nc  net.Conn
	log *slog.Logger

	ctx    context.Context
	cancel context.CancelFunc
	out    chan []byte

	inflight chan struct{}
	role     string

	mu          sync.Mutex
	nextChannel uint32
	channels    map[uint32]*channel
	values      map[any]any
}

// Role is the role the connection's token grants: empty for the token given to New, otherwise the
// one AddToken named.
func (c *Conn) Role() string { return c.role }

// Context is cancelled when the connection closes. Everything started on behalf of the connection
// (subscriptions, attachments, waiting requests) hangs off it.
func (c *Conn) Context() context.Context { return c.ctx }

// Send queues one frame. It returns an error once the connection is closed.
func (c *Conn) Send(channel uint32, payload []byte) error {
	if len(payload) > wire.MaxPayload {
		return wire.ErrFrameTooLarge
	}
	frame := wire.AppendFrame(make([]byte, 0, 8+len(payload)), channel, payload)
	select {
	case c.out <- frame:
		return nil
	case <-c.ctx.Done():
		return c.ctx.Err()
	}
}

// Notify sends a JSON-RPC notification on channel 0.
func (c *Conn) Notify(method string, params any) error {
	b, err := wire.EncodeNotification(method, params)
	if err != nil {
		return err
	}
	return c.Send(wire.ControlChannel, b)
}

// Value and SetValue keep per-connection state for handlers, such as the event subscription.
func (c *Conn) Value(key any) any {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.values[key]
}

func (c *Conn) SetValue(key, v any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if v == nil {
		delete(c.values, key)
	} else {
		c.values[key] = v
	}
}

// serve runs the handshake, then the read loop, and tears everything down when either ends.
func (s *Server) serveConn(nc net.Conn) {
	defer nc.Close()
	_ = nc.SetReadDeadline(time.Now().Add(handshakeTimeout))
	f, err := wire.ReadFrame(nc)
	if err != nil {
		return
	}
	role, ok := s.authenticate(f)
	if !ok {
		s.log.Warn("connection refused: bad token", "remote", nc.RemoteAddr().String())
		time.Sleep(badTokenDelay)
		return
	}
	_ = nc.SetReadDeadline(time.Time{})

	ctx, cancel := context.WithCancel(s.ctx)
	c := &Conn{srv: s, nc: nc, log: s.log, ctx: ctx, cancel: cancel, out: make(chan []byte, outQueue),
		inflight: make(chan struct{}, maxInflight), role: role, channels: map[uint32]*channel{}, values: map[any]any{}}
	s.track(c, true)
	defer s.track(c, false)

	writerDone := make(chan struct{})
	go func() {
		defer close(writerDone)
		for {
			select {
			case b := <-c.out:
				if _, err := nc.Write(b); err != nil {
					cancel()
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()
	// Unblock the reader when the context ends for any other reason (shutdown, a write error).
	go func() {
		<-ctx.Done()
		nc.Close()
	}()

	var hello any
	if s.RoleHello != nil {
		hello = s.RoleHello(role)
	} else if s.hello != nil {
		hello = s.hello()
	}
	if hello != nil {
		_ = c.Notify("hello", hello)
	}
	for {
		f, err := wire.ReadFrame(nc)
		if err != nil {
			if !errors.Is(err, net.ErrClosed) && ctx.Err() == nil && !isEOF(err) {
				c.log.Warn("connection read", "error", err.Error())
			}
			break
		}
		if f.Channel == wire.ControlChannel {
			c.dispatch(f.Payload)
			continue
		}
		if len(f.Payload) == 0 {
			c.peerClosed(f.Channel)
			continue
		}
		c.mu.Lock()
		ch := c.channels[f.Channel]
		c.mu.Unlock()
		if ch == nil {
			continue // a channel already closed from this side, or never opened
		}
		ch.h.Frame(f.Payload)
	}
	cancel()
	<-writerDone
	c.closeAll()
}

// authenticate finds the role of the token in the first frame. Every token is compared, in constant
// time, whether or not an earlier one matched, so the time a guess takes says nothing about which
// token it came close to.
func (s *Server) authenticate(f wire.Frame) (string, bool) {
	if f.Channel != wire.ControlChannel {
		return "", false
	}
	got := trimSpace(f.Payload)
	role, ok := "", subtle.ConstantTimeCompare(got, []byte(s.token)) == 1
	for token, r := range s.roles {
		if subtle.ConstantTimeCompare(got, []byte(token)) == 1 {
			role, ok = r, true
		}
	}
	return role, ok
}

func isEOF(err error) bool {
	return errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, net.ErrClosed)
}

func trimSpace(b []byte) []byte {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r' || b[len(b)-1] == ' ') {
		b = b[:len(b)-1]
	}
	return b
}

// reply sends the response to a call.
func (c *Conn) reply(id json.RawMessage, result any, err error) {
	resp := wire.Response{JSONRPC: "2.0", ID: id}
	if err != nil {
		var we *wire.Error
		if !errors.As(err, &we) {
			we = &wire.Error{Code: wire.CodeInternal, Message: err.Error()}
		}
		resp.Error = we
	} else {
		b, merr := json.Marshal(result)
		if merr != nil {
			resp.Error = &wire.Error{Code: wire.CodeInternal, Message: merr.Error()}
		} else {
			resp.Result = b
		}
	}
	b, _ := json.Marshal(resp)
	if len(b) > wire.MaxPayload {
		resp.Result = nil
		resp.Error = &wire.Error{Code: wire.CodeLimit, Message: "the result is larger than a frame; ask for less"}
		b, _ = json.Marshal(resp)
	}
	_ = c.Send(wire.ControlChannel, b)
}
