package desk

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The actions act takes.
const (
	ActMove   = "move"
	ActClick  = "click"
	ActDrag   = "drag"
	ActScroll = "scroll"
	ActType   = "type"
	ActKey    = "key"
	ActWait   = "wait"
)

// The pacing of synthetic input. Applications read input as a stream of events, and some (menus,
// drag sources, games) drop what arrives faster than a hand could make it.
const (
	clickGap     = 40 * time.Millisecond // between the clicks of a double click: well inside any double-click time
	keyGap       = 8 * time.Millisecond  // between chords
	dragStep     = 12.0                  // desktop units between the pointer's stops along a drag
	dragStepWait = 8 * time.Millisecond
	dragMaxSteps = 60
	scrollGap    = 15 * time.Millisecond
	typeChunk    = 40 // characters typed between two looks at whether to stop
	maxScroll    = 50
	maxRepeat    = 50
	maxPath      = 32
)

// Point is a position in a screenshot's pixels.
type Point struct {
	X int `json:"x"`
	Y int `json:"y"`
}

// ActParams is act's.
type ActParams struct {
	Session string `json:"session"`
	Action  string `json:"action"`
	// ScreenshotID is the screenshot X and Y (and Path) were read from. A point without one is
	// refused: a pixel means nothing without the picture it is a pixel of.
	ScreenshotID string `json:"screenshot_id,omitempty"`
	X            *int   `json:"x,omitempty"`
	Y            *int   `json:"y,omitempty"`
	// Button is left (the default), right or middle; Count is 1–3 clicks.
	Button string `json:"button,omitempty"`
	Count  int    `json:"count,omitempty"`
	// Mods are held around a click: ctrl, alt, shift, super.
	Mods []string `json:"mods,omitempty"`
	// Path is a drag's points, the first where the button goes down and the last where it comes up.
	Path []Point `json:"path,omitempty"`
	// DX and DY are wheel notches: DY > 0 scrolls down, DX > 0 right.
	DX int `json:"dx,omitempty"`
	DY int `json:"dy,omitempty"`
	// Text is typed whatever the keyboard layout.
	Text string `json:"text,omitempty"`
	// Keys are chords separated by spaces ("ctrl+s", "alt+tab enter"); Repeat presses them again.
	Keys   string `json:"keys,omitempty"`
	Repeat int    `json:"repeat,omitempty"`
	// Ms is how long wait waits.
	Ms int64 `json:"ms,omitempty"`
}

// Target is the window an action landed on, by identity.
type Target struct {
	Window string      `json:"window"`
	App    backend.App `json:"app"`
}

// ActResult is act's answer.
type ActResult struct {
	Action string `json:"action"`
	// Points are where the action went, in desktop units.
	Points [][2]float64 `json:"points,omitempty"`
	Target *Target      `json:"target,omitempty"`
	// Cursor is the pointer afterwards, in desktop units.
	Cursor     [2]float64 `json:"cursor"`
	DurationMs int64      `json:"duration_ms"`
	// ActionsLeft is what the lease still allows.
	ActionsLeft int `json:"actions_left"`
}

// plan is an action checked and ready to run.
type plan struct {
	p       ActParams
	points  [][2]float64
	button  backend.Button
	count   int
	mods    []keys.Mod
	chords  []keys.Chord
	repeat  int
	target  *backend.Window
	segment []keys.Segment
}

// Act runs one action for the session holding the lease.
func (d *Desk) Act(ctx context.Context, p ActParams) (ActResult, error) {
	start := d.now()
	pl, lease, err := d.check(ctx, p)
	var res ActResult
	if err == nil {
		res, err = d.run(ctx, pl, lease)
	}
	res.DurationMs = d.now().Sub(start).Milliseconds()
	d.auditAct(p, pl, lease, err)
	return res, err
}

func (d *Desk) auditAct(p ActParams, pl *plan, lease Lease, err error) {
	r := audit.Record{Role: "host", Event: "act", Session: p.Session, Lease: lease.ID, Action: p.Action,
		Screenshot: p.ScreenshotID, TextLength: utf8.RuneCountInString(p.Text)}
	if err != nil {
		r.Event, r.Code = "refused", codeOf(err)
	}
	if pl != nil {
		for _, pt := range pl.points {
			r.Points = append(r.Points, [2]int{int(math.Round(pt[0])), int(math.Round(pt[1]))})
		}
		if pl.p.Action == ActClick {
			r.Button, r.Count = pl.button.String(), pl.count
		}
		for _, c := range pl.chords {
			if c.Printable() {
				r.KeysHidden++
			} else {
				r.Keys = append(r.Keys, c.String())
			}
		}
		if pl.target != nil {
			r.App = identity(pl.target.App)
		}
	}
	d.write(r)
}

// identity is how the audit names an app: never by a title.
func identity(a backend.App) string {
	switch {
	case a.BundleID != "":
		return a.BundleID
	case a.ExePath != "":
		return a.ExePath
	case a.WMClass != "":
		return a.WMClass
	}
	return a.ID
}

// check validates an action against the lease, the pause, the screen and the limits, and resolves
// its points. Nothing has reached the desktop when it returns an error.
func (d *Desk) check(ctx context.Context, p ActParams) (*plan, Lease, error) {
	pl := &plan{p: p}
	if err := d.validate(pl); err != nil {
		return nil, Lease{}, err
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
	if err != nil {
		return pl, lease, err
	}
	if p.Action == ActWait {
		return pl, lease, nil
	}
	if err := d.checkSecure(ctx); err != nil {
		return pl, lease, err
	}
	// Points: every one is read off the screenshot it names, then judged where it lands.
	var raw []Point
	switch {
	case p.Action == ActDrag:
		raw = p.Path
	case p.X != nil:
		raw = []Point{{*p.X, *p.Y}}
	}
	if len(raw) > 0 {
		s, err := d.resolveShot(ctx, p.ScreenshotID, lease)
		if err != nil {
			return pl, lease, err
		}
		for _, pt := range raw {
			x, y, err := s.Frame.ToDesktop(pt.X, pt.Y)
			if err != nil {
				return pl, lease, refuse(CodeOutOfScope, err.Error(), map[string]any{"point": pt})
			}
			pl.points = append(pl.points, [2]float64{x, y})
		}
	} else if p.Action == ActClick || p.Action == ActScroll {
		// No point: at the pointer, wherever it is, still judged where it lands.
		x, y, err := d.be.Cursor(ctx)
		if err != nil {
			return pl, lease, fromBackend(err)
		}
		pl.target = nil
		w, err := d.pointTarget(ctx, lease, x, y)
		if err != nil {
			return pl, lease, err
		}
		pl.target = &w
	}
	for i, pt := range pl.points {
		w, err := d.pointTarget(ctx, lease, pt[0], pt[1])
		if err != nil {
			return pl, lease, err
		}
		if i == 0 {
			pl.target = &w
		}
	}
	if p.Action == ActType || p.Action == ActKey || (p.Action == ActClick && len(pl.mods) > 0) {
		w, err := d.keyboardTarget(ctx, lease)
		if err != nil {
			return pl, lease, err
		}
		if pl.target == nil {
			pl.target = &w
		}
	}
	if err := d.spend(lease); err != nil {
		return pl, lease, err
	}
	return pl, lease, nil
}

// validate checks the parameters of the action alone.
func (d *Desk) validate(pl *plan) error {
	p := pl.p
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	if (p.X == nil) != (p.Y == nil) {
		return bad("give x and y together")
	}
	if p.X != nil && p.ScreenshotID == "" {
		return bad("a point needs the screenshot_id it was read from")
	}
	switch p.Action {
	case ActMove:
		if p.X == nil {
			return bad("move needs x and y")
		}
	case ActClick:
		pl.button = backend.Left
		switch p.Button {
		case "", "left":
		case "right":
			pl.button = backend.Right
		case "middle":
			pl.button = backend.Middle
		default:
			return bad("button must be left, right or middle")
		}
		pl.count = p.Count
		if pl.count == 0 {
			pl.count = 1
		}
		if pl.count < 1 || pl.count > 3 {
			return bad("count must be 1–3")
		}
		for _, m := range p.Mods {
			c, err := keys.ParseChord(m)
			if err != nil || len(c.Mods) > 0 || !isMod(c.Key.Name) {
				return bad("mods: %q is not ctrl, alt, shift or super", m)
			}
			pl.mods = append(pl.mods, keys.Mod(c.Key.Name))
		}
	case ActDrag:
		if len(p.Path) < 2 || len(p.Path) > maxPath {
			return bad("a drag's path has 2–%d points", maxPath)
		}
		if p.ScreenshotID == "" {
			return bad("a drag needs the screenshot_id its path was read from")
		}
		if p.X != nil {
			return bad("a drag takes path, not x and y")
		}
	case ActScroll:
		if p.DX == 0 && p.DY == 0 {
			return bad("scroll needs dx or dy")
		}
		if abs(p.DX) > maxScroll || abs(p.DY) > maxScroll {
			return bad("scroll at most %d notches at a time", maxScroll)
		}
	case ActType:
		n := utf8.RuneCountInString(p.Text)
		if n == 0 {
			return bad("type needs text")
		}
		if n > d.cfg.MaxTypeChars {
			return wire.Errorf(wire.CodeLimit, "type at most %d characters a call", d.cfg.MaxTypeChars)
		}
		if !utf8.ValidString(p.Text) {
			return bad("text is not UTF-8")
		}
		if p.X != nil {
			return bad("type goes to the window with the keyboard; click it first")
		}
		pl.segment = keys.Split(p.Text)
	case ActKey:
		chords, err := keys.ParseSequence(p.Keys)
		if err != nil {
			return bad("keys: %v", err)
		}
		pl.chords = chords
		pl.repeat = p.Repeat
		if pl.repeat == 0 {
			pl.repeat = 1
		}
		if pl.repeat < 1 || pl.repeat > maxRepeat {
			return bad("repeat must be 1–%d", maxRepeat)
		}
		if p.X != nil {
			return bad("keys go to the window with the keyboard; click it first")
		}
	case ActWait:
		if p.Ms <= 0 || p.Ms > d.cfg.MaxWaitMs {
			return bad("wait 1–%d ms", d.cfg.MaxWaitMs)
		}
	default:
		return bad("action must be move, click, drag, scroll, type, key or wait")
	}
	return nil
}

func isMod(name string) bool {
	switch keys.Mod(name) {
	case keys.Ctrl, keys.Alt, keys.Shift, keys.Super:
		return true
	}
	return false
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

// windowInScope reports whether a window is one the lease covers.
func (d *Desk) windowInScope(l Lease, w backend.Window) error {
	ok := true
	switch l.Scope.Kind {
	case ScopeWindow:
		ok = w.ID == l.Scope.Window || (l.windowPID != 0 && w.App.PID == l.windowPID && w.App.ID == l.windowApp)
	case ScopeApp:
		ok = w.App.ID == l.Scope.App
	case ScopeDisplay:
		d.mu.Lock()
		ds := d.displays
		d.mu.Unlock()
		disp, found := geom.DisplayOf(ds, w.Bounds)
		ok = found && disp.ID == l.Scope.Display
	}
	if !ok {
		return refuse(CodeOutOfScope, "that window is outside what the lease covers", map[string]any{"scope": l.Scope, "window": w.ID})
	}
	return nil
}

// pointTarget judges where a point lands: on a protected app, on a window input cannot reach, or
// outside the lease.
func (d *Desk) pointTarget(ctx context.Context, l Lease, x, y float64) (backend.Window, error) {
	w, found, err := d.be.WindowAt(ctx, x, y)
	if err != nil {
		return backend.Window{}, fromBackend(err)
	}
	pt := [2]float64{x, y}
	if found && d.Protected(w.App) {
		return w, refuse(CodeProtected, "that point is on an app the daemon never touches", map[string]any{"app": w.App.ID, "point": pt})
	}
	if found && w.App.Elevated {
		return w, refuse(CodeUnreachable, "that window runs with more rights than the daemon: the system drops input to it",
			map[string]any{"reason": "elevated", "app": w.App.ID})
	}
	d.mu.Lock()
	ds := d.displays
	d.mu.Unlock()
	switch l.Scope.Kind {
	case ScopeWindow, ScopeApp:
		if !found {
			return w, refuse(CodeOutOfScope, "that point is on no window the lease covers", map[string]any{"scope": l.Scope, "point": pt})
		}
		return w, d.windowInScope(l, w)
	case ScopeDisplay:
		if disp, ok := geom.DisplayAt(ds, x, y); !ok || disp.ID != l.Scope.Display {
			return w, refuse(CodeOutOfScope, "that point is on another display", map[string]any{"scope": l.Scope, "point": pt})
		}
	default:
		if _, ok := geom.DisplayAt(ds, x, y); !ok {
			return w, refuse(CodeOutOfScope, "that point is on no display", map[string]any{"point": pt})
		}
	}
	return w, nil
}

// keyboardTarget judges the window that would receive keys.
func (d *Desk) keyboardTarget(ctx context.Context, l Lease) (backend.Window, error) {
	w, found, err := d.be.Foreground(ctx)
	if err != nil {
		return backend.Window{}, fromBackend(err)
	}
	if !found {
		if l.Scope.Kind == ScopeDesktop {
			return w, nil
		}
		return w, refuse(CodeOutOfScope, "no window the lease covers has the keyboard: click one first", map[string]any{"scope": l.Scope})
	}
	if d.Protected(w.App) {
		return w, refuse(CodeProtected, "the window with the keyboard belongs to an app the daemon never touches", map[string]any{"app": w.App.ID})
	}
	if w.App.Elevated {
		return w, refuse(CodeUnreachable, "the window with the keyboard runs with more rights than the daemon: the system drops input to it",
			map[string]any{"reason": "elevated", "app": w.App.ID})
	}
	if l.Scope.Kind == ScopeDesktop {
		return w, nil
	}
	if err := d.windowInScope(l, w); err != nil {
		return w, refuse(CodeOutOfScope, "the window with the keyboard is outside what the lease covers: click one inside it first",
			map[string]any{"scope": l.Scope, "window": w.ID})
	}
	return w, nil
}

// spend takes one action from the rate limit and from the lease's allowance.
func (d *Desk) spend(lease Lease) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lease == nil || d.lease.ID != lease.ID {
		return refuse(CodeNoLease, "the lease ended", map[string]any{"reason": d.ended.reason})
	}
	if d.lease.Actions >= d.lease.MaxActions {
		return refuse(CodeRate, fmt.Sprintf("the lease's %d actions are used up: the operator must grant a new one", d.lease.MaxActions),
			map[string]any{"limit": "lease_actions", "max": d.lease.MaxActions})
	}
	now := d.now()
	if !d.refilledAt.IsZero() {
		d.tokens = math.Min(float64(d.cfg.ActionBurst), d.tokens+now.Sub(d.refilledAt).Seconds()*d.cfg.ActionsPerSecond)
	}
	d.refilledAt = now
	if d.tokens < 1 {
		wait := time.Duration((1 - d.tokens) / d.cfg.ActionsPerSecond * float64(time.Second))
		return refuse(CodeRate, fmt.Sprintf("more than %g actions a second", d.cfg.ActionsPerSecond),
			map[string]any{"limit": "per_second", "retry_after_ms": wait.Milliseconds() + 1})
	}
	d.tokens--
	d.lease.Actions++
	return nil
}

func (d *Desk) pausedErrLocked() error {
	h := d.humanStateLocked()
	return refuse(CodeHumanDriving, "the operator is using the mouse or keyboard: wait for them to stop, or for the operator to resume", h)
}

// run sends a checked action's input.
func (d *Desk) run(ctx context.Context, pl *plan, lease Lease) (ActResult, error) {
	d.input.Lock()
	defer d.input.Unlock()
	actx, cancel := context.WithCancel(ctx)
	defer cancel()
	d.mu.Lock()
	if d.lease == nil || d.lease.ID != lease.ID {
		d.mu.Unlock()
		return ActResult{}, refuse(CodeNoLease, "the lease ended", map[string]any{"reason": d.ended.reason})
	}
	if d.paused {
		err := d.pausedErrLocked()
		d.mu.Unlock()
		return ActResult{}, err
	}
	d.actCancel = cancel
	d.mu.Unlock()
	// The last input is marked by each step that sends one, not here: an action that sent nothing
	// (a wait) must not open a window in which the operator's next key passes for the daemon's. For
	// the same reason the watch for the operator's hand is held off only while a step sends, so a
	// hand on the mouse during a long drag or wait stops it at its next step.
	defer func() {
		d.mu.Lock()
		d.actCancel = nil
		d.mu.Unlock()
	}()

	r := runner{d: d, ctx: actx, lease: lease.ID}
	err := r.do(pl)
	if err != nil {
		// Whatever the action left down comes up, so a stopped drag never leaves a button held.
		d.release()
	}
	res := ActResult{Action: pl.p.Action, Points: pl.points}
	if pl.target != nil {
		res.Target = &Target{Window: pl.target.ID, App: pl.target.App}
	}
	// The pointer's real position is read back, not assumed: the system may round a move, and the
	// watch for the operator's hand compares against what is really there.
	if x, y, cerr := d.be.Cursor(ctx); cerr == nil {
		res.Cursor = [2]float64{x, y}
		d.mu.Lock()
		d.ourX, d.ourY, d.ourKnown = x, y, true
		d.mu.Unlock()
	}
	d.mu.Lock()
	if d.lease != nil && d.lease.ID == lease.ID {
		res.ActionsLeft = d.lease.MaxActions - d.lease.Actions
	}
	d.mu.Unlock()
	return res, err
}

// runner sends the events of one action, looking before each step at whether it must stop.
type runner struct {
	d     *Desk
	ctx   context.Context
	lease string
}

// interrupted is why the action must stop now: the kill switch, the lease's end, or the operator's
// hand.
func (r runner) interrupted() error {
	d := r.d
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lease == nil || d.lease.ID != r.lease {
		reason := d.ended.reason
		return refuse(CodeNoLease, "the lease ended during the action ("+reason+")", map[string]any{"reason": reason})
	}
	if d.paused {
		return d.pausedErrLocked()
	}
	if err := r.ctx.Err(); err != nil {
		return refuse(CodeNoLease, "the action was stopped", map[string]any{"reason": "stopped"})
	}
	return nil
}

func (r runner) sleep(t time.Duration) error {
	select {
	case <-time.After(t):
		return r.interrupted()
	case <-r.ctx.Done():
		return r.interrupted()
	}
}

// step runs one backend call after checking that the action may go on, and marks the input as the
// daemon's own.
func (r runner) step(f func(ctx context.Context) error) error {
	if err := r.interrupted(); err != nil {
		return err
	}
	r.d.mu.Lock()
	r.d.injecting = true
	r.d.injectSerial++
	r.d.mu.Unlock()
	err := f(r.ctx)
	r.d.mu.Lock()
	r.d.injecting = false
	r.d.injectSerial++
	r.d.lastInject = r.d.now()
	r.d.mu.Unlock()
	if err != nil {
		if ierr := r.interrupted(); ierr != nil {
			return ierr
		}
		return fromBackend(err)
	}
	return nil
}

// move moves the pointer and records where it left it, which is what the watch for the operator's
// hand compares the pointer with between steps.
func (r runner) move(x, y float64) error {
	err := r.step(func(ctx context.Context) error { return r.d.be.Move(ctx, x, y) })
	if err == nil {
		r.d.mu.Lock()
		r.d.ourX, r.d.ourY, r.d.ourKnown = x, y, true
		r.d.mu.Unlock()
	}
	return err
}

func (r runner) button(b backend.Button, down bool) error {
	err := r.step(func(ctx context.Context) error { return r.d.be.Button(ctx, b, down) })
	if err == nil {
		r.d.mu.Lock()
		if down {
			r.d.heldButtons[b] = true
		} else {
			delete(r.d.heldButtons, b)
		}
		r.d.mu.Unlock()
	}
	return err
}

func (r runner) key(k keys.Key, down bool) error {
	err := r.step(func(ctx context.Context) error { return r.d.be.Key(ctx, k, down) })
	if err == nil {
		r.d.mu.Lock()
		if down {
			r.d.heldKeys = append(r.d.heldKeys, heldKey{k})
		} else {
			for i := len(r.d.heldKeys) - 1; i >= 0; i-- {
				if r.d.heldKeys[i].k == k {
					r.d.heldKeys = append(r.d.heldKeys[:i], r.d.heldKeys[i+1:]...)
					break
				}
			}
		}
		r.d.mu.Unlock()
	}
	return err
}

func (r runner) chord(c keys.Chord) error {
	for _, m := range c.Mods {
		if err := r.key(keys.Key{Name: string(m)}, true); err != nil {
			return err
		}
	}
	if err := r.key(c.Key, true); err != nil {
		return err
	}
	if err := r.key(c.Key, false); err != nil {
		return err
	}
	for i := len(c.Mods) - 1; i >= 0; i-- {
		if err := r.key(keys.Key{Name: string(c.Mods[i])}, false); err != nil {
			return err
		}
	}
	return nil
}

func (r runner) do(pl *plan) error {
	p := pl.p
	switch p.Action {
	case ActWait:
		return r.sleep(time.Duration(p.Ms) * time.Millisecond)
	case ActMove:
		return r.move(pl.points[0][0], pl.points[0][1])
	case ActClick:
		if len(pl.points) > 0 {
			if err := r.move(pl.points[0][0], pl.points[0][1]); err != nil {
				return err
			}
		}
		for _, m := range pl.mods {
			if err := r.key(keys.Key{Name: string(m)}, true); err != nil {
				return err
			}
		}
		for i := 0; i < pl.count; i++ {
			if i > 0 {
				if err := r.sleep(clickGap); err != nil {
					return err
				}
			}
			if err := r.button(pl.button, true); err != nil {
				return err
			}
			if err := r.button(pl.button, false); err != nil {
				return err
			}
		}
		for i := len(pl.mods) - 1; i >= 0; i-- {
			if err := r.key(keys.Key{Name: string(pl.mods[i])}, false); err != nil {
				return err
			}
		}
		return nil
	case ActDrag:
		pts := pl.points
		if err := r.move(pts[0][0], pts[0][1]); err != nil {
			return err
		}
		if err := r.button(backend.Left, true); err != nil {
			return err
		}
		for i := 1; i < len(pts); i++ {
			from, to := pts[i-1], pts[i]
			dist := math.Hypot(to[0]-from[0], to[1]-from[1])
			steps := min(dragMaxSteps, max(1, int(dist/dragStep)))
			for s := 1; s <= steps; s++ {
				f := float64(s) / float64(steps)
				if err := r.move(from[0]+(to[0]-from[0])*f, from[1]+(to[1]-from[1])*f); err != nil {
					return err
				}
				if err := r.sleep(dragStepWait); err != nil {
					return err
				}
			}
		}
		return r.button(backend.Left, false)
	case ActScroll:
		if len(pl.points) > 0 {
			if err := r.move(pl.points[0][0], pl.points[0][1]); err != nil {
				return err
			}
		}
		// One notch at a time, as a wheel turns: many programs scroll a fixed amount per event.
		dx, dy := p.DX, p.DY
		for dx != 0 || dy != 0 {
			sx, sy := sign(dx), sign(dy)
			if err := r.step(func(ctx context.Context) error { return r.d.be.Scroll(ctx, sx, sy) }); err != nil {
				return err
			}
			dx, dy = dx-sx, dy-sy
			if dx != 0 || dy != 0 {
				if err := r.sleep(scrollGap); err != nil {
					return err
				}
			}
		}
		return nil
	case ActType:
		for _, seg := range pl.segment {
			if seg.Key != "" {
				if err := r.chord(keys.Chord{Key: keys.Key{Name: seg.Key}}); err != nil {
					return err
				}
				continue
			}
			runes := []rune(seg.Text)
			for len(runes) > 0 {
				n := min(typeChunk, len(runes))
				chunk := string(runes[:n])
				runes = runes[n:]
				if err := r.step(func(ctx context.Context) error { return r.d.be.Type(ctx, chunk) }); err != nil {
					return err
				}
			}
		}
		return nil
	case ActKey:
		for i := 0; i < pl.repeat; i++ {
			for j, c := range pl.chords {
				if i > 0 || j > 0 {
					if err := r.sleep(keyGap); err != nil {
						return err
					}
				}
				if err := r.chord(c); err != nil {
					return err
				}
			}
		}
		return nil
	}
	return errors.New("unreachable action")
}

func sign(v int) int {
	switch {
	case v > 0:
		return 1
	case v < 0:
		return -1
	}
	return 0
}

type heldKey struct{ k keys.Key }

// release lets go of every button and key the daemon holds down. The caller holds the input lock.
func (d *Desk) release() {
	d.mu.Lock()
	buttons := d.heldButtons
	held := d.heldKeys
	d.heldButtons = map[backend.Button]bool{}
	d.heldKeys = nil
	if len(buttons) == 0 && len(held) == 0 {
		d.mu.Unlock()
		return
	}
	// Releases are the daemon's own input, not the operator's.
	d.injecting = true
	d.injectSerial++
	d.mu.Unlock()
	defer func() {
		d.mu.Lock()
		d.injecting = false
		d.injectSerial++
		d.lastInject = d.now()
		d.mu.Unlock()
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for b := range buttons {
		if err := d.be.Button(ctx, b, false); err != nil {
			d.log.Warn("releasing a button", "button", b.String(), "error", err.Error())
		}
	}
	for i := len(held) - 1; i >= 0; i-- {
		if err := d.be.Key(ctx, held[i].k, false); err != nil {
			d.log.Warn("releasing a key", "key", held[i].k.String(), "error", err.Error())
		}
	}
}
