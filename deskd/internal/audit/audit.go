// Package audit is the daemon's own record of what was done to the desktop: who granted control,
// every action with where it landed and on which app, every refusal and every stop. It holds
// metadata only. No pixels, no typed text (its length), no printable keys (their count), no window
// titles: the record must be safe to show and to keep, and a title or a typed string is exactly what
// would make it neither.
package audit

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

// DefaultMaxBytes is the size at which the log is rotated: the current file becomes <path>.1,
// replacing the previous one.
const DefaultMaxBytes = 8 << 20

// Record is one entry. Only these fields exist, so nothing else can be written by mistake.
type Record struct {
	At      time.Time `json:"at"`
	Role    string    `json:"role"`
	Event   string    `json:"event"` // act, screenshot, lease.granted, lease.ended, stop, resume, human.input, refused, focus
	Session string    `json:"session,omitempty"`
	Lease   string    `json:"lease,omitempty"`
	Action  string    `json:"action,omitempty"`
	// Code is the refusal's error code, 0 for success.
	Code   int    `json:"code,omitempty"`
	Reason string `json:"reason,omitempty"`
	// Points are desktop coordinates, rounded to whole units.
	Points [][2]int `json:"points,omitempty"`
	Button string   `json:"button,omitempty"`
	Count  int      `json:"count,omitempty"`
	// Keys are chords with a modifier or named keys ("ctrl+s", "enter"); printable keys are only
	// counted in KeysHidden.
	Keys       []string `json:"keys,omitempty"`
	KeysHidden int      `json:"keys_hidden,omitempty"`
	TextLength int      `json:"text_length,omitempty"`
	Screenshot string   `json:"screenshot,omitempty"`
	Bytes      int      `json:"bytes,omitempty"`
	// App is the identity of the app under the point or with the keyboard: its bundle id, executable
	// or window class, never a title.
	App   string `json:"app,omitempty"`
	Scope string `json:"scope,omitempty"`
	TTLMs int64  `json:"ttl_ms,omitempty"`
}

// Log appends records as JSON lines to a 0600 file.
type Log struct {
	mu       sync.Mutex
	path     string
	f        *os.File
	size     int64
	maxBytes int64
}

// Open opens (or creates) the log at path. An empty path keeps no log.
func Open(path string, maxBytes int64) (*Log, error) {
	l := &Log{path: path, maxBytes: maxBytes}
	if path == "" {
		return l, nil
	}
	if l.maxBytes <= 0 {
		l.maxBytes = DefaultMaxBytes
	}
	if err := l.open(); err != nil {
		return nil, err
	}
	return l, nil
}

func (l *Log) open() error {
	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	l.f, l.size = f, st.Size()
	return nil
}

// Write appends one record. A failure to write is returned but never stops the daemon: an audit
// that cannot be written is logged by the caller, and the action it describes has happened anyway.
func (l *Log) Write(r Record) error {
	if l == nil || l.path == "" {
		return nil
	}
	if r.At.IsZero() {
		r.At = time.Now().UTC()
	}
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f == nil {
		if err := l.open(); err != nil {
			return err
		}
	}
	if l.size+int64(len(b)) > l.maxBytes && l.size > 0 {
		l.f.Close()
		l.f = nil
		_ = os.Rename(l.path, l.path+".1")
		if err := l.open(); err != nil {
			return err
		}
	}
	n, err := l.f.Write(b)
	l.size += int64(n)
	return err
}

// Close closes the file.
func (l *Log) Close() error {
	if l == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f == nil {
		return nil
	}
	err := l.f.Close()
	l.f = nil
	return err
}
