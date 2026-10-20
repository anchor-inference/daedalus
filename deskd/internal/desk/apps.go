package desk

import (
	"context"
	"sort"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// AppView is one app with its windows, as apps.list shows it.
type AppView struct {
	backend.App
	// Protected apps are listed to the operator only, so a lease is never scoped to one by mistake;
	// the host never sees them at all.
	Protected bool `json:"protected,omitempty"`
	// Reachable is false for an app the system will not let synthetic input reach.
	Reachable bool         `json:"reachable"`
	InScope   bool         `json:"in_scope"`
	Windows   []WindowView `json:"windows"`
}

// WindowView is one window of an app.
type WindowView struct {
	ID        string    `json:"id"`
	Title     string    `json:"title"`
	Bounds    geom.Rect `json:"bounds"`
	Display   string    `json:"display,omitempty"`
	Focused   bool      `json:"focused"`
	Minimized bool      `json:"minimized,omitempty"`
	InScope   bool      `json:"in_scope"`
	// Order is the window's place front to back among every window on screen.
	Order int `json:"order"`
}

// AppsResult is apps.list's answer.
type AppsResult struct {
	Apps []AppView `json:"apps"`
	// Foreground is the window with the keyboard.
	Foreground *WindowView `json:"foreground,omitempty"`
}

// Apps lists the apps with windows on screen, front to back. host hides the protected ones and
// requires the session's lease; the operator sees everything, to choose a scope from.
func (d *Desk) Apps(ctx context.Context, session string, host bool) (AppsResult, error) {
	var lease *Lease
	if host {
		d.mu.Lock()
		l, err := d.holderLocked(session)
		if l != nil {
			c := *l
			lease = &c
		}
		d.mu.Unlock()
		if err != nil {
			return AppsResult{}, err
		}
	} else {
		d.mu.Lock()
		if d.lease != nil {
			c := *d.lease
			lease = &c
		}
		d.mu.Unlock()
	}
	ws, err := d.be.Windows(ctx)
	if err != nil {
		return AppsResult{}, fromBackend(err)
	}
	ds, _ := d.refreshDisplays(ctx)
	byID := map[string]*AppView{}
	var order []string
	var res AppsResult
	for i, w := range ws {
		prot := d.Protected(w.App)
		if host && prot {
			continue
		}
		inScope := lease != nil && d.windowInScope(*lease, w) == nil
		wv := WindowView{ID: w.ID, Title: w.Title, Bounds: w.Bounds, Focused: w.Focused, Minimized: w.Minimized,
			InScope: inScope, Order: i}
		if disp, ok := geom.DisplayOf(ds, w.Bounds); ok {
			wv.Display = disp.ID
		}
		a := byID[w.App.ID]
		if a == nil {
			a = &AppView{App: w.App, Protected: prot, Reachable: !w.App.Elevated}
			byID[w.App.ID] = a
			order = append(order, w.App.ID)
		}
		a.InScope = a.InScope || inScope
		a.Windows = append(a.Windows, wv)
		if w.Focused {
			f := wv
			res.Foreground = &f
		}
	}
	for _, id := range order {
		res.Apps = append(res.Apps, *byID[id])
	}
	sort.SliceStable(res.Apps, func(i, j int) bool { return res.Apps[i].Windows[0].Order < res.Apps[j].Windows[0].Order })
	if res.Apps == nil {
		res.Apps = []AppView{}
	}
	return res, nil
}

// FocusParams is apps.focus's: one of the two.
type FocusParams struct {
	Session string `json:"session"`
	App     string `json:"app,omitempty"`
	Window  string `json:"window,omitempty"`
}

// Focus gives a window, or an app's frontmost window, the keyboard, for the session holding the
// lease. It counts as an action.
func (d *Desk) Focus(ctx context.Context, p FocusParams) (WindowView, error) {
	if (p.App == "") == (p.Window == "") {
		return WindowView{}, wire.Errorf(wire.CodeInvalidParams, "give app or window")
	}
	d.mu.Lock()
	l, err := d.holderLocked(p.Session)
	var lease Lease
	if l != nil {
		lease = *l
	}
	if err == nil && d.paused {
		err = d.pausedErrLocked()
	}
	d.mu.Unlock()
	rec := audit.Record{Role: "host", Event: "focus", Session: p.Session, Lease: lease.ID}
	fail := func(err error) (WindowView, error) {
		rec.Event, rec.Code = "refused", codeOf(err)
		d.write(rec)
		return WindowView{}, err
	}
	if err != nil {
		return fail(err)
	}
	if err := d.checkSecure(ctx); err != nil {
		return fail(err)
	}
	ws, err := d.be.Windows(ctx)
	if err != nil {
		return fail(fromBackend(err))
	}
	var target *backend.Window
	for i := range ws {
		w := ws[i]
		if (p.Window != "" && w.ID == p.Window) || (p.App != "" && w.App.ID == p.App && !w.Minimized) {
			target = &w
			break
		}
	}
	if target == nil {
		return fail(wire.Errorf(wire.CodeNotFound, "no such window or app on screen"))
	}
	rec.App = identity(target.App)
	if d.Protected(target.App) {
		return fail(refuse(CodeProtected, "that window belongs to an app the daemon never touches", map[string]any{"app": target.App.ID}))
	}
	if target.App.Elevated {
		return fail(refuse(CodeUnreachable, "that window runs with more rights than the daemon", map[string]any{"reason": "elevated"}))
	}
	if err := d.windowInScope(lease, *target); err != nil {
		return fail(err)
	}
	if err := d.spend(lease); err != nil {
		return fail(err)
	}
	d.input.Lock()
	d.mu.Lock()
	d.injecting = true
	d.injectSerial++
	d.mu.Unlock()
	err = d.be.Activate(ctx, target.ID)
	d.mu.Lock()
	d.injecting = false
	d.injectSerial++
	d.lastInject = d.now()
	d.mu.Unlock()
	d.input.Unlock()
	if err != nil {
		return fail(fromBackend(err))
	}
	d.write(rec)
	return WindowView{ID: target.ID, Title: target.Title, Bounds: target.Bounds, Focused: true, InScope: true}, nil
}

// CursorResult is cursor's answer.
type CursorResult struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	// InScreenshot is the pointer in the named screenshot's pixels, when it is on that picture.
	InScreenshot *[2]int `json:"in_screenshot,omitempty"`
}

// Cursor reads the pointer for the session holding the lease.
func (d *Desk) Cursor(ctx context.Context, session, screenshot string) (CursorResult, error) {
	d.mu.Lock()
	l, err := d.holderLocked(session)
	var lease Lease
	if l != nil {
		lease = *l
	}
	d.mu.Unlock()
	if err != nil {
		return CursorResult{}, err
	}
	x, y, err := d.be.Cursor(ctx)
	if err != nil {
		return CursorResult{}, fromBackend(err)
	}
	res := CursorResult{X: x, Y: y}
	if screenshot != "" {
		s, err := d.resolveShot(ctx, screenshot, lease)
		if err != nil {
			return CursorResult{}, err
		}
		if px, py, in := s.Frame.FromDesktop(x, y); in {
			res.InScreenshot = &[2]int{px, py}
		}
	}
	return res, nil
}
