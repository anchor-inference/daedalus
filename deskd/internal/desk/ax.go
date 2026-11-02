package desk

import (
	"context"
	"encoding/json"
	"errors"
	"time"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/ax"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The accessibility tree's codes (docs/architecture/computer.md, The accessibility tree).
const (
	CodeAXUnavailable = 1209 // no tree for this call: no tool, a broken tool, or a window without one
	CodeStaleRef      = 1210 // the element reference cannot be used
	CodeAmbiguous     = 1211 // the reference or the window matches more than one element or window
	CodeAXFailed      = 1213 // the tool ran the call and it failed
)

// The limits of the accessibility calls.
const (
	// keptAXSnapshots is how many of the tool's snapshots a lease keeps references of: a model that
	// drills into a few regions of two windows stays well inside it.
	keptAXSnapshots = 32
	// maxAXRefs bounds the references kept of one snapshot. A tree with more is answered whole, and
	// a reference past the bound is refused as unknown.
	maxAXRefs = 20000
	// maxAXAnswer is the largest tree or match list a reply carries: a frame is 1 MiB, and an
	// answer this size is already far more than a model should read in one go.
	maxAXAnswer           = 768 << 10
	defaultAXSnapshotMs   = 3000
	defaultAXMs           = 5000
	minAXMs, maxAXMs      = 500, 30000
	defaultFindLimit      = 50
	maxFindLimit          = 200
	defaultScrollAmount   = 3
	maxAXScrollAmount     = 50
	axStaleReasonUnknown  = "unknown"
	axStaleReasonOld      = "old"
	axStaleReasonGone     = "window_gone"
	axStaleReasonChanged  = "changed"
	axStaleReasonExpired  = "expired"
	axStaleReasonNotFound = "not_found"
)

// axSnapshot is what the daemon remembers of one of the tool's snapshots: the window it was taken
// of, and every reference it handed out from it. A reference is honoured only when it is here.
type axSnapshot struct {
	id     string
	lease  string
	window string
	tool   string
	at     time.Time
	refs   map[string]ax.RefInfo
}

// axBinding is a window of the daemon matched to the tool's, valid while the window keeps the bounds
// it had when it was matched.
type axBinding struct {
	tool   string
	window backend.Window
}

// SetAX gives the desk its accessibility tool. Called once, before the daemon serves.
func (d *Desk) SetAX(t *ax.Tool) { d.ax = t }

// AXStatus is whether the tree is served.
func (d *Desk) AXStatus() ax.Status { return d.ax.Status() }

// retireAXLocked forgets a lease's snapshots and window matches, kills any of its children still
// running, and deletes the tool's state for it: its reference maps hold the screen's text.
func (d *Desk) retireAXLocked(lease string) {
	for _, cancel := range d.axCancels {
		cancel()
	}
	d.axSnaps = map[string]*axSnapshot{}
	d.axOrder = nil
	d.axWindows = map[string]axBinding{}
	if lease != "" {
		go d.ax.EndLease(lease)
	}
}

// axCall is a call checked through the window: the lease, the reference and the window it acts in.
type axCall struct {
	method string
	lease  Lease
	window backend.Window
	snap   *axSnapshot
	ref    string
	info   ax.RefInfo
}

// axBegin runs the checks every accessibility call shares, in the contract's order: the lease, the
// reference, the pause (for an action), the locked screen, then the window read fresh. window names
// the window when there is no reference; empty means the lease's own.
func (d *Desk) axBegin(ctx context.Context, method, session, ref, window string, act bool) (axCall, error) {
	c := axCall{method: method, ref: ref}
	d.mu.Lock()
	l, err := d.holderLocked(session)
	if l != nil {
		c.lease = *l
	}
	if err == nil && ref != "" {
		c.snap, c.info, err = d.axRefLocked(ref, c.lease)
	}
	if err == nil && act && d.paused {
		err = d.pausedErrLocked()
	}
	d.mu.Unlock()
	if err != nil {
		return c, err
	}
	if err := d.checkSecure(ctx); err != nil {
		return c, err
	}
	id := window
	switch {
	case c.snap != nil:
		id = c.snap.window
	case id == "" && c.lease.Scope.Kind == ScopeWindow:
		id = c.lease.Scope.Window
	case id == "":
		return c, wire.Errorf(wire.CodeInvalidParams, "give window: the lease covers more than one")
	}
	w, err := d.findWindow(ctx, id)
	if err != nil {
		if c.snap != nil {
			return c, refuse(CodeStaleRef, "the window of that reference is gone: take a new snapshot",
				map[string]any{"ref": ref, "reason": axStaleReasonGone})
		}
		return c, err
	}
	c.window = w
	if err := d.axWindowAllowed(c.lease, w); err != nil {
		return c, err
	}
	if !d.ax.Status().Available {
		return c, axUnavailable(d.ax.Status().Reason, "")
	}
	return c, nil
}

// axWindowAllowed is step 6 for one window: protected, elevated, in scope. A covered or background
// window passes: an element action needs neither the pointer nor the keyboard.
func (d *Desk) axWindowAllowed(l Lease, w backend.Window) error {
	if d.Protected(w.App) {
		return refuse(CodeProtected, "that window belongs to an app the daemon never touches", map[string]any{"app": w.App.ID})
	}
	if w.App.Elevated {
		return refuse(CodeUnreachable, "that window runs with more rights than the daemon: the system keeps it out of reach",
			map[string]any{"reason": "elevated", "app": w.App.ID})
	}
	if l.Scope.Kind == ScopeDesktop {
		return nil
	}
	return d.windowInScope(l, w)
}

// axRefLocked finds the snapshot a reference came from, or says why it cannot be used.
func (d *Desk) axRefLocked(ref string, l Lease) (*axSnapshot, ax.RefInfo, error) {
	stale := func(reason, msg string) error {
		return refuse(CodeStaleRef, msg+": take a new snapshot", map[string]any{"ref": ref, "reason": reason})
	}
	id, ok := ax.SnapshotOf(ref)
	if !ok {
		return nil, ax.RefInfo{}, wire.Errorf(wire.CodeInvalidParams, "ref must be @<snapshot_id>:e<n> as a snapshot gave it")
	}
	s := d.axSnaps[id]
	if s == nil || s.lease != l.ID {
		return nil, ax.RefInfo{}, stale(axStaleReasonUnknown, "the daemon did not hand out that reference under this lease")
	}
	info, ok := s.refs[ref]
	if !ok {
		return nil, ax.RefInfo{}, stale(axStaleReasonUnknown, "the daemon did not hand out that reference under this lease")
	}
	if d.now().Sub(s.at) > time.Duration(d.cfg.ScreenshotTTLs)*time.Second {
		return nil, ax.RefInfo{}, stale(axStaleReasonOld, "the snapshot of that reference is too old")
	}
	return s, info, nil
}

func axUnavailable(reason, detail string) error {
	msg := "the accessibility tree is not available here (" + reason + "): use screenshots and points"
	if detail != "" {
		msg += ": " + detail
	}
	return refuse(CodeAXUnavailable, msg, map[string]any{"reason": reason})
}

// axToolWindow is the tool's id of a window, matched once per lease and again when the window moves.
func (d *Desk) axToolWindow(ctx context.Context, c axCall) (string, error) {
	system := d.be.Platform().OS
	if ax.MappingFor(system) == ax.MapDirect {
		id, err := ax.DirectID(system, c.window.ID)
		if err != nil {
			return "", axUnavailable(ax.ReasonNoWindow, err.Error())
		}
		return id, nil
	}
	d.mu.Lock()
	b, ok := d.axWindows[c.window.ID]
	d.mu.Unlock()
	if ok && b.window.Bounds == c.window.Bounds && b.window.App.PID == c.window.App.PID {
		return b.tool, nil
	}
	raw, err := d.axRun(ctx, c, false, defaultAXMs, ax.ListWindowsArgs()...)
	if err != nil {
		return "", err
	}
	var ws []ax.ToolWindow
	if err := ax.DecodeData(raw, &ws); err != nil {
		return "", axUnavailable(ax.ReasonCrashed, "a window list of an unknown shape")
	}
	m := ax.MatchByOverlap(ws, c.window.App.PID, c.window.Bounds)
	if m.Ambiguous {
		return "", refuse(CodeAmbiguous, "more than one accessible window of that program sits where the window is: use screenshots there",
			map[string]any{"window": c.window.ID, "candidates": m.Candidates})
	}
	if m.ID == "" {
		return "", axUnavailable(ax.ReasonNoWindow, "the window exposes no accessibility tree the daemon can match to it")
	}
	d.mu.Lock()
	if d.lease != nil && d.lease.ID == c.lease.ID {
		d.axWindows[c.window.ID] = axBinding{tool: m.ID, window: c.window}
	}
	d.mu.Unlock()
	return m.ID, nil
}

// axRun runs the tool for a call, killing it when the lease ends, the daemon stops, or (for an
// action) the operator's hand pauses the agent.
func (d *Desk) axRun(ctx context.Context, c axCall, act bool, timeoutMs int, args ...string) (json.RawMessage, error) {
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	d.mu.Lock()
	if d.lease == nil || d.lease.ID != c.lease.ID {
		d.mu.Unlock()
		return nil, withDelivery(refuse(CodeNoLease, "the lease ended", map[string]any{"reason": d.ended.reason}), NotDelivered)
	}
	d.axSerial++
	key := d.axSerial
	d.axCancels[key] = cancel
	if act {
		d.actCancel = cancel
	}
	d.mu.Unlock()
	defer func() {
		d.mu.Lock()
		delete(d.axCancels, key)
		if act {
			d.actCancel = nil
		}
		d.mu.Unlock()
	}()
	raw, err := d.ax.Run(cctx, c.lease.ID, time.Duration(timeoutMs)*time.Millisecond, args...)
	if err != nil && cctx.Err() != nil && ctx.Err() == nil {
		return nil, d.axInterrupted(c.lease.ID)
	}
	if err != nil {
		return nil, d.fromTool(err, c.ref, c.info)
	}
	return raw, nil
}

// axInterrupted is why a child was killed: the lease ended, the operator's hand, or the stop.
func (d *Desk) axInterrupted(lease string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lease == nil || d.lease.ID != lease {
		reason := d.ended.reason
		return refuse(CodeNoLease, "the lease ended during the call ("+reason+")", map[string]any{"reason": reason})
	}
	if d.paused {
		return d.pausedErrLocked()
	}
	return refuse(CodeNoLease, "the call was stopped", map[string]any{"reason": "stopped"})
}

// fromTool turns the tool's errors into the contract's codes. Each code says what the host should do
// next: take a new snapshot (1210), narrow the target (1211), fall back to pixels (1007, 1209), or
// look before doing anything again (1213).
func (d *Desk) fromTool(err error, ref string, info ax.RefInfo) error {
	var un *ax.Unavailable
	if errors.As(err, &un) {
		return axUnavailable(un.Reason, un.Detail)
	}
	if errors.Is(err, ax.ErrTooLarge) {
		return wire.Errorf(wire.CodeLimit, "the tree is too large for one answer: ask for depth skeleton, or a root")
	}
	if errors.Is(err, ax.ErrDeadline) {
		return refuse(CodeAXFailed, "the accessibility tool did not answer in time", map[string]any{"reason": "timeout"})
	}
	var te *ax.ToolError
	if !errors.As(err, &te) {
		return err
	}
	stale := func(reason, msg string) error {
		return refuse(CodeStaleRef, msg+": take a new snapshot", map[string]any{"ref": ref, "reason": reason})
	}
	failed := func(reason, msg string) error {
		data := map[string]any{"reason": reason}
		var det struct {
			PostState *ax.ToolPostState `json:"post_state"`
		}
		if len(te.Details) > 0 && json.Unmarshal(te.Details, &det) == nil && det.PostState != nil {
			data["post_state"] = det.PostState.Post(info.Secure)
		}
		return refuse(CodeAXFailed, msg, data)
	}
	var out error
	switch te.Code {
	case "PERM_DENIED":
		out = refuse(CodeNoPermission, "the system has not granted the accessibility permission",
			map[string]any{"which": backend.PermAccessibility, "hint": PermissionHint(backend.PermAccessibility)})
	case "STALE_REF":
		out = stale(axStaleReasonChanged, "that element is no longer in the window as the snapshot saw it")
	case "SNAPSHOT_NOT_FOUND":
		out = stale(axStaleReasonExpired, "the accessibility tool no longer has that snapshot")
	case "ELEMENT_NOT_FOUND":
		out = stale(axStaleReasonNotFound, "no element matches that reference")
	case "WINDOW_NOT_FOUND", "APP_NOT_FOUND":
		if ref != "" {
			out = stale(axStaleReasonGone, "the window of that reference is gone")
		} else {
			out = axUnavailable(ax.ReasonNoWindow, "the accessibility tool cannot find the window")
		}
	case "AMBIGUOUS_TARGET":
		data := map[string]any{}
		if ref != "" {
			data["ref"] = ref
		}
		out = refuse(CodeAmbiguous, "more than one element matches: narrow it with ax.find or a root", data)
	case "POLICY_DENIED":
		out = &wire.Error{Code: wire.CodeUnsupported, Message: "that element offers no way to do this without real input: use a screenshot and act",
			Data: map[string]any{"reason": "needs_pointer"}}
	case "ACTION_NOT_SUPPORTED", "PLATFORM_NOT_SUPPORTED":
		out = &wire.Error{Code: wire.CodeUnsupported, Message: "that element does not offer this: use a screenshot and act",
			Data: map[string]any{"reason": "not_supported"}}
	case "ACTION_FAILED":
		var det struct {
			Kind string `json:"kind"`
		}
		_ = json.Unmarshal(te.Details, &det)
		if det.Kind == "post_action_verification" {
			out = failed("contradicted", "the element took the action but reads back otherwise: look before doing it again")
		} else {
			out = failed("rejected", "the element refused the action")
		}
	case "TIMEOUT":
		out = failed("timeout", "the element did not become ready in time")
	case "APP_UNRESPONSIVE":
		out = failed("unresponsive", "the program stopped responding")
	case "INVALID_ARGS":
		out = wire.Errorf(wire.CodeInvalidParams, "the accessibility tool refused the parameters")
	default:
		out = failed("internal", "the accessibility tool failed")
	}
	// The tool's own word on delivery decides the refusal's: only its not_delivered means nothing
	// reached the element.
	if te.Delivery == ax.ToolNotDelivered {
		return withDelivery(out, NotDelivered)
	}
	return withDelivery(out, DeliveryUnknown)
}

func axTimeout(ms, def int) (int, error) {
	if ms == 0 {
		return def, nil
	}
	if ms < minAXMs || ms > maxAXMs {
		return 0, wire.Errorf(wire.CodeInvalidParams, "timeout_ms must be %d–%d", minAXMs, maxAXMs)
	}
	return ms, nil
}

// remember keeps a snapshot's references under the call's lease. A root drilled again, or a find
// inside a snapshot, adds to the snapshot it belongs to.
func (d *Desk) remember(c axCall, id, toolWindow string, refs map[string]ax.RefInfo) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lease == nil || d.lease.ID != c.lease.ID {
		// Stopped or revoked while the tool read: the references are not handed out.
		return refuse(CodeNoLease, "the lease ended during the call", map[string]any{"reason": d.ended.reason})
	}
	s := d.axSnaps[id]
	if s == nil {
		s = &axSnapshot{id: id, lease: c.lease.ID, window: c.window.ID, tool: toolWindow, refs: map[string]ax.RefInfo{}}
		d.axSnaps[id] = s
		d.axOrder = append(d.axOrder, id)
		for len(d.axOrder) > keptAXSnapshots {
			delete(d.axSnaps, d.axOrder[0])
			d.axOrder = d.axOrder[1:]
		}
	} else if s.window != c.window.ID {
		// The tool reused an id for another window: never let one window's reference act in another.
		return refuse(CodeAXFailed, "the accessibility tool answered for another window", map[string]any{"reason": "internal"})
	}
	s.at = d.now()
	for r, info := range refs {
		if len(s.refs) >= maxAXRefs {
			break
		}
		if sid, ok := ax.SnapshotOf(r); ok && sid == id {
			s.refs[r] = info
		}
	}
	return nil
}

func (d *Desk) auditAX(c axCall, session, action string, textLen int, delivery string, err error) {
	r := audit.Record{Role: "host", Event: c.method, Session: session, Lease: c.lease.ID, Action: action, Ref: c.ref,
		RoleName: c.info.Role, TextLength: textLen, Delivery: delivery}
	if c.window.ID != "" {
		r.App = identity(c.window.App)
	}
	if err != nil {
		r.Event, r.Code, r.Reason = "refused", codeOf(err), c.method
	}
	d.write(r)
}

// AXSnapshotParams is ax.snapshot's.
type AXSnapshotParams struct {
	Session     string `json:"session"`
	Window      string `json:"window,omitempty"`
	Root        string `json:"root,omitempty"`
	Surface     string `json:"surface,omitempty"`
	Depth       string `json:"depth,omitempty"`
	Interactive *bool  `json:"interactive,omitempty"`
	Bounds      bool   `json:"bounds,omitempty"`
	TimeoutMs   int    `json:"timeout_ms,omitempty"`
}

// AXSnapshotResult is ax.snapshot's answer.
type AXSnapshotResult struct {
	SnapshotID string  `json:"snapshot_id"`
	Window     string  `json:"window"`
	Complete   bool    `json:"complete"`
	RefCount   int     `json:"ref_count"`
	Tree       ax.Node `json:"tree"`
}

// AXSnapshot reads a window's tree, or one region of a snapshot again.
func (d *Desk) AXSnapshot(ctx context.Context, p AXSnapshotParams) (AXSnapshotResult, error) {
	c := axCall{method: "ax.snapshot", ref: p.Root}
	res, err := d.axSnapshot(ctx, p, &c)
	d.auditAX(c, p.Session, "", 0, "", err)
	return res, err
}

func (d *Desk) axSnapshot(ctx context.Context, p AXSnapshotParams, c *axCall) (AXSnapshotResult, error) {
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	if p.Root != "" && (p.Window != "" || p.Surface != "") {
		return AXSnapshotResult{}, bad("root reads a region of its own snapshot: give no window or surface with it")
	}
	if p.Surface != "" && !ax.ValidSurface(p.Surface) {
		return AXSnapshotResult{}, bad("surface must be window, menu, sheet, popover or alert")
	}
	if p.Depth != "" && p.Depth != "full" && p.Depth != "skeleton" {
		return AXSnapshotResult{}, bad("depth must be full or skeleton")
	}
	timeout, err := axTimeout(p.TimeoutMs, defaultAXSnapshotMs)
	if err != nil {
		return AXSnapshotResult{}, err
	}
	*c, err = d.axBegin(ctx, c.method, p.Session, p.Root, p.Window, false)
	if err != nil {
		return AXSnapshotResult{}, err
	}
	args := ax.SnapshotArgs{Root: p.Root, Surface: p.Surface, Skeleton: p.Depth == "skeleton",
		Interactive: p.Interactive == nil || *p.Interactive, Bounds: p.Bounds, TimeoutMs: timeout}
	toolWindow := ""
	if c.snap != nil {
		toolWindow = c.snap.tool
	} else if toolWindow, err = d.axToolWindow(ctx, *c); err != nil {
		return AXSnapshotResult{}, err
	}
	args.Window = toolWindow
	raw, err := d.axRun(ctx, *c, false, timeout, args.Args()...)
	if err != nil {
		return AXSnapshotResult{}, err
	}
	var data ax.SnapshotData
	if err := ax.DecodeData(raw, &data); err != nil || data.SnapshotID == "" {
		return AXSnapshotResult{}, axUnavailable(ax.ReasonCrashed, "a snapshot of an unknown shape")
	}
	if data.Window.ID != toolWindow {
		if err := d.axSurfaceWindow(ctx, c, data.Window.ID); err != nil {
			return AXSnapshotResult{}, err
		}
		toolWindow = data.Window.ID
	}
	refs := map[string]ax.RefInfo{}
	tree := ax.Convert(data.Tree, refs)
	res := AXSnapshotResult{SnapshotID: data.SnapshotID, Window: c.window.ID, Complete: data.Complete, RefCount: data.RefCount, Tree: tree}
	if b, err := json.Marshal(res); err != nil || len(b) > maxAXAnswer {
		return AXSnapshotResult{}, wire.Errorf(wire.CodeLimit, "the tree is too large for one answer: ask for depth skeleton, or a root")
	}
	if err := d.remember(*c, data.SnapshotID, toolWindow, refs); err != nil {
		return AXSnapshotResult{}, err
	}
	return res, nil
}

// axSurfaceWindow judges a snapshot that came back from another window than the one asked for (a
// menu or a sheet with a window of its own). On Windows and macOS its id is the system's and the
// window is judged like any other; on Linux the tool's id cannot be placed, so it is refused.
func (d *Desk) axSurfaceWindow(ctx context.Context, c *axCall, toolID string) error {
	system := d.be.Platform().OS
	refuseOther := refuse(CodeOutOfScope, "the accessibility tool answered from a window the daemon cannot place inside the lease",
		map[string]any{"scope": c.lease.Scope, "reason": "surface_window"})
	if ax.MappingFor(system) != ax.MapDirect {
		return refuseOther
	}
	id, ok := ax.FromDirectID(system, toolID)
	if !ok {
		return refuseOther
	}
	w, err := d.findWindow(ctx, id)
	if err != nil {
		return refuseOther
	}
	if err := d.axWindowAllowed(c.lease, w); err != nil {
		return err
	}
	c.window = w
	return nil
}

// AXFindParams is ax.find's.
type AXFindParams struct {
	Session   string `json:"session"`
	Window    string `json:"window,omitempty"`
	Root      string `json:"root,omitempty"`
	Role      string `json:"role,omitempty"`
	Name      string `json:"name,omitempty"`
	Value     string `json:"value,omitempty"`
	Text      string `json:"text,omitempty"`
	Exact     bool   `json:"exact,omitempty"`
	Limit     int    `json:"limit,omitempty"`
	TimeoutMs int    `json:"timeout_ms,omitempty"`
}

// AXFindResult is ax.find's answer.
type AXFindResult struct {
	SnapshotID   string     `json:"snapshot_id"`
	Window       string     `json:"window"`
	Matches      []ax.Match `json:"matches"`
	RolesPresent []string   `json:"roles_present,omitempty"`
}

// AXFind searches a window's tree, or a region of a snapshot.
func (d *Desk) AXFind(ctx context.Context, p AXFindParams) (AXFindResult, error) {
	c := axCall{method: "ax.find", ref: p.Root}
	res, err := d.axFind(ctx, p, &c)
	d.auditAX(c, p.Session, "", 0, "", err)
	return res, err
}

func (d *Desk) axFind(ctx context.Context, p AXFindParams, c *axCall) (AXFindResult, error) {
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	if p.Root != "" && p.Window != "" {
		return AXFindResult{}, bad("give window or root, not both")
	}
	if p.Role == "" && p.Name == "" && p.Value == "" && p.Text == "" {
		return AXFindResult{}, bad("give at least one of role, name, value, text")
	}
	limit := p.Limit
	if limit == 0 {
		limit = defaultFindLimit
	}
	if limit < 1 || limit > maxFindLimit {
		return AXFindResult{}, bad("limit must be 1–%d", maxFindLimit)
	}
	timeout, err := axTimeout(p.TimeoutMs, defaultAXMs)
	if err != nil {
		return AXFindResult{}, err
	}
	*c, err = d.axBegin(ctx, c.method, p.Session, p.Root, p.Window, false)
	if err != nil {
		return AXFindResult{}, err
	}
	args := ax.FindArgs{Root: p.Root, Role: p.Role, Name: p.Name, Value: p.Value, Text: p.Text, Exact: p.Exact, Limit: limit, TimeoutMs: timeout}
	toolWindow := ""
	if c.snap != nil {
		toolWindow = c.snap.tool
	} else if toolWindow, err = d.axToolWindow(ctx, *c); err != nil {
		return AXFindResult{}, err
	}
	args.Window = toolWindow
	raw, err := d.axRun(ctx, *c, false, timeout, args.Args()...)
	if err != nil {
		return AXFindResult{}, err
	}
	var data ax.FindData
	if err := ax.DecodeData(raw, &data); err != nil {
		return AXFindResult{}, axUnavailable(ax.ReasonCrashed, "a find answer of an unknown shape")
	}
	refs := map[string]ax.RefInfo{}
	res := AXFindResult{SnapshotID: data.SnapshotID, Window: c.window.ID, Matches: []ax.Match{}, RolesPresent: data.RolesPresent}
	for _, m := range data.Matches {
		res.Matches = append(res.Matches, ax.ConvertMatch(m, refs))
	}
	if b, err := json.Marshal(res); err != nil || len(b) > maxAXAnswer {
		return AXFindResult{}, wire.Errorf(wire.CodeLimit, "too many matches for one answer: lower limit or narrow the search")
	}
	if data.SnapshotID != "" && len(refs) > 0 {
		if err := d.remember(*c, data.SnapshotID, toolWindow, refs); err != nil {
			return AXFindResult{}, err
		}
	}
	return res, nil
}

// AXGetParams is ax.get's.
type AXGetParams struct {
	Session  string `json:"session"`
	Ref      string `json:"ref"`
	Property string `json:"property"`
}

// AXGetResult is ax.get's answer.
type AXGetResult struct {
	Ref        string `json:"ref"`
	Property   string `json:"property"`
	Value      any    `json:"value"`
	Live       *bool  `json:"live,omitempty"`
	Applicable *bool  `json:"applicable,omitempty"`
	Secure     bool   `json:"secure,omitempty"`
}

// AXGet reads one property of an element.
func (d *Desk) AXGet(ctx context.Context, p AXGetParams) (AXGetResult, error) {
	c := axCall{method: "ax.get", ref: p.Ref}
	res, err := d.axGet(ctx, p, &c)
	d.auditAX(c, p.Session, p.Property, 0, "", err)
	return res, err
}

func (d *Desk) axGet(ctx context.Context, p AXGetParams, c *axCall) (AXGetResult, error) {
	boolean, ok := ax.IsBoolProperty(p.Property)
	if !ok {
		return AXGetResult{}, wire.Errorf(wire.CodeInvalidParams,
			"property must be text, value, title, bounds, role, states, visible, enabled, checked, focused, expanded or selected")
	}
	if p.Ref == "" {
		return AXGetResult{}, wire.Errorf(wire.CodeInvalidParams, "give ref")
	}
	var err error
	*c, err = d.axBegin(ctx, c.method, p.Session, p.Ref, "", false)
	if err != nil {
		return AXGetResult{}, err
	}
	res := AXGetResult{Ref: p.Ref, Property: p.Property}
	if c.info.Secure && (p.Property == "text" || p.Property == "value") {
		// The value of a password field is never read at all, so it is never in the daemon's memory.
		res.Secure = true
		return res, nil
	}
	raw, err := d.axRun(ctx, *c, false, defaultAXMs, ax.GetArgs(p.Ref, p.Property)...)
	if err != nil {
		return AXGetResult{}, err
	}
	if boolean {
		var data struct {
			Result     bool  `json:"result"`
			Applicable *bool `json:"applicable"`
		}
		if err := ax.DecodeData(raw, &data); err != nil {
			return AXGetResult{}, axUnavailable(ax.ReasonCrashed, "an answer of an unknown shape")
		}
		res.Value, res.Applicable = data.Result, data.Applicable
		return res, nil
	}
	var data struct {
		Value json.RawMessage `json:"value"`
		Live  *bool           `json:"live"`
	}
	if err := ax.DecodeData(raw, &data); err != nil {
		return AXGetResult{}, axUnavailable(ax.ReasonCrashed, "an answer of an unknown shape")
	}
	res.Live = data.Live
	switch p.Property {
	case "bounds":
		var r *ax.ToolRect
		if json.Unmarshal(data.Value, &r) == nil && r != nil {
			res.Value = r.Rect()
		}
	case "states":
		var states []string
		_ = json.Unmarshal(data.Value, &states)
		res.Value = states
		if ax.IsSecure(c.info.Role, states) {
			res.Secure = true
		}
	default:
		var s *string
		_ = json.Unmarshal(data.Value, &s)
		res.Value = s
	}
	if len(data.Value) > maxAXAnswer {
		return AXGetResult{}, wire.Errorf(wire.CodeLimit, "that value is too large for one answer")
	}
	return res, nil
}

// AXActParams is ax.act's.
type AXActParams struct {
	Session   string `json:"session"`
	Ref       string `json:"ref"`
	Action    string `json:"action"`
	Text      string `json:"text,omitempty"`
	Direction string `json:"direction,omitempty"`
	Amount    int    `json:"amount,omitempty"`
	TimeoutMs int    `json:"timeout_ms,omitempty"`
}

// AXActResult is ax.act's answer.
type AXActResult struct {
	Action      string        `json:"action"`
	Ref         string        `json:"ref"`
	Window      string        `json:"window"`
	Delivery    string        `json:"delivery"`
	Verified    bool          `json:"verified"`
	PostState   *ax.PostState `json:"post_state,omitempty"`
	Surfaces    []string      `json:"surfaces,omitempty"`
	ActionsLeft int           `json:"actions_left"`
	DurationMs  int64         `json:"duration_ms"`
}

// AXAct runs one element action for the session holding the lease. Its refusals say whether the
// element may have taken it, as act's do.
func (d *Desk) AXAct(ctx context.Context, p AXActParams) (AXActResult, error) {
	start := d.now()
	c := axCall{method: "ax.act", ref: p.Ref}
	res, sent, err := d.axAct(ctx, p, &c)
	res.DurationMs = d.now().Sub(start).Milliseconds()
	delivery := res.Delivery
	if err != nil {
		var we *wire.Error
		if errors.As(err, &we) {
			if m, ok := we.Data.(map[string]any); ok {
				if v, ok := m["delivery"].(string); ok {
					delivery = v
				}
			}
		}
		if delivery == "" {
			delivery = NotDelivered
			if sent {
				delivery = DeliveryUnknown
			}
			err = withDelivery(err, delivery)
		}
	}
	d.auditAX(c, p.Session, p.Action, utf8.RuneCountInString(p.Text), delivery, err)
	return res, err
}

func (d *Desk) axAct(ctx context.Context, p AXActParams, c *axCall) (AXActResult, bool, error) {
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	takesText, ok := ax.TakesText(p.Action)
	if !ok {
		return AXActResult{}, false, bad("action must be click, set_value, clear, type, select, toggle, check, uncheck, expand, collapse, scroll, scroll_to or focus")
	}
	if p.Ref == "" {
		return AXActResult{}, false, bad("give ref")
	}
	n := utf8.RuneCountInString(p.Text)
	switch {
	case takesText && (n == 0 || !utf8.ValidString(p.Text)):
		return AXActResult{}, false, bad("%s needs text", p.Action)
	case takesText && n > d.cfg.MaxTypeChars:
		return AXActResult{}, false, wire.Errorf(wire.CodeLimit, "text is at most %d characters a call", d.cfg.MaxTypeChars)
	case !takesText && p.Text != "":
		return AXActResult{}, false, bad("%s takes no text", p.Action)
	}
	direction, amount := p.Direction, p.Amount
	if p.Action == ax.ActScroll {
		if direction == "" {
			direction = "down"
		}
		if direction != "up" && direction != "down" && direction != "left" && direction != "right" {
			return AXActResult{}, false, bad("direction must be up, down, left or right")
		}
		if amount == 0 {
			amount = defaultScrollAmount
		}
		if amount < 1 || amount > maxAXScrollAmount {
			return AXActResult{}, false, bad("amount must be 1–%d", maxAXScrollAmount)
		}
	} else if direction != "" || amount != 0 {
		return AXActResult{}, false, bad("direction and amount are for scroll")
	}
	timeout, err := axTimeout(p.TimeoutMs, defaultAXMs)
	if err != nil {
		return AXActResult{}, false, err
	}
	args, err := ax.ActArgs(p.Action, p.Ref, p.Text, direction, amount, timeout)
	if err != nil {
		return AXActResult{}, false, bad("%v", err)
	}
	*c, err = d.axBegin(ctx, c.method, p.Session, p.Ref, "", true)
	if err != nil {
		return AXActResult{}, false, err
	}
	if err := d.spend(c.lease); err != nil {
		return AXActResult{}, false, err
	}
	// The same lock as act's: an element action never runs between the events of a drag or a typed
	// text, and stop waits for it as it waits for those.
	d.input.Lock()
	defer d.input.Unlock()
	d.mu.Lock()
	if d.paused {
		err = d.pausedErrLocked()
	}
	d.mu.Unlock()
	if err != nil {
		return AXActResult{}, false, err
	}
	raw, err := d.axRun(ctx, *c, true, timeout, args...)
	if err != nil {
		// Whatever the tool said of delivery is in the refusal already; a child killed half way may
		// have acted, which axRun's refusal leaves for AXAct to mark unknown.
		return AXActResult{}, true, err
	}
	var data ax.ActionData
	if err := ax.DecodeData(raw, &data); err != nil {
		return AXActResult{}, true, axUnavailable(ax.ReasonCrashed, "an action answer of an unknown shape")
	}
	res := AXActResult{Action: p.Action, Ref: p.Ref, Window: c.window.ID, Delivery: Delivered,
		Verified: data.Disposition.Delivery == ax.ToolDeliveredVerified, PostState: data.PostState.Post(c.info.Secure)}
	if data.Disposition.Delivery == ax.ToolNotDelivered {
		// The element was already as asked: the tool sent nothing.
		res.Delivery = NotDelivered
	}
	for _, s := range data.Surfaces {
		if s.Kind != "" {
			res.Surfaces = append(res.Surfaces, s.Kind)
		}
	}
	d.mu.Lock()
	if d.lease != nil && d.lease.ID == c.lease.ID {
		res.ActionsLeft = d.lease.MaxActions - d.lease.Actions
	}
	d.mu.Unlock()
	return res, true, nil
}
