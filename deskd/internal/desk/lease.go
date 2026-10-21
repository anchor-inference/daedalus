package desk

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The kinds of scope a lease has, narrowest first. A window scope is the safest and the one the
// selftest takes: every point must land inside that one window, and keys go only to it.
const (
	ScopeWindow  = "window"
	ScopeApp     = "app"
	ScopeDisplay = "display"
	ScopeDesktop = "desktop"
)

// Scope is what a lease lets the agent touch.
type Scope struct {
	Kind    string `json:"kind"`
	Window  string `json:"window,omitempty"`
	App     string `json:"app,omitempty"`
	Display string `json:"display,omitempty"`
}

func (s Scope) String() string {
	switch s.Kind {
	case ScopeWindow:
		return "window:" + s.Window
	case ScopeApp:
		return "app:" + s.App
	case ScopeDisplay:
		return "display:" + s.Display
	}
	return s.Kind
}

// Lease is the operator's grant of the desktop to one session.
type Lease struct {
	ID         string    `json:"id"`
	Session    string    `json:"session"`
	GrantedAt  time.Time `json:"granted_at"`
	ExpiresAt  time.Time `json:"expires_at"`
	Scope      Scope     `json:"scope"`
	MaxActions int       `json:"max_actions"`
	Actions    int       `json:"actions"`
	// windowPID is the process of a window-scoped lease's window: a dialog the window opens belongs
	// to the same process and is inside the scope.
	windowPID int
	windowApp string
}

type endedLease struct {
	id, session, reason string
}

// LeaseStatus is lease.status's answer.
type LeaseStatus struct {
	Active bool   `json:"active"`
	Lease  *Lease `json:"lease,omitempty"`
	// RemainingMs is the time the lease has left.
	RemainingMs int64 `json:"remaining_ms,omitempty"`
	// LastEnded says why the last lease ended, when none is active.
	LastEnded string `json:"last_ended,omitempty"`
}

// GrantParams is lease.grant's.
type GrantParams struct {
	Session    string `json:"session"`
	TTLMs      int64  `json:"ttl_ms,omitempty"`
	Scope      Scope  `json:"scope"`
	MaxActions int    `json:"max_actions,omitempty"`
}

func newID(prefix string) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return prefix + hex.EncodeToString(b)
}

// Grant gives the desktop to a session. Only the operator's role reaches it; a lease already held is
// ended (superseded), so one session holds the desktop at a time. A grant is the operator's consent
// now, so it also ends a pause for the operator's hand.
func (d *Desk) Grant(ctx context.Context, p GrantParams) (Lease, error) {
	if p.Session == "" || len(p.Session) > 128 {
		return Lease{}, wire.Errorf(wire.CodeInvalidParams, "session must be 1–128 characters")
	}
	ttl := p.TTLMs
	if ttl == 0 {
		ttl = d.cfg.DefaultLeaseMs
	}
	if ttl < 1000 || ttl > d.cfg.MaxLeaseMs {
		return Lease{}, wire.Errorf(wire.CodeInvalidParams, "ttl_ms must be between 1000 and %d", d.cfg.MaxLeaseMs)
	}
	maxActions := d.cfg.LeaseActions
	if p.MaxActions > 0 && p.MaxActions < maxActions {
		maxActions = p.MaxActions
	}
	l := &Lease{ID: newID("l"), Session: p.Session, Scope: p.Scope, MaxActions: maxActions}
	switch p.Scope.Kind {
	case ScopeWindow:
		w, err := d.findWindow(ctx, p.Scope.Window)
		if err != nil {
			return Lease{}, err
		}
		if d.Protected(w.App) {
			return Lease{}, refuse(CodeProtected, "that window belongs to an app the daemon never touches", map[string]any{"app": w.App.ID})
		}
		l.windowPID, l.windowApp = w.App.PID, w.App.ID
	case ScopeApp:
		ws, err := d.be.Windows(ctx)
		if err != nil {
			return Lease{}, fromBackend(err)
		}
		found := false
		for _, w := range ws {
			if w.App.ID == p.Scope.App {
				found = true
				if d.Protected(w.App) {
					return Lease{}, refuse(CodeProtected, "that app is one the daemon never touches", map[string]any{"app": w.App.ID})
				}
			}
		}
		if !found {
			return Lease{}, wire.Errorf(wire.CodeNotFound, "no app %q with a window on screen", p.Scope.App)
		}
	case ScopeDisplay:
		ds, err := d.refreshDisplays(ctx)
		if err != nil {
			return Lease{}, fromBackend(err)
		}
		found := false
		for _, disp := range ds {
			found = found || disp.ID == p.Scope.Display
		}
		if !found {
			return Lease{}, wire.Errorf(wire.CodeNotFound, "no display %q", p.Scope.Display)
		}
	case ScopeDesktop:
	default:
		return Lease{}, wire.Errorf(wire.CodeInvalidParams, "scope.kind must be window, app, display or desktop")
	}
	cx, cy, cerr := d.be.Cursor(ctx)

	d.mu.Lock()
	now := d.now()
	l.GrantedAt, l.ExpiresAt = now.UTC(), now.Add(time.Duration(ttl)*time.Millisecond).UTC()
	old := d.lease
	if old != nil && d.actCancel != nil {
		d.actCancel()
	}
	d.lease = l
	d.clearShotsLocked()
	d.paused = false
	d.tokens, d.refilledAt = float64(d.cfg.ActionBurst), now
	// The pointer's position now is the baseline: any move from here that the daemon did not make is
	// the operator's.
	d.ourX, d.ourY, d.ourKnown = cx, cy, cerr == nil
	d.lastInject, d.lastSeenInput = now, now
	view := *l
	d.mu.Unlock()

	if old != nil {
		d.input.Lock()
		d.release()
		d.input.Unlock()
		d.publishEnded(old, "superseded", "operator")
	}
	d.events.Publish("lease.granted", "", map[string]any{"lease": view})
	d.write(audit.Record{Role: "operator", Event: "lease.granted", Session: l.Session, Lease: l.ID, Scope: l.Scope.String(), TTLMs: ttl})
	return view, nil
}

func (d *Desk) findWindow(ctx context.Context, id string) (backend.Window, error) {
	ws, err := d.be.Windows(ctx)
	if err != nil {
		return backend.Window{}, fromBackend(err)
	}
	for _, w := range ws {
		if w.ID == id {
			return w, nil
		}
	}
	return backend.Window{}, wire.Errorf(wire.CodeNotFound, "no window %q on screen", id)
}

// Revoke ends the lease, if any; session, when given, must be the lease's. by names the role.
func (d *Desk) Revoke(session, by string) (bool, error) {
	d.mu.Lock()
	l := d.lease
	if l == nil || (session != "" && l.Session != session) {
		d.mu.Unlock()
		return false, nil
	}
	d.endLocked("revoked")
	d.mu.Unlock()
	d.release()
	d.publishEnded(l, "revoked", by)
	return true, nil
}

// Stop is the kill switch: the action in flight is cancelled and lets go of everything it held, the
// lease ends, and nothing more happens until the operator grants a new one.
func (d *Desk) Stop(by string) {
	d.mu.Lock()
	l := d.lease
	if d.actCancel != nil {
		d.actCancel()
	}
	if l != nil {
		d.endLocked("stopped")
	}
	d.mu.Unlock()
	// The action in flight sees its context cancelled at its next step and returns, which frees the
	// input lock; release then lets go of anything still down.
	d.input.Lock()
	d.release()
	d.input.Unlock()
	id := ""
	if l != nil {
		id = l.ID
		d.publishEnded(l, "stopped", by)
	}
	d.events.Publish("stopped", "", map[string]any{"by": by, "lease_id": id})
	d.write(audit.Record{Role: by, Event: "stop", Lease: id})
}

// Resume ends a pause for the operator's hand.
func (d *Desk) Resume(by string) bool {
	d.mu.Lock()
	was := d.paused
	d.resumeLocked()
	d.mu.Unlock()
	if was {
		d.events.Publish("human.resumed", "", map[string]any{"by": by})
		d.write(audit.Record{Role: by, Event: "resume"})
	}
	return was
}

// resumeLocked ends the pause and takes the pointer's position as the new baseline, wherever the
// operator left it.
func (d *Desk) resumeLocked() {
	d.paused = false
	if x, y, err := d.be.Cursor(context.Background()); err == nil {
		d.ourX, d.ourY, d.ourKnown = x, y, true
	}
	d.lastInject = d.now()
}

func (d *Desk) endLocked(reason string) {
	if d.lease != nil {
		d.ended = endedLease{id: d.lease.ID, session: d.lease.Session, reason: reason}
	}
	d.lease = nil
	d.paused = false
	d.clearShotsLocked()
}

func (d *Desk) publishEnded(l *Lease, reason, by string) {
	d.events.Publish("lease.ended", "", map[string]any{"lease_id": l.ID, "session": l.Session, "reason": reason, "by": by,
		"actions": l.Actions})
	d.write(audit.Record{Role: by, Event: "lease.ended", Session: l.Session, Lease: l.ID, Reason: reason, Count: l.Actions})
}

// expire ends a lease whose time is up.
func (d *Desk) expire() {
	d.mu.Lock()
	l := d.lease
	if l == nil || d.now().Before(l.ExpiresAt) {
		d.mu.Unlock()
		return
	}
	if d.actCancel != nil {
		d.actCancel()
	}
	d.endLocked("expired")
	d.mu.Unlock()
	d.input.Lock()
	d.release()
	d.input.Unlock()
	d.publishEnded(l, "expired", "daemon")
}

// LeaseStatus reads the lease.
func (d *Desk) LeaseStatus() LeaseStatus {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lease == nil {
		return LeaseStatus{LastEnded: d.ended.reason}
	}
	l := *d.lease
	return LeaseStatus{Active: true, Lease: &l, RemainingMs: max(0, l.ExpiresAt.Sub(d.now()).Milliseconds())}
}

// holder checks that session holds an active lease, and returns a copy of it.
func (d *Desk) holderLocked(session string) (*Lease, error) {
	l := d.lease
	if l != nil && !d.now().Before(l.ExpiresAt) {
		l = nil
	}
	if l == nil {
		reason := "none"
		if d.ended.reason != "" && (session == "" || d.ended.session == session) {
			reason = d.ended.reason
		}
		return nil, refuse(CodeNoLease, fmt.Sprintf("no lease is active (%s): the operator must grant control first", reason),
			map[string]any{"reason": reason})
	}
	if session == "" || l.Session != session {
		return nil, refuse(CodeNoLease, "another session holds the desktop", map[string]any{"reason": "other_session"})
	}
	return l, nil
}
