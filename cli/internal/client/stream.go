package client

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"
)

// KeepaliveWindow is how long the reader waits for any frame before it calls the connection dead.
// The server sends `: keepalive` after 15 s of silence, so three of them missing is a dead link and
// not a quiet session: without the watchdog a connection that silently stopped delivering looks like
// an idle agent forever.
const KeepaliveWindow = 45 * time.Second

// Envelope is one session event as the server streams it.
type Envelope struct {
	Kind            string          `json:"kind"`
	Seq             int64           `json:"event_seq"`
	RunID           string          `json:"run_id"`
	HistoryRevision int64           `json:"history_revision"`
	Payload         json.RawMessage `json:"payload"`
	// Ephemeral events (streamed tokens) carry no cursor: they are live only, and a reader that
	// missed some re-reads the transcript instead.
	Ephemeral bool `json:"-"`
}

// Hello is the stream's first frame: where the session's event log stands.
type Hello struct {
	SessionID       string `json:"session_id"`
	EventSeq        int64  `json:"event_seq"`
	HistoryRevision int64  `json:"history_revision"`
	RuntimeEpoch    string `json:"runtime_epoch"`
}

// Signal says what a StreamItem is.
type Signal int

const (
	// SignalEvent carries an Envelope.
	SignalEvent Signal = iota
	// SignalHello is a connection made: the reader is live from Hello.EventSeq.
	SignalHello
	// SignalResync is the server saying the gap is too wide to replay: whatever the reader shows
	// must be read again from the transcript. The stream reconnects at once.
	SignalResync
	// SignalDropped is a connection lost. Whatever was streaming is over as far as the reader
	// knows, and it should re-read; the stream reconnects after RetryIn.
	SignalDropped
	// SignalFatal ends the stream: a refused credential, a session that is gone. Err says which.
	SignalFatal
)

// StreamItem is one thing the stream reports.
type StreamItem struct {
	Signal   Signal
	Envelope Envelope
	Hello    Hello
	// Reason is the server's resync reason, or what ended the dropped connection.
	Reason  string
	Attempt int
	RetryIn time.Duration
	Err     error
}

// StreamOptions tunes a session stream.
type StreamOptions struct {
	// After resumes past a cursor. Nil joins at the current boundary, which is right for a reader
	// that has just read the transcript.
	After *int64
	// KeepaliveWindow overrides KeepaliveWindow (tests use a short one).
	KeepaliveWindow time.Duration
	// MinBackoff and MaxBackoff bound the wait between reconnects; the wait doubles from the first
	// to the second and resets once a connection delivers. There is no attempt limit: a turn paused
	// by a dropped link resumes whenever the link is back, which is the whole point.
	MinBackoff time.Duration
	MaxBackoff time.Duration
}

// Stream follows one session until closed.
type Stream struct {
	items  chan StreamItem
	cancel context.CancelFunc
	done   chan struct{}

	mu    sync.Mutex
	after *int64
}

// FollowSession opens a session's stream. Items arrive on Items() until the context ends, Close is
// called, or a fatal item is delivered.
func (c *Client) FollowSession(ctx context.Context, sessionID string, opt StreamOptions) *Stream {
	if opt.KeepaliveWindow <= 0 {
		opt.KeepaliveWindow = KeepaliveWindow
	}
	if opt.MinBackoff <= 0 {
		opt.MinBackoff = time.Second
	}
	if opt.MaxBackoff <= 0 {
		opt.MaxBackoff = 15 * time.Second
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &Stream{items: make(chan StreamItem, 64), cancel: cancel, done: make(chan struct{}), after: opt.After}
	go s.run(ctx, c, sessionID, opt)
	return s
}

// Items yields what the stream reports and is closed when it ends.
func (s *Stream) Items() <-chan StreamItem { return s.items }

// Cursor is the last event sequence delivered, or nil before the first hello.
func (s *Stream) Cursor() *int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.after == nil {
		return nil
	}
	v := *s.after
	return &v
}

// Close stops the stream and waits for it.
func (s *Stream) Close() {
	s.cancel()
	<-s.done
}

func (s *Stream) setAfter(v *int64) {
	s.mu.Lock()
	s.after = v
	s.mu.Unlock()
}

func (s *Stream) advance(seq int64) {
	s.mu.Lock()
	if s.after == nil || seq > *s.after {
		v := seq
		s.after = &v
	}
	s.mu.Unlock()
}

func (s *Stream) emit(ctx context.Context, item StreamItem) bool {
	select {
	case s.items <- item:
		return true
	case <-ctx.Done():
		return false
	}
}

// errResync ends one connection on the server's resync frame: reconnect at once, no backoff.
var errResync = errors.New("resync required")

func (s *Stream) run(ctx context.Context, c *Client, sessionID string, opt StreamOptions) {
	defer close(s.done)
	defer close(s.items)
	attempt := 0
	for {
		delivered, err := s.connect(ctx, c, sessionID, opt)
		if ctx.Err() != nil {
			return
		}
		if delivered {
			attempt = 0
		}
		if errors.Is(err, errResync) {
			continue
		}
		if IsUnauthorized(err) || IsNotFound(err) {
			s.emit(ctx, StreamItem{Signal: SignalFatal, Err: err})
			return
		}
		attempt++
		delay := backoff(attempt, opt.MinBackoff, opt.MaxBackoff)
		reason := "the connection ended"
		if err != nil {
			reason = err.Error()
		}
		if !s.emit(ctx, StreamItem{Signal: SignalDropped, Reason: reason, Attempt: attempt, RetryIn: delay, Err: err}) {
			return
		}
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			return
		}
	}
}

// backoff is the wait before reconnect attempt n: doubling from min to max, with up to a quarter
// shaved off at random so a server restart is not met by every client at the same instant.
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

// connect runs one connection until it ends, reporting whether it delivered anything.
func (s *Stream) connect(ctx context.Context, c *Client, sessionID string, opt StreamOptions) (bool, error) {
	attemptCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	query := url.Values{}
	if after := s.Cursor(); after != nil {
		query.Set("after", strconv.FormatInt(*after, 10))
	}
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodGet, c.url("/api/sessions/"+url.PathEscape(sessionID)+"/stream", query), nil)
	if err != nil {
		return false, err
	}
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Cache-Control", "no-store")
	c.authorize(req)
	resp, err := c.stream.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, readAPIError(resp)
	}

	frames := make(chan frame, 32)
	readErr := make(chan error, 1)
	go func() {
		defer close(frames)
		reader := newSSEReader(resp.Body)
		for {
			f, err := reader.next()
			if err != nil {
				readErr <- err
				return
			}
			select {
			case frames <- f:
			case <-attemptCtx.Done():
				return
			}
		}
	}()

	delivered := false
	watchdog := time.NewTimer(opt.KeepaliveWindow)
	defer watchdog.Stop()
	for {
		select {
		case <-ctx.Done():
			return delivered, ctx.Err()
		case <-watchdog.C:
			// Cancelling unblocks the reader, which is otherwise waiting on a socket that will
			// never produce another byte.
			cancel()
			for range frames {
			}
			return delivered, fmt.Errorf("no frame for %s", opt.KeepaliveWindow)
		case f, ok := <-frames:
			if !ok {
				select {
				case err := <-readErr:
					if errors.Is(err, bufio.ErrTooLong) {
						return delivered, fmt.Errorf("an event larger than %d bytes", maxFrameBytes)
					}
					if errors.Is(err, io.EOF) {
						return delivered, nil
					}
					return delivered, err
				default:
					return delivered, nil
				}
			}
			delivered = true
			if err := s.handle(ctx, f); err != nil {
				cancel()
				for range frames {
				}
				return delivered, err
			}
			// Reset after the frame is handed on, not before: handing it on can wait on a slow
			// reader, and a timer armed before that wait fires during it and tears down a healthy
			// connection.
			if !watchdog.Stop() {
				select {
				case <-watchdog.C:
				default:
				}
			}
			watchdog.Reset(opt.KeepaliveWindow)
		}
	}
}

func (s *Stream) handle(ctx context.Context, f frame) error {
	if f.isComment || f.data == "" {
		return nil
	}
	switch f.event {
	case "hello":
		var h Hello
		if err := json.Unmarshal([]byte(f.data), &h); err != nil {
			return fmt.Errorf("an unreadable hello: %w", err)
		}
		// A fresh join starts at the boundary; a resumed one keeps its own cursor, which the
		// replay that follows the hello continues from.
		if s.Cursor() == nil {
			v := h.EventSeq
			s.setAfter(&v)
		}
		if !s.emit(ctx, StreamItem{Signal: SignalHello, Hello: h}) {
			return ctx.Err()
		}
		return nil
	case "resync_required":
		var r struct {
			Reason    string `json:"reason"`
			Watermark *int64 `json:"watermark"`
		}
		_ = json.Unmarshal([]byte(f.data), &r)
		if r.Reason == "" {
			// The overflow frame wraps its reason in a payload.
			var wrapped struct {
				Payload struct {
					Reason string `json:"reason"`
				} `json:"payload"`
			}
			_ = json.Unmarshal([]byte(f.data), &wrapped)
			r.Reason = wrapped.Payload.Reason
		}
		// The reader re-reads the transcript, which is current up to the watermark; resuming
		// from there misses nothing and replays nothing.
		s.setAfter(r.Watermark)
		if !s.emit(ctx, StreamItem{Signal: SignalResync, Reason: r.Reason}) {
			return ctx.Err()
		}
		return errResync
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(f.data), &raw); err != nil {
		// One malformed frame is not a reason to end the stream.
		return nil
	}
	var env Envelope
	if err := json.Unmarshal([]byte(f.data), &env); err != nil {
		return nil
	}
	if env.Kind == "" {
		env.Kind = f.event
	}
	_, env.Ephemeral = raw["ephemeral"]
	if !s.emit(ctx, StreamItem{Signal: SignalEvent, Envelope: env}) {
		return ctx.Err()
	}
	if !env.Ephemeral && f.hasID {
		s.advance(env.Seq)
	}
	return nil
}
