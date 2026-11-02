package client

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// AppEvent is one event of the global stream (/api/events): approvals, questions, session status,
// the remote mark and the device's own events.
type AppEvent struct {
	Seq       int64           `json:"seq"`
	At        string          `json:"at"`
	Type      string          `json:"type"`
	SessionID string          `json:"session_id"`
	Payload   json.RawMessage `json:"payload"`
}

// EventItem is what FollowEvents reports: an event, or a resync (re-read what the events describe,
// because some were lost), or a fatal end.
type EventItem struct {
	Event  AppEvent
	Resync bool
	Fatal  error
}

// EventsOptions selects the global stream. Session is required for a device token.
type EventsOptions struct {
	Session string
	Client  string
	Types   []string
	// KeepaliveWindow, MinBackoff and MaxBackoff as for the session stream.
	KeepaliveWindow time.Duration
	MinBackoff      time.Duration
	MaxBackoff      time.Duration
}

// FollowEvents follows the global stream until ctx ends, resuming from the last event seen.
func (c *Client) FollowEvents(ctx context.Context, opt EventsOptions) <-chan EventItem {
	if opt.KeepaliveWindow <= 0 {
		opt.KeepaliveWindow = KeepaliveWindow
	}
	if opt.MinBackoff <= 0 {
		opt.MinBackoff = time.Second
	}
	if opt.MaxBackoff <= 0 {
		opt.MaxBackoff = 15 * time.Second
	}
	out := make(chan EventItem, 64)
	go func() {
		defer close(out)
		var after *int64
		attempt := 0
		for {
			delivered, err := c.eventsOnce(ctx, opt, &after, out)
			if ctx.Err() != nil {
				return
			}
			if delivered {
				attempt = 0
			}
			if IsUnauthorized(err) || IsNotFound(err) {
				select {
				case out <- EventItem{Fatal: err}:
				case <-ctx.Done():
				}
				return
			}
			attempt++
			select {
			case <-time.After(backoff(attempt, opt.MinBackoff, opt.MaxBackoff)):
			case <-ctx.Done():
				return
			}
		}
	}()
	return out
}

func (c *Client) eventsOnce(ctx context.Context, opt EventsOptions, after **int64, out chan<- EventItem) (bool, error) {
	attemptCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	q := url.Values{"kind": {"cli"}}
	if opt.Session != "" {
		q.Set("session", opt.Session)
	}
	if opt.Client != "" {
		q.Set("client", opt.Client)
	}
	if len(opt.Types) > 0 {
		types := ""
		for i, t := range opt.Types {
			if i > 0 {
				types += ","
			}
			types += t
		}
		q.Set("types", types)
	}
	if *after != nil {
		q.Set("after", strconv.FormatInt(**after, 10))
	}
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodGet, c.url("/api/events", q), nil)
	if err != nil {
		return false, err
	}
	req.Header.Set("Accept", "text/event-stream")
	c.authorize(req)
	resp, err := c.stream.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, readAPIError(resp)
	}
	reader := newSSEReader(resp.Body)
	frames := make(chan frame)
	readErr := make(chan error, 1)
	go func() {
		defer close(frames)
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
	for {
		select {
		case <-ctx.Done():
			return delivered, ctx.Err()
		case <-time.After(opt.KeepaliveWindow):
			cancel()
			for range frames {
			}
			return delivered, fmt.Errorf("no frame for %s", opt.KeepaliveWindow)
		case f, ok := <-frames:
			if !ok {
				err := <-readErr
				if errors.Is(err, io.EOF) {
					return delivered, nil
				}
				return delivered, err
			}
			delivered = true
			if f.isComment || f.data == "" {
				continue
			}
			switch f.event {
			case "hello":
				var h struct {
					Head int64 `json:"head"`
				}
				if json.Unmarshal([]byte(f.data), &h) == nil && *after == nil {
					v := h.Head
					*after = &v
				}
				continue
			case "resync":
				var r struct {
					Head int64 `json:"head"`
				}
				if json.Unmarshal([]byte(f.data), &r) == nil {
					v := r.Head
					*after = &v
				}
				select {
				case out <- EventItem{Resync: true}:
				case <-ctx.Done():
					return delivered, ctx.Err()
				}
				continue
			}
			var ev AppEvent
			if err := json.Unmarshal([]byte(f.data), &ev); err != nil {
				continue
			}
			if ev.Type == "" {
				ev.Type = f.event
			}
			select {
			case out <- EventItem{Event: ev}:
			case <-ctx.Done():
				return delivered, ctx.Err()
			}
			if ev.Seq > 0 {
				v := ev.Seq
				*after = &v
			}
		}
	}
}
