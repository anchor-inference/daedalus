// Package device is the outbound device connection: one WebSocket to the server's relay, down which
// the server sends the shell and file calls of the sessions whose hands are on this machine.
//
// What it promises (docs/architecture/cli.md, "Disconnects"): a call is never run twice. Every call
// is filed under its request id before it starts, and stays filed, with its result once it has one,
// for fifteen minutes after it ends. A drop does not cancel what is running. On the next hello the
// device lists what it still holds (running or done); the server collects a finished result with
// device.result and waits for a running one, and a request the server sends again under an id the
// device already holds is answered from the file, never run a second time. A call the device does not
// list is one it never received or lost with a restart, and the server reports it to the agent as
// having no known outcome instead of sending it again.
package device

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/ascorblack/daedalus/cli/internal/exec"
	"github.com/ascorblack/daedalus/cli/internal/guard"
)

// Protocol is the relay protocol this client speaks.
const Protocol = 1

// Close codes the server uses.
const (
	CloseProtocol     = 4400
	CloseUnauthorized = 4401
	CloseForbidden    = 4403
	CloseReplaced     = 4409
)

// maxMessage is the largest relay message, as the contract bounds it, with room for the envelope.
const maxMessage = 1<<20 + 64<<10

// State is where the link stands.
type State int

const (
	Connecting State = iota
	Connected
	// Reconnecting is a dropped link being retried; calls held on the server wait for it.
	Reconnecting
	// Unauthorized is a credential the server refused: the device must pair again.
	Unauthorized
	// Replaced is another connection of this device taking over: a second daedalus-cli of the same
	// pairing on this machine. The link stops instead of taking it back, which would only make the
	// two replace each other forever.
	Replaced
	// Stopped is the link ended by its owner.
	Stopped
)

func (s State) String() string {
	return [...]string{"connecting", "connected", "reconnecting", "unauthorized", "replaced", "stopped"}[s]
}

// Status is a state change, with the detail the screen shows.
type Status struct {
	State   State
	Attempt int
	RetryIn time.Duration
	Err     error
}

// SessionGuard is one session this process shows, with the guard it runs under.
type SessionGuard struct {
	SessionID string `json:"session_id"`
	Guard     string `json:"guard"`
}

// Bound is one session the server says is bound to this device.
type Bound struct {
	SessionID string `json:"session_id"`
	Cwd       string `json:"cwd"`
}

// Welcome is the server's answer to the hello.
type Welcome struct {
	DeviceID      string  `json:"device_id"`
	Contract      int     `json:"contract"`
	ServerVersion string  `json:"server_version"`
	Bound         []Bound `json:"bound"`
}

// Options configures a link.
type Options struct {
	// Server is the API's base URL (http or https); the relay is at /api/cli/relay under it.
	Server string
	// Token is the device token. The static API token cannot open the relay.
	Token string
	// Client names this process to the server, the id its presence reports carry: a device holds one
	// connection per process, so two terminals on one machine do not replace each other. New makes one
	// up when it is empty.
	Client        string
	ClientVersion string
	Executor      *exec.Executor
	Guard         *guard.Guard
	// Sessions are the sessions this process shows, for the hello.
	Sessions func() []SessionGuard
	// OnStatus and OnWelcome are called from the link's goroutine and must not block.
	OnStatus  func(Status)
	OnWelcome func(Welcome)
	// OnCall is told about each call as it starts and ends, for the screen and for tests.
	OnCall func(CallEvent)
	Log    *slog.Logger
	// ResultTTL is how long a finished call's result is kept; 15 minutes by the contract.
	ResultTTL time.Duration
	// MinBackoff and MaxBackoff bound reconnects: 1 s doubling to 30 s by the contract.
	MinBackoff time.Duration
	MaxBackoff time.Duration
	// Silence is how long without any frame (a ping counts) makes the link dead: 60 s.
	Silence time.Duration
	// HTTPClient dials the relay; tests point it at a fake server.
	HTTPClient *http.Client
}

// CallEvent reports one call.
type CallEvent struct {
	ID     string
	Method string
	Action guard.Action
	// Done is false when the call starts and true when it ends; Err is its failure, if any.
	Done bool
	Err  error
	// Replayed is a request answered from the file instead of being run.
	Replayed bool
}

// Link is the device connection.
type Link struct {
	opt Options
	log *slog.Logger

	mu      sync.Mutex
	conn    *websocket.Conn
	calls   map[string]*call
	welcome *Welcome
}

type call struct {
	id     string
	done   chan struct{}
	result json.RawMessage
	err    *exec.Error
	ended  time.Time
	// cancel ends the call on the server's request.cancel (the operator stopped the turn).
	cancel context.CancelFunc
}

// New returns a link; Run connects it.
func New(opt Options) *Link {
	if opt.ResultTTL <= 0 {
		opt.ResultTTL = 15 * time.Minute
	}
	if opt.MinBackoff <= 0 {
		opt.MinBackoff = time.Second
	}
	if opt.MaxBackoff <= 0 {
		opt.MaxBackoff = 30 * time.Second
	}
	if opt.Silence <= 0 {
		opt.Silence = 60 * time.Second
	}
	if opt.Sessions == nil {
		opt.Sessions = func() []SessionGuard { return nil }
	}
	if opt.Client == "" {
		opt.Client = fmt.Sprintf("cli-%012x", rand.Uint64()&0xffffffffffff)
	}
	log := opt.Log
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return &Link{opt: opt, log: log, calls: map[string]*call{}}
}

// Welcome is the server's last welcome, nil before the first.
func (l *Link) Welcome() *Welcome {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.welcome
}

func (l *Link) status(s Status) {
	if l.opt.OnStatus != nil {
		l.opt.OnStatus(s)
	}
}

// ErrUnauthorized and ErrReplaced are the ends Run reports.
var (
	ErrUnauthorized = errors.New("the server refused this device's token: run daedalus-cli login again")
	ErrForbidden    = errors.New("the server does not let this credential open the device connection")
	ErrReplaced     = errors.New("another daedalus-cli of this device took over the device connection")
)

// Run keeps the link up until ctx ends or the server refuses it for good. Calls in flight when ctx
// ends are cancelled, which ends their processes: the process that ran them is going away.
func (l *Link) Run(ctx context.Context) error {
	attempt := 0
	for {
		l.status(Status{State: Connecting, Attempt: attempt})
		connected, err := l.session(ctx)
		if ctx.Err() != nil {
			l.status(Status{State: Stopped})
			return nil
		}
		if connected {
			attempt = 0
		}
		switch websocket.CloseStatus(err) {
		case CloseUnauthorized:
			l.status(Status{State: Unauthorized, Err: ErrUnauthorized})
			return ErrUnauthorized
		case CloseForbidden:
			l.status(Status{State: Unauthorized, Err: ErrForbidden})
			return ErrForbidden
		case CloseReplaced:
			l.status(Status{State: Replaced, Err: ErrReplaced})
			return ErrReplaced
		}
		var unauthorized *handshakeError
		if errors.As(err, &unauthorized) && (unauthorized.status == http.StatusUnauthorized || unauthorized.status == http.StatusForbidden) {
			l.status(Status{State: Unauthorized, Err: ErrUnauthorized})
			return ErrUnauthorized
		}
		attempt++
		delay := backoff(attempt, l.opt.MinBackoff, l.opt.MaxBackoff)
		l.log.Info("device link down", "err", err, "attempt", attempt, "retry_in", delay)
		l.status(Status{State: Reconnecting, Attempt: attempt, RetryIn: delay, Err: err})
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			l.status(Status{State: Stopped})
			return nil
		}
	}
}

func backoff(n int, min, max time.Duration) time.Duration {
	d := min
	for i := 1; i < n && d < max; i++ {
		d *= 2
	}
	if d > max {
		d = max
	}
	return d - time.Duration(rand.Float64()*float64(d)/4)
}

type handshakeError struct {
	status int
	err    error
}

func (e *handshakeError) Error() string {
	return fmt.Sprintf("the relay refused the connection (%d): %v", e.status, e.err)
}

// relayURL is the relay's address for an API base URL.
func relayURL(server string) (string, error) {
	u, err := url.Parse(strings.TrimRight(server, "/"))
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "http":
		u.Scheme = "ws"
	case "https":
		u.Scheme = "wss"
	default:
		return "", fmt.Errorf("server address %q: not http or https", server)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/cli/relay"
	return u.String(), nil
}

// session runs one connection to its end, reporting whether it got as far as the welcome.
func (l *Link) session(ctx context.Context) (bool, error) {
	target, err := relayURL(l.opt.Server)
	if err != nil {
		return false, err
	}
	lastFrame := make(chan struct{}, 1)
	touch := func() {
		select {
		case lastFrame <- struct{}{}:
		default:
		}
	}
	dialCtx, cancelDial := context.WithTimeout(ctx, 30*time.Second)
	conn, resp, err := websocket.Dial(dialCtx, target, &websocket.DialOptions{
		HTTPClient: l.opt.HTTPClient,
		HTTPHeader: http.Header{"Authorization": {"Device " + l.opt.Token}, "User-Agent": {"daedalus-cli/" + l.opt.ClientVersion}},
		OnPingReceived: func(context.Context, []byte) bool {
			touch()
			return true
		},
	})
	cancelDial()
	if err != nil {
		if resp != nil && resp.StatusCode >= 400 {
			return false, &handshakeError{status: resp.StatusCode, err: err}
		}
		return false, err
	}
	conn.SetReadLimit(maxMessage)
	connCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer conn.CloseNow()

	if err := l.send(connCtx, conn, map[string]any{"jsonrpc": "2.0", "method": "device.hello", "params": l.hello()}); err != nil {
		return false, err
	}
	// The watchdog: no message and no ping for the silence window is a dead link, which a TCP
	// connection that stopped delivering would otherwise hide forever. Halfway through a silence the
	// client pings the server itself, so a quiet link to a server that does not ping (the contract says
	// it pings every 20 s; a proxy may eat that) is proved alive by the pong rather than torn down.
	go func() {
		half := l.opt.Silence / 2
		timer := time.NewTimer(half)
		defer timer.Stop()
		probing := false
		for {
			select {
			case <-connCtx.Done():
				return
			case <-lastFrame:
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				probing = false
				timer.Reset(half)
			case <-timer.C:
				if probing {
					l.log.Warn("device link silent", "for", l.opt.Silence)
					_ = conn.CloseNow()
					return
				}
				probing = true
				timer.Reset(half)
				go func() {
					pingCtx, cancel := context.WithTimeout(connCtx, half)
					defer cancel()
					if conn.Ping(pingCtx) == nil {
						touch()
					}
				}()
			}
		}
	}()

	welcomed := false
	for {
		kind, data, err := conn.Read(connCtx)
		if err != nil {
			l.detach(conn)
			return welcomed, err
		}
		touch()
		if kind != websocket.MessageText {
			// Binary frames are reserved for ptyd's framed channels; a device without them ignores them.
			continue
		}
		var msg struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.Unmarshal(data, &msg); err != nil {
			l.log.Warn("unreadable relay message", "err", err)
			continue
		}
		if !welcomed {
			if msg.Method != "device.welcome" {
				_ = conn.Close(CloseProtocol, "expected device.welcome")
				return false, fmt.Errorf("the server sent %q before device.welcome", msg.Method)
			}
			var w Welcome
			if err := json.Unmarshal(msg.Params, &w); err != nil {
				return false, fmt.Errorf("an unreadable welcome: %w", err)
			}
			for _, b := range w.Bound {
				if err := l.opt.Executor.Bind(b.SessionID, b.Cwd); err != nil {
					l.log.Warn("bound session refused", "session", b.SessionID, "cwd", b.Cwd, "err", err)
				}
			}
			l.mu.Lock()
			l.conn = conn
			l.welcome = &w
			l.mu.Unlock()
			welcomed = true
			l.status(Status{State: Connected})
			if l.opt.OnWelcome != nil {
				l.opt.OnWelcome(w)
			}
			// Results that finished while the link was down and that the server already knows
			// about wait for device.result; nothing is pushed unasked.
			continue
		}
		if len(msg.ID) == 0 || string(msg.ID) == "null" {
			if msg.Method == "request.cancel" {
				l.cancelCall(msg.Params)
			}
			continue
		}
		var id string
		if err := json.Unmarshal(msg.ID, &id); err != nil {
			// Ids are strings by the contract; a number is answered under what was sent.
			id = string(msg.ID)
		}
		go l.serve(ctx, conn, msg.ID, id, msg.Method, msg.Params)
	}
}

// detach forgets conn as the current connection.
func (l *Link) detach(conn *websocket.Conn) {
	l.mu.Lock()
	if l.conn == conn {
		l.conn = nil
	}
	l.mu.Unlock()
}

func (l *Link) hello() map[string]any {
	home, _ := os.UserHomeDir()
	shell := os.Getenv("SHELL")
	if runtime.GOOS == "windows" {
		shell = "powershell"
	}
	return map[string]any{
		"protocol":       Protocol,
		"client":         l.opt.Client,
		"client_version": l.opt.ClientVersion,
		"os":             runtime.GOOS,
		"arch":           runtime.GOARCH,
		"shell":          shell,
		"home":           home,
		"capabilities":   map[string]any{"exec": true, "fs": exec.FSMethods, "pty": false},
		"sessions":       nonNil(l.opt.Sessions()),
		"known":          l.known(),
	}
}

func nonNil(s []SessionGuard) []SessionGuard {
	if s == nil {
		return []SessionGuard{}
	}
	return s
}

// known lists the calls still held, after dropping finished ones older than the TTL.
func (l *Link) known() []map[string]string {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.pruneLocked()
	out := []map[string]string{}
	ids := make([]string, 0, len(l.calls))
	for id := range l.calls {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		state := "running"
		select {
		case <-l.calls[id].done:
			state = "done"
		default:
		}
		out = append(out, map[string]string{"id": id, "state": state})
	}
	return out
}

func (l *Link) pruneLocked() {
	cutoff := time.Now().Add(-l.opt.ResultTTL)
	for id, c := range l.calls {
		select {
		case <-c.done:
			if c.ended.Before(cutoff) {
				delete(l.calls, id)
			}
		default:
		}
	}
}

// Held reports the request ids the link holds, for tests and for doctor.
func (l *Link) Held() []string {
	var out []string
	for _, k := range l.known() {
		out = append(out, k["id"]+"="+k["state"])
	}
	return out
}

// serve answers one request on conn. A request under an id already held is answered from the file.
func (l *Link) serve(ctx context.Context, conn *websocket.Conn, rawID json.RawMessage, id, method string, params json.RawMessage) {
	if method == "device.result" {
		var p struct {
			ID string `json:"id"`
		}
		_ = json.Unmarshal(params, &p)
		l.mu.Lock()
		c := l.calls[p.ID]
		l.mu.Unlock()
		if c == nil {
			l.reply(ctx, rawID, nil, &exec.Error{Code: exec.CodeNotFound, Message: "this terminal holds no request " + p.ID})
			return
		}
		l.awaitAndReply(ctx, c, rawID)
		return
	}

	callCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	l.mu.Lock()
	c, seen := l.calls[id]
	if !seen {
		c = &call{id: id, done: make(chan struct{}), cancel: cancel}
		l.calls[id] = c
	}
	l.mu.Unlock()
	action := Describe(method, params)
	if seen {
		if l.opt.OnCall != nil {
			l.opt.OnCall(CallEvent{ID: id, Method: method, Action: action, Replayed: true})
		}
		l.awaitAndReply(ctx, c, rawID)
		return
	}

	if l.opt.OnCall != nil {
		l.opt.OnCall(CallEvent{ID: id, Method: method, Action: action})
	}
	result, callErr := l.run(callCtx, id, method, params, action)
	l.mu.Lock()
	c.result, c.err, c.ended = result, callErr, time.Now()
	close(c.done)
	l.mu.Unlock()
	if l.opt.OnCall != nil {
		var err error
		if callErr != nil {
			err = callErr
		}
		l.opt.OnCall(CallEvent{ID: id, Method: method, Action: action, Done: true, Err: err})
	}
	l.reply(ctx, rawID, c.result, c.err)
}

func (l *Link) awaitAndReply(ctx context.Context, c *call, rawID json.RawMessage) {
	select {
	case <-c.done:
	case <-ctx.Done():
		return
	}
	l.mu.Lock()
	result, err := c.result, c.err
	l.mu.Unlock()
	l.reply(ctx, rawID, result, err)
}

// run decides and performs one call.
func (l *Link) run(ctx context.Context, id, method string, params json.RawMessage, action guard.Action) (json.RawMessage, *exec.Error) {
	if l.opt.Guard != nil {
		if err := l.opt.Guard.Check(ctx, action); err != nil {
			var refusal *guard.Refusal
			if errors.As(err, &refusal) {
				reason := exec.ReasonOperator
				if refusal.Level == guard.Plan {
					reason = exec.ReasonPlan
				}
				return nil, exec.Refuse(reason, refusal.Reason)
			}
			return nil, &exec.Error{Code: exec.CodeInternal, Message: err.Error()}
		}
	}
	req := exec.Request{Method: method, Params: params, Session: action.Session}
	// Only a command run as one streams its output: a file read the server does through the shell
	// (intent read) would otherwise cross the link twice.
	if method == "exec.run" && intentOf(params) == "exec" {
		out := newOutputStream(l, id)
		defer out.close()
		req.Output = out
	}
	res, err := l.opt.Executor.Handle(ctx, req)
	if err == nil && ctx.Err() != nil {
		// Stopped by request.cancel: the command was ended, and its partial result says so.
		var wrapped map[string]any
		if raw, merr := json.Marshal(res); merr == nil && json.Unmarshal(raw, &wrapped) == nil {
			wrapped["cancelled"] = true
			res = wrapped
		}
	}
	if err != nil {
		var own *exec.Error
		if errors.As(err, &own) {
			return nil, own
		}
		return nil, &exec.Error{Code: exec.CodeInternal, Message: err.Error()}
	}
	raw, merr := json.Marshal(res)
	if merr != nil {
		return nil, &exec.Error{Code: exec.CodeInternal, Message: merr.Error()}
	}
	return raw, nil
}

// cancelCall ends a running call the server no longer wants. Its result, the command ended part way,
// is still filed under its id, so a later device.result answers it.
func (l *Link) cancelCall(params json.RawMessage) {
	var p struct {
		ID string `json:"id"`
	}
	_ = json.Unmarshal(params, &p)
	l.mu.Lock()
	c := l.calls[p.ID]
	l.mu.Unlock()
	if c != nil && c.cancel != nil {
		c.cancel()
	}
}

// reply sends a response on whatever connection is current. With none, the result stays held for
// device.result after the reconnect.
func (l *Link) reply(ctx context.Context, rawID json.RawMessage, result json.RawMessage, callErr *exec.Error) {
	l.mu.Lock()
	conn := l.conn
	l.mu.Unlock()
	if conn == nil {
		return
	}
	msg := map[string]any{"jsonrpc": "2.0", "id": rawID}
	if callErr != nil {
		msg["error"] = callErr
	} else {
		msg["result"] = result
	}
	if err := l.send(ctx, conn, msg); err != nil {
		l.log.Info("reply not sent; held for device.result", "err", err)
	}
}

func (l *Link) send(ctx context.Context, conn *websocket.Conn, msg any) error {
	raw, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	wctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	return conn.Write(wctx, websocket.MessageText, raw)
}

// Notify sends a notification on the current connection, if there is one; it reports whether it
// was sent. A guard change made while the link is down reaches the server in the next hello.
func (l *Link) Notify(ctx context.Context, method string, params any) bool {
	l.mu.Lock()
	conn := l.conn
	l.mu.Unlock()
	if conn == nil {
		return false
	}
	return l.send(ctx, conn, map[string]any{"jsonrpc": "2.0", "method": method, "params": params}) == nil
}

// NotifyGuard tells the server the operator moved the guard of a session.
func (l *Link) NotifyGuard(ctx context.Context, sessionID string, level guard.Level) bool {
	return l.Notify(ctx, "device.guard", map[string]string{"session_id": sessionID, "guard": level.String()})
}

// NotifySessions tells the server which sessions this process shows now.
func (l *Link) NotifySessions(ctx context.Context) bool {
	return l.Notify(ctx, "device.sessions", map[string]any{"sessions": nonNil(l.opt.Sessions())})
}
