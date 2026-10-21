package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"sync"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// maxInflight bounds the requests one connection may have running. A request past it is refused
// with a limit error rather than queued, so a client that floods the daemon learns so at once.
const maxInflight = 256

// Handler serves one method. It returns the result to encode, or an error; a *wire.Error keeps its
// code, anything else becomes an internal error.
type Handler func(ctx context.Context, c *Conn, params json.RawMessage) (any, error)

// Then is a result with work to do after the reply is on its way: a subscription must not deliver
// its first event before the reply that tells the client where it starts.
type Then struct {
	Result any
	After  func()
}

// Server accepts connections and dispatches their requests.
type Server struct {
	token   string
	roles   map[string]string // further tokens and the role each one grants
	log     *slog.Logger
	methods map[string]Handler
	hello   func() any

	// RoleHello, when set, builds the hello for a client by the role its token grants, in place of
	// hello: a daemon with more than one role says which one a client holds, so a client that holds
	// the wrong token learns it at the handshake rather than from its first refused call.
	RoleHello func(role string) any

	ctx    context.Context
	cancel context.CancelFunc

	mu    sync.Mutex
	conns map[*Conn]bool
	wg    sync.WaitGroup
}

// New returns a server that accepts clients presenting token. hello builds the params of the
// notification sent after a good handshake.
func New(token string, log *slog.Logger, hello func() any) *Server {
	ctx, cancel := context.WithCancel(context.Background())
	return &Server{token: token, roles: map[string]string{}, log: log, methods: map[string]Handler{}, hello: hello,
		ctx: ctx, cancel: cancel, conns: map[*Conn]bool{}}
}

// AddToken accepts a further token, whose connections carry role (Conn.Role). The token given to New
// grants the empty role. A daemon whose clients differ in what they may do (the desktop daemon's host
// and operator) tells them apart by the token they present, so a role can never be claimed in a
// request. It must be called before Serve.
func (s *Server) AddToken(token, role string) { s.roles[token] = role }

// Handle registers a method.
func (s *Server) Handle(method string, h Handler) { s.methods[method] = h }

// Serve accepts connections until the listener is closed.
func (s *Server) Serve(ln net.Listener) error {
	for {
		nc, err := ln.Accept()
		if err != nil {
			if s.ctx.Err() != nil {
				return nil
			}
			return err
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.serveConn(nc)
		}()
	}
}

// Close ends every connection and waits for them.
func (s *Server) Close() {
	s.cancel()
	s.wg.Wait()
}

func (s *Server) track(c *Conn, add bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if add {
		s.conns[c] = true
	} else {
		delete(s.conns, c)
	}
}

// Connections is the number of connected clients.
func (s *Server) Connections() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.conns)
}

// dispatch parses one control frame and runs its method on its own goroutine, so a slow method (a
// write waiting for the keyboard, a kill waiting for its grace) never holds up the others.
func (c *Conn) dispatch(payload []byte) {
	var req wire.Request
	if err := json.Unmarshal(payload, &req); err != nil {
		c.reply(json.RawMessage("null"), nil, &wire.Error{Code: wire.CodeParseError, Message: "not JSON: " + err.Error()})
		return
	}
	if req.JSONRPC != "2.0" || req.Method == "" {
		if !req.IsNotification() {
			c.reply(req.ID, nil, &wire.Error{Code: wire.CodeInvalidRequest, Message: "not a JSON-RPC 2.0 request"})
		}
		return
	}
	h := c.srv.methods[req.Method]
	if h == nil {
		if !req.IsNotification() {
			c.reply(req.ID, nil, wire.Errorf(wire.CodeMethodNotFound, "no method %q", req.Method))
		}
		return
	}
	select {
	case c.inflight <- struct{}{}:
	default:
		if !req.IsNotification() {
			c.reply(req.ID, nil, wire.Errorf(wire.CodeLimit, "more than %d requests in flight", maxInflight))
		}
		return
	}
	go func() {
		defer func() { <-c.inflight }()
		defer func() {
			if r := recover(); r != nil {
				// A bug in one method must not take every terminal down with the daemon.
				c.log.Error("method panicked", "method", req.Method, "panic", r)
				if !req.IsNotification() {
					c.reply(req.ID, nil, wire.Errorf(wire.CodeInternal, "internal error in %s", req.Method))
				}
			}
		}()
		result, err := h(c.ctx, c, req.Params)
		if req.IsNotification() {
			return
		}
		if then, ok := result.(Then); ok {
			c.reply(req.ID, then.Result, err)
			if err == nil && then.After != nil {
				then.After()
			}
			return
		}
		c.reply(req.ID, result, err)
	}()
}
