// Package cdp is a client of the Chrome DevTools Protocol over Chromium's debugging pipe: messages
// are JSON objects each ended by a NUL byte, sessions are flat (a "sessionId" beside the id), and a
// reply is matched to its call by id. It knows nothing of what the messages mean.
package cdp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

// MaxMessage bounds one message from Chromium. A full-page PNG of a long page is the largest thing
// that ever comes back; past this the connection is broken rather than grown without end.
const MaxMessage = 256 << 20

// ErrClosed is returned by calls on a connection whose pipe has ended.
var ErrClosed = errors.New("the browser's pipe is closed")

// Error is an error reply from Chromium.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    string `json:"data,omitempty"`
}

func (e *Error) Error() string {
	if e.Data != "" {
		return fmt.Sprintf("%s (%d): %s", e.Message, e.Code, e.Data)
	}
	return fmt.Sprintf("%s (%d)", e.Message, e.Code)
}

// Event is one message without an id.
type Event struct {
	Session string
	Method  string
	Params  json.RawMessage
}

type message struct {
	ID        int64           `json:"id,omitempty"`
	Method    string          `json:"method,omitempty"`
	Params    json.RawMessage `json:"params,omitempty"`
	Result    json.RawMessage `json:"result,omitempty"`
	Error     *Error          `json:"error,omitempty"`
	SessionID string          `json:"sessionId,omitempty"`
}

type reply struct {
	result json.RawMessage
	err    error
}

// Conn is one pipe to one browser.
type Conn struct {
	w   io.Writer
	wmu sync.Mutex

	mu      sync.Mutex
	next    int64
	pending map[int64]chan reply
	closed  bool

	// Events are handed to the handler in order, on one goroutine of their own, so a handler may call
	// back into the connection (its reply is read by the reader, which never waits for a handler).
	qmu     sync.Mutex
	queue   []Event
	qwake   chan struct{}
	handler func(Event)

	done chan struct{}
}

// New starts reading r. handler receives every event, in order; it must not block for long, since
// the events behind it wait (their memory is bounded by Chromium, which sends a screencast frame
// only after the previous one was acknowledged).
func New(r io.Reader, w io.Writer, handler func(Event)) *Conn {
	c := &Conn{w: w, pending: map[int64]chan reply{}, qwake: make(chan struct{}, 1), handler: handler,
		done: make(chan struct{})}
	go c.read(r)
	go c.dispatch()
	return c
}

// Done is closed when the pipe ends.
func (c *Conn) Done() <-chan struct{} { return c.done }

// Call sends one command and waits for its reply, decoding the result into result (which may be
// nil). session is empty for the browser itself.
func (c *Conn) Call(ctx context.Context, session, method string, params, result any) error {
	raw, err := c.CallRaw(ctx, session, method, params)
	if err != nil {
		return err
	}
	if result != nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, result); err != nil {
			return fmt.Errorf("%s: %w", method, err)
		}
	}
	return nil
}

// Send writes one command now and returns a channel that gets its error (nil on success). Commands
// sent one after another reach Chromium in that order, and a session applies them in order, which is
// how a new page is prepared: its setup is sent, then the command that lets it run, and only then
// are the replies awaited. Awaiting each in turn would never end for a popup, whose renderer is paused
// until it is let run and so cannot answer.
func (c *Conn) Send(ctx context.Context, session, method string, params any) <-chan error {
	out := make(chan error, 1)
	ch, id, err := c.write(session, method, params)
	if err != nil {
		out <- err
		return out
	}
	go func() {
		_, err := c.await(ctx, method, ch, id)
		out <- err
	}()
	return out
}

// Reply is a command's result as it came, or its error.
type Reply struct {
	Result json.RawMessage
	Err    error
}

// Start writes one command now, as Send does, and returns a channel that gets its result as it
// came. A proxy of commands (the tests' fake of the Chrome extension's relay) forwards each the
// moment it arrives and answers it whenever Chromium does, so the order on the pipe is the order
// they were sent in, and a paused page's setup is never held up behind a reply it cannot give yet.
func (c *Conn) Start(ctx context.Context, session, method string, params any) <-chan Reply {
	out := make(chan Reply, 1)
	ch, id, err := c.write(session, method, params)
	if err != nil {
		out <- Reply{Err: err}
		return out
	}
	go func() {
		r, err := c.await(ctx, method, ch, id)
		out <- Reply{Result: r, Err: err}
	}()
	return out
}

// CallRaw is Call returning the result as it came.
func (c *Conn) CallRaw(ctx context.Context, session, method string, params any) (json.RawMessage, error) {
	ch, id, err := c.write(session, method, params)
	if err != nil {
		return nil, err
	}
	return c.await(ctx, method, ch, id)
}

func (c *Conn) write(session, method string, params any) (chan reply, int64, error) {
	var p json.RawMessage
	if params == nil {
		p = json.RawMessage("{}")
	} else {
		b, err := json.Marshal(params)
		if err != nil {
			return nil, 0, err
		}
		p = b
	}
	ch := make(chan reply, 1)
	// The id is taken and the message written under one lock, so the order of ids is the order on
	// the pipe.
	c.wmu.Lock()
	defer c.wmu.Unlock()
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil, 0, ErrClosed
	}
	c.next++
	id := c.next
	c.pending[id] = ch
	c.mu.Unlock()
	b, err := json.Marshal(message{ID: id, Method: method, Params: p, SessionID: session})
	if err != nil {
		c.forget(id)
		return nil, 0, err
	}
	if _, err := c.w.Write(append(b, 0)); err != nil {
		c.forget(id)
		return nil, 0, fmt.Errorf("%s: %w", method, ErrClosed)
	}
	return ch, id, nil
}

func (c *Conn) await(ctx context.Context, method string, ch chan reply, id int64) (json.RawMessage, error) {
	select {
	case r := <-ch:
		if r.err != nil {
			var ce *Error
			if errors.As(r.err, &ce) {
				return nil, fmt.Errorf("%s: %w", method, r.err)
			}
			return nil, r.err
		}
		return r.result, nil
	case <-ctx.Done():
		c.forget(id)
		return nil, fmt.Errorf("%s: %w", method, ctx.Err())
	}
}

func (c *Conn) forget(id int64) {
	c.mu.Lock()
	delete(c.pending, id)
	c.mu.Unlock()
}

func (c *Conn) read(r io.Reader) {
	br := bufio.NewReaderSize(r, 1<<20)
	var buf []byte
	for {
		chunk, err := br.ReadSlice(0)
		if errors.Is(err, bufio.ErrBufferFull) {
			buf = append(buf, chunk...)
			if len(buf) > MaxMessage {
				break
			}
			continue
		}
		if err != nil {
			break
		}
		var msg []byte
		if len(buf) > 0 {
			buf = append(buf, chunk...)
			msg, buf = buf[:len(buf)-1], nil
		} else {
			msg = chunk[:len(chunk)-1]
		}
		c.handle(msg)
	}
	c.mu.Lock()
	c.closed = true
	for id, ch := range c.pending {
		ch <- reply{err: ErrClosed}
		delete(c.pending, id)
	}
	c.mu.Unlock()
	close(c.done)
	c.wakeDispatch()
}

func (c *Conn) handle(b []byte) {
	var m message
	if err := json.Unmarshal(b, &m); err != nil {
		return
	}
	if m.ID != 0 {
		c.mu.Lock()
		ch := c.pending[m.ID]
		delete(c.pending, m.ID)
		c.mu.Unlock()
		if ch == nil {
			return
		}
		if m.Error != nil {
			ch <- reply{err: m.Error}
		} else {
			// The slice returned by ReadSlice is reused by the next read; the result keeps a copy.
			ch <- reply{result: bytes.Clone(m.Result)}
		}
		return
	}
	if m.Method == "" {
		return
	}
	c.qmu.Lock()
	c.queue = append(c.queue, Event{Session: m.SessionID, Method: m.Method, Params: bytes.Clone(m.Params)})
	c.qmu.Unlock()
	c.wakeDispatch()
}

func (c *Conn) wakeDispatch() {
	select {
	case c.qwake <- struct{}{}:
	default:
	}
}

func (c *Conn) dispatch() {
	for {
		c.qmu.Lock()
		q := c.queue
		c.queue = nil
		c.qmu.Unlock()
		for _, e := range q {
			if c.handler != nil {
				c.handler(e)
			}
		}
		if len(q) > 0 {
			continue
		}
		select {
		case <-c.qwake:
		case <-c.done:
			// Whatever arrived before the end is still delivered, then the dispatcher stops.
			c.qmu.Lock()
			q := c.queue
			c.queue = nil
			c.qmu.Unlock()
			for _, e := range q {
				if c.handler != nil {
					c.handler(e)
				}
			}
			return
		}
	}
}
