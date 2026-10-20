package main

// The launcher's own connection to the desktop daemon, as the operator. The daemon tells its two
// clients apart by the token they open with: the host's, in the run directory the agent is told
// about, and the operator's, in the state directory only the launcher reads. Only the operator's
// grants a lease, lifts a pause or asks the system for a permission, so this connection is what the
// shell's consent dialog, Stop button and permissions page reach the daemon through.
//
// The framing and the handshake are ptyd's (docs/architecture/terminals.md). The launcher cannot
// import ptyd/proto — that module is on a newer Go than the one every launcher build uses, and the
// launcher keeps to golang.org/x/sys alone — so the little of it a client needs is written here:
// u32be length | u32be channel | payload, JSON-RPC 2.0 on channel 0, the token as the first frame.

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// deskdProtocol is the one protocol version this client speaks.
const deskdProtocol = 1

// deskdMaxPayload is the largest payload of one frame, ptyd's limit.
const deskdMaxPayload = 1 << 20

// deskdHandshakeTimeout bounds the connection and the hello: a daemon that accepts and says nothing
// is a daemon that is not serving, and the operator is waiting on a button.
const deskdHandshakeTimeout = 5 * time.Second

// deskdError is an error the daemon answered with: its code, message and data, which the launcher's
// API hands on untouched so the host can explain the refusal in its own words.
type deskdError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *deskdError) Error() string { return fmt.Sprintf("%s (%d)", e.Message, e.Code) }

// errDeskdUnavailable wraps every failure to reach the daemon at all, as opposed to the daemon
// refusing a call: the first is the API's 503, the second its 409.
type errDeskdUnavailable struct{ reason string }

func (e *errDeskdUnavailable) Error() string { return e.reason }

func unavailable(format string, args ...any) error {
	return &errDeskdUnavailable{reason: fmt.Sprintf(format, args...)}
}

// deskdOperator is the launcher's operator client. It connects when it is first asked something
// and again whenever the connection it had is gone — the daemon restarts with a new token and a new
// endpoint, so nothing about a connection is kept past its end.
type deskdOperator struct {
	runDir    string
	tokenFile string

	mu   sync.Mutex
	conn *deskdConn
}

func newDeskdOperator(p Paths) *deskdOperator {
	return &deskdOperator{runDir: deskdRunDir(p), tokenFile: deskdOperatorToken(p)}
}

// Call makes one JSON-RPC call as the operator and returns its result. A call the daemon refuses is
// a *deskdError; a daemon that cannot be reached is an *errDeskdUnavailable.
func (o *deskdOperator) Call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	conn, err := o.connection()
	if err != nil {
		return nil, err
	}
	result, err := conn.call(ctx, method, params)
	var refused *deskdError
	if err != nil && !errors.As(err, &refused) && ctx.Err() == nil {
		// The connection is gone (the daemon restarted, or stopped): forget it so the next call
		// dials the daemon that is there now.
		o.forget(conn)
	}
	return result, err
}

// Close ends the connection, if there is one.
func (o *deskdOperator) Close() {
	o.mu.Lock()
	conn := o.conn
	o.conn = nil
	o.mu.Unlock()
	if conn != nil {
		conn.close(errors.New("closed"))
	}
}

func (o *deskdOperator) forget(conn *deskdConn) {
	o.mu.Lock()
	if o.conn == conn {
		o.conn = nil
	}
	o.mu.Unlock()
	conn.close(errors.New("forgotten"))
}

func (o *deskdOperator) connection() (*deskdConn, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.conn != nil && !o.conn.dead() {
		return o.conn, nil
	}
	conn, err := dialDeskd(o.runDir, o.tokenFile)
	if err != nil {
		return nil, err
	}
	o.conn = conn
	return conn, nil
}

// dialDeskd connects to the daemon in runDir, presents the operator's token and waits for its hello.
// The token is read here and nowhere else, kept only in the frame that carries it, and never put in
// an error: an error from here ends up in a log and in the API's answer.
func dialDeskd(runDir, tokenFile string) (*deskdConn, error) {
	if reason := deskdReason(runDir); reason != "" {
		return nil, unavailable("%s", reason)
	}
	body, err := os.ReadFile(filepath.Join(runDir, "endpoint"))
	if err != nil {
		return nil, unavailable("the desktop daemon is not running")
	}
	network, address, ok := parseEndpoint(strings.TrimSpace(string(body)), runDir)
	if !ok {
		return nil, unavailable("the desktop daemon's endpoint file names no address the launcher can reach")
	}
	token, err := os.ReadFile(tokenFile)
	if err != nil {
		return nil, unavailable("the desktop daemon's operator token cannot be read")
	}
	secret := strings.TrimSpace(string(token))
	if secret == "" {
		return nil, unavailable("the desktop daemon's operator token is empty")
	}
	nc, err := net.DialTimeout(network, address, deskdHandshakeTimeout)
	if err != nil {
		return nil, unavailable("the desktop daemon is not answering")
	}
	_ = nc.SetDeadline(time.Now().Add(deskdHandshakeTimeout))
	if err := writeDeskdFrame(nc, []byte(secret)); err != nil {
		nc.Close()
		return nil, unavailable("the desktop daemon closed the connection")
	}
	payload, err := readDeskdFrame(nc)
	if err != nil {
		nc.Close()
		// A wrong token is answered by the connection closing a second later.
		return nil, unavailable("the desktop daemon refused the launcher's operator token")
	}
	var hello struct {
		Method string `json:"method"`
		Params struct {
			Protocol int    `json:"protocol"`
			Role     string `json:"role"`
		} `json:"params"`
	}
	if json.Unmarshal(payload, &hello) != nil || hello.Method != "hello" {
		nc.Close()
		return nil, unavailable("the desktop daemon did not greet the launcher")
	}
	if hello.Params.Protocol != deskdProtocol {
		nc.Close()
		return nil, unavailable("the desktop daemon speaks protocol %d and this launcher %d; update the application", hello.Params.Protocol, deskdProtocol)
	}
	if hello.Params.Role != "operator" {
		nc.Close()
		return nil, unavailable("the desktop daemon took the launcher for the %q, not the operator", hello.Params.Role)
	}
	_ = nc.SetDeadline(time.Time{})
	conn := &deskdConn{nc: nc, pending: map[int64]chan deskdReply{}, done: make(chan struct{})}
	go conn.read()
	return conn, nil
}

type deskdReply struct {
	result json.RawMessage
	err    error
}

// deskdConn is one authenticated connection. Calls run concurrently; each waits for the reply with
// its id, and the connection ending answers every one still waiting.
type deskdConn struct {
	nc net.Conn

	writeMu sync.Mutex

	mu      sync.Mutex
	nextID  int64
	pending map[int64]chan deskdReply
	err     error
	done    chan struct{}
}

func (c *deskdConn) dead() bool {
	select {
	case <-c.done:
		return true
	default:
		return false
	}
}

func (c *deskdConn) close(err error) {
	c.mu.Lock()
	if c.err != nil {
		c.mu.Unlock()
		return
	}
	c.err = err
	pending := c.pending
	c.pending = map[int64]chan deskdReply{}
	close(c.done)
	c.mu.Unlock()
	c.nc.Close()
	for _, ch := range pending {
		ch <- deskdReply{err: unavailable("the connection to the desktop daemon ended")}
	}
}

func (c *deskdConn) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	if params == nil {
		params = struct{}{}
	}
	c.mu.Lock()
	if c.err != nil {
		c.mu.Unlock()
		return nil, unavailable("the connection to the desktop daemon ended")
	}
	c.nextID++
	id := c.nextID
	reply := make(chan deskdReply, 1)
	c.pending[id] = reply
	c.mu.Unlock()
	forget := func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}

	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	if err != nil {
		forget()
		return nil, err
	}
	c.writeMu.Lock()
	if deadline, ok := ctx.Deadline(); ok {
		_ = c.nc.SetWriteDeadline(deadline)
	}
	err = writeDeskdFrame(c.nc, body)
	_ = c.nc.SetWriteDeadline(time.Time{})
	c.writeMu.Unlock()
	if err != nil {
		forget()
		c.close(err)
		return nil, unavailable("the desktop daemon could not be written to")
	}
	select {
	case r := <-reply:
		return r.result, r.err
	case <-ctx.Done():
		forget()
		return nil, unavailable("the desktop daemon did not answer %s in time", method)
	}
}

// read delivers replies by id. Notifications — events, were the launcher subscribed — are dropped;
// nothing here asks for any yet.
func (c *deskdConn) read() {
	for {
		payload, err := readDeskdFrame(c.nc)
		if err != nil {
			c.close(err)
			return
		}
		var message struct {
			ID     *int64          `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *deskdError     `json:"error"`
		}
		if json.Unmarshal(payload, &message) != nil || message.ID == nil {
			continue
		}
		c.mu.Lock()
		reply, ok := c.pending[*message.ID]
		delete(c.pending, *message.ID)
		c.mu.Unlock()
		if !ok {
			continue
		}
		if message.Error != nil {
			reply <- deskdReply{err: message.Error}
		} else {
			reply <- deskdReply{result: message.Result}
		}
	}
}

// writeDeskdFrame writes one frame on channel 0.
func writeDeskdFrame(w io.Writer, payload []byte) error {
	if len(payload) > deskdMaxPayload {
		return fmt.Errorf("a frame of %d bytes is over the limit", len(payload))
	}
	frame := make([]byte, 8+len(payload))
	binary.BigEndian.PutUint32(frame[0:4], uint32(4+len(payload)))
	copy(frame[8:], payload)
	_, err := w.Write(frame)
	return err
}

// readDeskdFrame reads frames until one on channel 0 with a payload. Another channel is not used by
// this daemon yet, and an empty payload on channel 0 is the other side closing.
func readDeskdFrame(r io.Reader) ([]byte, error) {
	for {
		var header [8]byte
		if _, err := io.ReadFull(r, header[:]); err != nil {
			return nil, err
		}
		length := binary.BigEndian.Uint32(header[0:4])
		channel := binary.BigEndian.Uint32(header[4:8])
		if length < 4 || length > deskdMaxPayload+4 {
			return nil, fmt.Errorf("a frame length of %d is out of bounds", length)
		}
		payload := make([]byte, length-4)
		if _, err := io.ReadFull(r, payload); err != nil {
			return nil, err
		}
		if channel != 0 {
			continue
		}
		if len(payload) == 0 {
			return nil, io.EOF
		}
		return payload, nil
	}
}
