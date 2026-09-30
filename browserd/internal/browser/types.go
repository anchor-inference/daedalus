package browser

import (
	"context"
	"encoding/json"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Browser is one Chromium on one profile.
type Browser struct {
	ID        string
	Profile   string
	Ephemeral bool
	StartedAt time.Time
	Version   string

	proc        *chrome.Process
	conn        *cdp.Conn
	m           *Manager
	userAgent   string
	uaMetadata  map[string]any
	tempDir     string
	downloadDir string
	gone        chan struct{}

	// Guarded by the manager's lock.
	groups      map[string]*Group
	contexts    map[string]*Group    // a throwaway group's browser context
	claims      map[string]chan *Tab // targets a NewTab waits for
	claimGroups map[string]*Group
	early       map[string]pageAttach // pages that attached before anyone claimed them
	spare       *pageAttach

	mu           sync.Mutex
	closing      string
	lastActivity time.Time
	frameW       int // how much wider and taller a window is than the page it holds
	frameH       int
	frameKnown   bool
}

type pageAttach struct {
	session string
	info    targetInfo
}

// Conn is the browser's pipe, for the packages that speak to its pages.
func (b *Browser) Conn() *cdp.Conn { return b.conn }

// Pid is the browser's main process.
func (b *Browser) Pid() int { return b.proc.Pid }

// DownloadDir is where Chromium saves this browser's downloads.
func (b *Browser) DownloadDir() string { return b.downloadDir }

func (b *Browser) setClosing(reason string) {
	b.mu.Lock()
	if b.closing == "" {
		b.closing = reason
	}
	b.mu.Unlock()
}

// closeReason is why the browser ended: the reason it was closed for, else a crash.
func (b *Browser) closeReason() (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closing != "" {
		return b.closing, false
	}
	return "crashed", true
}

func (b *Browser) touch() {
	b.mu.Lock()
	b.lastActivity = time.Now()
	b.mu.Unlock()
}

// LastActivity is the last agent call, human input or start in any of its groups.
func (b *Browser) LastActivity() time.Time {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.lastActivity.IsZero() {
		return b.StartedAt
	}
	return b.lastActivity
}

func (b *Browser) removeTemp() {
	if b.tempDir != "" {
		_ = os.RemoveAll(b.tempDir)
	}
}

// GroupIDs lists the browser's groups.
func (b *Browser) GroupIDs() []string {
	b.m.mu.Lock()
	defer b.m.mu.Unlock()
	out := make([]string, 0, len(b.groups))
	for id := range b.groups {
		out = append(out, id)
	}
	return out
}

func (b *Browser) groupList() []*Group {
	b.m.mu.Lock()
	defer b.m.mu.Unlock()
	out := make([]*Group, 0, len(b.groups))
	for _, g := range b.groups {
		out = append(out, g)
	}
	return out
}

// View is the Browser object of the contract.
func (b *Browser) View() map[string]any {
	b.m.mu.Lock()
	groups, tabs := len(b.groups), 0
	for _, g := range b.groups {
		tabs += len(g.tabs)
	}
	b.m.mu.Unlock()
	status := "running"
	select {
	case <-b.proc.Done():
		status = "exited"
	default:
	}
	return map[string]any{"id": b.ID, "profile": b.Profile, "pid": b.proc.Pid, "status": status,
		"started_at": b.StartedAt, "groups": groups, "tabs": tabs, "chromium_version": b.Version}
}

// The owners of a group's control.
const (
	OwnerAgent  = "agent"
	OwnerHuman  = "human"
	OwnerPaused = "paused"
)

// Control is who drives a group.
type Control struct {
	Owner  string
	Holder string
	Until  time.Time
	Reason string
}

// View is the contract's Control, as seen by client (a live-view client id), or with the holder's
// id when client is empty.
func (c Control) View(client string) map[string]any {
	var holder any
	if c.Holder != "" {
		switch {
		case client == "":
			holder = c.Holder
		case client == c.Holder:
			holder = "you"
		default:
			holder = "other"
		}
	}
	var until any
	if !c.Until.IsZero() {
		until = c.Until.UnixMilli()
	}
	return map[string]any{"owner": c.Owner, "holder": holder, "until": until, "reason": c.Reason}
}

// Group is one owner's tabs in one browser.
type Group struct {
	ID        string
	Browser   *Browser
	Profile   string
	ContextID string
	Viewport  Viewport
	Labels    map[string]string
	CreatedAt time.Time

	m *Manager

	// Guarded by the manager's lock.
	tabs   []*Tab
	active *Tab

	cmu          sync.Mutex
	control      Control
	wake         chan struct{} // closed at every change of control
	ttl          *time.Timer
	lastActivity time.Time
	closed       bool
}

// Tabs lists the group's tabs in the order they opened.
func (g *Group) Tabs() []*Tab {
	g.m.mu.Lock()
	defer g.m.mu.Unlock()
	return append([]*Tab(nil), g.tabs...)
}

// ActiveTab is the tab the group shows by default.
func (g *Group) ActiveTab() *Tab {
	g.m.mu.Lock()
	defer g.m.mu.Unlock()
	return g.active
}

// SetActive makes t the group's active tab.
func (g *Group) SetActive(t *Tab) {
	g.m.mu.Lock()
	g.active = t
	g.m.mu.Unlock()
}

// View is the contract's Group.
func (g *Group) View() map[string]any {
	g.m.mu.Lock()
	var active any
	if g.active != nil {
		active = g.active.ID
	}
	n := len(g.tabs)
	g.m.mu.Unlock()
	g.cmu.Lock()
	control, last, vp := g.control, g.lastActivity, g.Viewport
	g.cmu.Unlock()
	return map[string]any{"id": g.ID, "browser_id": g.Browser.ID, "profile": g.Profile,
		"viewport": vp, "tabs": n, "active_tab": active, "control": control.View(""), "labels": g.Labels,
		"created_at": g.CreatedAt, "last_activity_at": last}
}

// ViewportNow is the page's size in CSS pixels. It changes while the operator watches (Resize),
// so a read takes the same lock as the write and cannot tear one side from the other.
func (g *Group) ViewportNow() Viewport {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	return g.Viewport
}

func (g *Group) setViewport(vp Viewport) {
	g.cmu.Lock()
	g.Viewport = vp
	g.cmu.Unlock()
}

// Touch records activity: the browser is not idle.
func (g *Group) Touch() {
	g.cmu.Lock()
	g.lastActivity = time.Now().UTC()
	g.cmu.Unlock()
	g.Browser.touch()
}

// Control is the group's control now.
func (g *Group) Control() Control {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	return g.control
}

// SetControl changes who drives. A human holder's control ends after ttl without input.
func (g *Group) SetControl(owner, holder string, ttl time.Duration, reason string) (Control, error) {
	switch owner {
	case OwnerAgent:
		holder, ttl = "", 0
	case OwnerPaused:
		holder = ""
	case OwnerHuman:
		if holder == "" {
			return Control{}, wire.Errorf(wire.CodeInvalidParams, "human control needs the client_id of a live view")
		}
	default:
		return Control{}, wire.Errorf(wire.CodeInvalidParams, "owner must be agent, human or paused")
	}
	g.cmu.Lock()
	c := Control{Owner: owner, Holder: holder, Reason: reason}
	if g.ttl != nil {
		g.ttl.Stop()
		g.ttl = nil
	}
	if owner == OwnerHuman && ttl > 0 {
		c.Until = time.Now().Add(ttl)
		g.ttl = time.AfterFunc(ttl, func() { g.expire(c.Until) })
	}
	g.control = c
	g.lastActivity = time.Now().UTC()
	close(g.wake)
	g.wake = make(chan struct{})
	g.cmu.Unlock()
	g.Browser.touch()
	g.m.publish("control", g.controlEvent(c))
	return c, nil
}

func (g *Group) controlEvent(c Control) map[string]any {
	v := c.View("")
	v["group_id"] = g.ID
	return v
}

// expire gives control back to the agent when a human grant ran out untouched.
func (g *Group) expire(until time.Time) {
	g.cmu.Lock()
	if g.control.Owner != OwnerHuman || !g.control.Until.Equal(until) {
		g.cmu.Unlock()
		return
	}
	g.cmu.Unlock()
	_, _ = g.SetControl(OwnerAgent, "", 0, "")
}

// RenewHuman extends a human holder's grant; input from the holder calls it. It reports whether
// client holds control.
func (g *Group) RenewHuman(client string, ttl time.Duration) bool {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	if g.control.Owner != OwnerHuman || g.control.Holder != client {
		return false
	}
	g.lastActivity = time.Now().UTC()
	g.Browser.touch()
	if ttl <= 0 {
		return true
	}
	until := time.Now().Add(ttl)
	// Renewed at most once a second: every mouse move would otherwise re-arm a timer.
	if until.Sub(g.control.Until) < time.Second {
		return true
	}
	g.control.Until = until
	if g.ttl != nil {
		g.ttl.Stop()
	}
	g.ttl = time.AfterFunc(ttl, func() { g.expire(until) })
	return true
}

func (g *Group) closeControl() {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	if g.ttl != nil {
		g.ttl.Stop()
	}
	if !g.closed {
		g.closed = true
		close(g.wake)
		g.wake = make(chan struct{})
	}
}

// Closed reports whether the group is gone.
func (g *Group) Closed() bool {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	return g.closed
}

// The actors a call may come from.
const (
	ActorAgent    = "agent"
	ActorOperator = "operator"
)

// Origin says who makes a call.
type Origin struct {
	Actor    string `json:"actor"`
	LaunchID string `json:"launch_id,omitempty"`
	WaitMs   *int64 `json:"wait_ms,omitempty"`
}

// IsAgent reports whether the call is the agent's: without an origin, it is.
func (o *Origin) IsAgent() bool { return o == nil || o.Actor != ActorOperator }

// Gate lets an agent's call through while the agent drives, waits for a person to give control
// back, and refuses while paused. The operator's calls always pass.
func (g *Group) Gate(ctx context.Context, o *Origin, defaultWait time.Duration) error {
	if !o.IsAgent() {
		g.Touch()
		return nil
	}
	if o != nil && o.Actor != "" && o.Actor != ActorAgent {
		return wire.Errorf(wire.CodeInvalidParams, "origin.actor must be agent or operator")
	}
	wait := defaultWait
	if o != nil && o.WaitMs != nil {
		wait = time.Duration(*o.WaitMs) * time.Millisecond
	}
	deadline := time.NewTimer(wait)
	defer deadline.Stop()
	for {
		g.cmu.Lock()
		c, wake, closed := g.control, g.wake, g.closed
		g.cmu.Unlock()
		if closed {
			return errBrowserGone("closed")
		}
		switch c.Owner {
		case OwnerAgent:
			g.Touch()
			return nil
		case OwnerPaused:
			return ErrPaused(c.Reason)
		}
		select {
		case <-wake:
		case <-deadline.C:
			return ErrHumanDriving(c)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Dialog is a page dialog waiting for an answer.
type Dialog struct {
	Type          string `json:"type"`
	Message       string `json:"message"`
	DefaultPrompt string `json:"default_prompt,omitempty"`
}

// Tab is one page.
type Tab struct {
	ID        string
	Group     *Group
	TargetID  string
	Session   string
	Opener    string
	CreatedAt time.Time

	mu      sync.Mutex
	url     string
	title   string
	loading bool
	dialog  *Dialog
	loader  string          // the main frame's current document
	life    map[string]bool // lifecycle events of that document
	wake    chan struct{}
	closed  bool
	values  map[any]any
	frames  map[string]string // frame id -> session, for the frames of other sites in their own process

	auto     []AutoDialog // the dialogs the daemon answered by itself, the last few (dialogs.go)
	autoSeq  int64
	autoOpen bool // one of them is being answered, and its closing is not an agent's dialog closing
}

// Call sends a command to the tab's page.
func (t *Tab) Call(ctx context.Context, method string, params, result any) error {
	return t.Group.Browser.conn.Call(ctx, t.Session, method, params, result)
}

// CallIn sends a command to one of the tab's sessions: its page's (session "") or a frame's.
func (t *Tab) CallIn(ctx context.Context, session, method string, params, result any) error {
	if session == "" {
		session = t.Session
	}
	return t.Group.Browser.conn.Call(ctx, session, method, params, result)
}

// FrameSession is the session of the frame, when it runs in a process of its own; "" when it runs in
// the page's.
func (t *Tab) FrameSession(frameID string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.frames[frameID]
}

func (t *Tab) setFrame(frameID, session string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.frames == nil {
		t.frames = map[string]string{}
	}
	t.frames[frameID] = session
}

func (t *Tab) dropFrame(session string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	for f, s := range t.frames {
		if s == session {
			delete(t.frames, f)
		}
	}
}

// FramesApart counts the frames of the tab that run in a process of their own and are followed
// through their own session: what shows a page's frames of other sites are being read as such.
func (t *Tab) FramesApart() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.frames)
}

func (t *Tab) frameSessions() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make([]string, 0, len(t.frames))
	for _, s := range t.frames {
		out = append(out, s)
	}
	return out
}

// Input sends one input event and waits for Chromium to have handled it, or for a dialog the event
// opened: a click whose handler calls confirm() is not answered until the dialog is, and the
// dialog is the input's effect, not a failure.
func (t *Tab) Input(ctx context.Context, method string, params any) error {
	reply := t.Group.Browser.conn.Send(ctx, t.Session, method, params)
	for {
		wake := t.Wake()
		select {
		case err := <-reply:
			return err
		case <-wake:
			if t.Dialog() != nil {
				return nil
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Value and SetValue keep per-tab state for other packages (the page model's refs).
func (t *Tab) Value(key any) any {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.values[key]
}

func (t *Tab) SetValue(key, v any) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.values == nil {
		t.values = map[any]any{}
	}
	t.values[key] = v
}

// URL and Title are the page's, as Chromium last reported them.
func (t *Tab) URL() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.url
}

func (t *Tab) Title() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.title
}

// Loader is the page's current document, as its lifecycle events name it.
func (t *Tab) Loader() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.loader
}

// Dialog is the dialog open on the page, or nil.
func (t *Tab) Dialog() *Dialog {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.dialog
}

// Closed reports whether the tab is gone.
func (t *Tab) Closed() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.closed
}

func (t *Tab) markClosed() {
	t.mu.Lock()
	if !t.closed {
		t.closed = true
		close(t.wake)
		t.wake = make(chan struct{})
	}
	t.mu.Unlock()
}

// View is the contract's Tab.
func (t *Tab) View() map[string]any {
	t.mu.Lock()
	u, title, loading := t.url, t.title, t.loading
	t.mu.Unlock()
	active := t.Group.ActiveTab() == t
	var opener any
	if t.Opener != "" {
		opener = t.Opener
	}
	return map[string]any{"id": t.ID, "group_id": t.Group.ID, "url": u, "title": title, "favicon_url": faviconURL(u),
		"loading": loading, "active": active, "opener": opener, "created_at": t.CreatedAt}
}

// faviconURL is where a page's icon conventionally is. Reading the page's own <link rel=icon> would
// mean running something in it for a picture the app can do without.
func faviconURL(u string) string {
	p, err := url.Parse(u)
	if err != nil || (p.Scheme != "http" && p.Scheme != "https") {
		return ""
	}
	return p.Scheme + "://" + p.Host + "/favicon.ico"
}

func (t *Tab) broadcastLocked() {
	close(t.wake)
	t.wake = make(chan struct{})
}

func (t *Tab) infoChanged(u, title string) {
	t.mu.Lock()
	changed := u != t.url || title != t.title
	t.url, t.title = u, title
	t.mu.Unlock()
	if changed {
		t.publishUpdate()
	}
}

func (t *Tab) publishUpdate() {
	t.mu.Lock()
	data := map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "url": t.url, "title": t.title,
		"favicon_url": faviconURL(t.url), "loading": t.loading}
	t.mu.Unlock()
	t.Group.m.publishKeyed("tab.updated", t.ID, data)
}

// event keeps the tab's own state: its document, its lifecycle, its dialog, whether it loads.
func (t *Tab) event(e cdp.Event) {
	switch e.Method {
	case "Page.lifecycleEvent":
		var p struct {
			FrameID  string `json:"frameId"`
			LoaderID string `json:"loaderId"`
			Name     string `json:"name"`
		}
		if json.Unmarshal(e.Params, &p) != nil || p.FrameID != t.TargetID {
			return
		}
		t.mu.Lock()
		if p.Name == "init" || p.LoaderID != t.loader {
			t.loader = p.LoaderID
			t.life = map[string]bool{}
		}
		t.life[p.Name] = true
		t.broadcastLocked()
		t.mu.Unlock()
	case "Page.frameStartedLoading", "Page.frameStoppedLoading":
		var p struct {
			FrameID string `json:"frameId"`
		}
		if json.Unmarshal(e.Params, &p) != nil || p.FrameID != t.TargetID {
			return
		}
		t.mu.Lock()
		t.loading = e.Method == "Page.frameStartedLoading"
		t.broadcastLocked()
		t.mu.Unlock()
		t.publishUpdate()
	case "Page.javascriptDialogOpening":
		var p struct {
			URL           string `json:"url"`
			Message       string `json:"message"`
			Type          string `json:"type"`
			DefaultPrompt string `json:"defaultPrompt"`
		}
		if json.Unmarshal(e.Params, &p) != nil {
			return
		}
		d := &Dialog{Type: p.Type, Message: p.Message, DefaultPrompt: p.DefaultPrompt}
		if t.autoDialog(d, p.URL) {
			// Never the tab's open dialog: nothing waits on it or is refused for it.
			return
		}
		t.mu.Lock()
		t.dialog = d
		t.broadcastLocked()
		t.mu.Unlock()
		t.Group.m.publish("dialog.opened", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "type": d.Type,
			"message": d.Message, "default_prompt": d.DefaultPrompt})
	case "Page.javascriptDialogClosed":
		t.mu.Lock()
		had := t.dialog != nil
		t.autoOpen = false
		t.dialog = nil
		t.broadcastLocked()
		t.mu.Unlock()
		if had {
			t.Group.m.publish("dialog.closed", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID})
		}
	}
}

// WaitLifecycle waits until the page's current document has seen the lifecycle event name ("load",
// "networkIdle", …), or the context ends. loader, when set, is the document it must be.
func (t *Tab) WaitLifecycle(ctx context.Context, loader, name string) error {
	for {
		t.mu.Lock()
		ok := t.life[name] && (loader == "" || t.loader == loader)
		wake, closed, dialog := t.wake, t.closed, t.dialog
		t.mu.Unlock()
		if ok {
			return nil
		}
		if closed {
			return errNoSuchTab(t.ID)
		}
		if dialog != nil {
			// The page is stopped under a dialog: nothing will load until it is answered.
			return ErrDialogOpen(dialog)
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Wake is closed at the tab's next change of state (lifecycle, loading, dialog, closing).
func (t *Tab) Wake() <-chan struct{} {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.wake
}

// CheckURL refuses what a browser tool must never open: anything but http, https and about:blank.
// file:, data:, blob:, javascript:, chrome: and their kind are how a page or a prompt reaches past
// the network wall, as Browser Use's data: and blob: bypass showed.
func CheckURL(raw, actor string) error {
	if raw == "about:blank" {
		return nil
	}
	p, err := url.Parse(raw)
	if err != nil {
		return wire.Errorf(wire.CodeInvalidParams, "not a URL: %v", err)
	}
	switch strings.ToLower(p.Scheme) {
	case "http", "https":
		if p.Host == "" {
			return wire.Errorf(wire.CodeInvalidParams, "the URL has no host")
		}
		return nil
	case "":
		return wire.Errorf(wire.CodeInvalidParams, "the URL has no scheme; write https://%s", raw)
	}
	return wire.Errorf(wire.CodeForbidden, "%s: URLs are not opened in this browser; only http and https", p.Scheme)
}
