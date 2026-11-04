package browser

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// attach is the backend of the operator's own Chrome, driven through the Daedalus extension over a
// relay. A tab is a debuggee of the extension, its session "x<tabId>"; everything above a tab —
// making one, closing it, its window, the downloads — is a call the extension makes with the
// ordinary extension APIs. The daemon never sees the browser itself, and must not behave as though
// it owned it: none of what launch does to a browser of its own (adopting or closing pages it did
// not open, pausing every new page, moving the downloads, writing the profile, closing the browser
// when idle, rewriting the user agent, the screen position script) has a path here.
type attach struct {
	m   *Manager
	hub *relay.Hub

	mu sync.Mutex
	// cur is the browser of the operator's Chrome, which a relay the same Chrome makes again is
	// bound to (onSession).
	cur *attachProc
	// grace is RelayGrace, shorter in a test.
	grace time.Duration
	// dispatched is when the daemon last sent input to each tab, by Chrome's tab id: the operator's
	// own input is told from the echo of the daemon's by it.
	dispatched map[int]time.Time
	// setting are the tabs being prepared, by tab id, so a tab the extension announces while
	// tab.create is still answering is prepared once.
	setting map[int]chan *Tab
}

// ChromeHolder is the holder of human control the operator's own input in Chrome takes: a person at
// the browser, not a live view.
const ChromeHolder = "chrome"

// ChromeInputTTL is how long control stays with the operator after their last input in an agent tab.
// The agent waits that long for the tab to be still before it acts again.
const ChromeInputTTL = 15 * time.Second

// inputEcho is how long after the daemon's own input to a tab the extension's report of trusted
// input there is taken for that input: the extension's content script cannot tell a dispatched
// event from a person's, both are trusted.
const inputEcho = 750 * time.Millisecond

func (a *attach) Name() string { return BackendAttach }

// ErrAttachRefused is what an operation gets that would change the operator's own browser beyond
// the agent's tabs: their cookies, their profile, a browser context of the daemon's.
func ErrAttachRefused(what string) *wire.Error {
	return errWith(wire.CodeForbidden, map[string]any{"reason": "user_browser"},
		"%s is not done in the operator's own Chrome; use the agent's browser for it", what)
}

// errNotConnected is browser.open while no Chrome is connected.
func errNotConnected() *wire.Error {
	return errWith(wire.CodeUnsupported, map[string]any{"reason": "chrome_not_connected"},
		"the operator's Chrome is not connected: the Daedalus extension must be installed, and connected in its popup")
}

// relayErr turns a call's failure into the daemon's errors: a refusal of the extension's is 1004,
// a relay gone is the browser gone.
func relayErr(err error) error {
	var re *relay.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, &re) && re.Code == relay.ErrRefused:
		return errWith(wire.CodeForbidden, map[string]any{"reason": "refused_by_extension"}, "the extension refused: %s", re.Message)
	case errors.Is(err, relay.ErrNotConnected):
		// The relay ended under the call; the browser waits RelayGrace for it, and sessionOf says
		// gone once it stops waiting.
		return errReconnecting()
	}
	return err
}

// errReconnecting is a call made while the browser waits for its Chrome to connect again.
func errReconnecting() *wire.Error {
	return errWith(wire.CodeUnsupported, map[string]any{"reason": "chrome_reconnecting"}, "%s", relay.ErrReconnecting)
}

// RelayGrace is how long the browser of the operator's Chrome outlives its relay, waiting for the same
// Chrome profile to connect again. The extension reconnects within seconds of a drop (a worker
// restart, the host restarting, a frame the host's server refused); ending the browser at once closed
// the agent's group, gave the same tabs new ids and lost the conversation's grants, three times in
// one session.
const RelayGrace = 45 * time.Second

// RelayWait is the longest a call waits for a relay that is expected back (Manager.AwaitRelay). It is
// less than the host's slack on a call to this daemon, so the host hears the answer, not its timeout.
const RelayWait = 20 * time.Second

// rebindSettle is how long after a rebind the tabs the extension replays are taken as the ones the
// browser already has: its debugger stayed attached through the drop.
const rebindSettle = 15 * time.Second

// attachProc is the relayed Chrome as the manager waits for it: it is done when the relay ends and
// the same Chrome does not connect again within RelayGrace, or the daemon lets go of it. Closing it
// closes the agent's groups, never the browser.
type attachProc struct {
	a       *attach
	b       *Browser
	line    *relay.Line
	profile string
	done    chan struct{}
	once    sync.Once
	closing sync.Mutex

	mu        sync.Mutex
	s         *relay.Session // the relay bound now, or the one that ended while none is
	rebound   chan struct{}  // closed when another relay is bound
	reboundAt time.Time
}

func (p *attachProc) Done() <-chan struct{} { return p.done }
func (p *attachProc) ExitCode() int         { return 0 }

func (p *attachProc) session() *relay.Session {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.s
}

func (p *attachProc) finish() {
	p.once.Do(func() {
		p.line.Close()
		close(p.done)
	})
}

// end gives the browser up: reason is what its groups answer from now on.
func (p *attachProc) end(reason string) {
	p.b.setClosing(reason)
	p.finish()
}

// watch follows the relays the browser runs over: one that ends is waited for, and the browser
// ends only when no relay of the same Chrome comes back within RelayGrace.
func (p *attachProc) watch() {
	for {
		s := p.session()
		select {
		case <-p.done:
			return
		case <-s.Done():
		}
		p.mu.Lock()
		rebound := p.rebound
		same := p.s == s
		p.mu.Unlock()
		if !same {
			continue // bound to the next relay already
		}
		reason := s.Reason()
		if reason == relay.LostShutdown || p.profile == "" {
			p.end("chrome disconnected (" + reason + ")")
			return
		}
		grace := p.a.grace
		p.a.m.log.Info("relay lost; the browser waits for Chrome to connect again", "browser", p.b.ID, "reason", reason, "grace", grace)
		timer := time.NewTimer(grace)
		select {
		case <-rebound:
			timer.Stop()
		case <-timer.C:
			p.end(fmt.Sprintf("chrome disconnected (%s, and it did not connect again within %s)", reason, grace))
			return
		case <-p.done:
			timer.Stop()
			return
		}
	}
}

// onSession binds the browser to a relay the hub accepted, before its welcome goes out and so before
// the extension replays the tabs it holds. Another Chrome than the browser's ends it.
func (a *attach) onSession(s *relay.Session) {
	a.mu.Lock()
	p := a.cur
	a.mu.Unlock()
	if p == nil {
		return
	}
	select {
	case <-p.done:
		return
	default:
	}
	if p.session() == s {
		return
	}
	if p.profile == "" || s.Hello().ProfileID != p.profile {
		p.end("chrome disconnected (another Chrome connected)")
		return
	}
	p.line.Bind(s)
	s.SetObserver(&attachObserver{a: a, b: p.b})
	p.mu.Lock()
	p.s = s
	p.reboundAt = time.Now()
	close(p.rebound)
	p.rebound = make(chan struct{})
	p.mu.Unlock()
	a.m.log.Info("browser bound to the relay Chrome made again", "browser", p.b.ID)
}

// justRebound reports whether b was bound to a new relay moments ago.
func (a *attach) justRebound(b *Browser) bool {
	p, ok := b.proc.(*attachProc)
	if !ok {
		return false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return !p.reboundAt.IsZero() && time.Since(p.reboundAt) < rebindSettle
}

// Close detaches: the agent's groups are closed through the extension (their tabs and the agent's
// window), unless the daemon is stopping, when the extension is told by the relay's bye and keeps
// what the operator may still be reading.
func (p *attachProc) Close(grace time.Duration) {
	p.closing.Lock()
	defer p.closing.Unlock()
	reason, _ := p.b.closeReason()
	if reason != "shutdown" {
		ctx, cancel := context.WithTimeout(context.Background(), grace)
		s := p.session()
		for _, id := range p.b.GroupIDs() {
			_ = s.Call(ctx, relay.OpGroupClose, relay.GroupArgs{Group: id}, nil)
		}
		cancel()
	}
	p.finish()
}

func (p *attachProc) Kill() { p.finish() }

func (a *attach) session() (*relay.Session, error) {
	if a.hub == nil {
		return nil, errNotConnected()
	}
	s := a.hub.Current()
	if s == nil {
		return nil, errNotConnected()
	}
	return s, nil
}

func (a *attach) start(ctx context.Context, profile string) (*Browser, error) {
	s, err := a.session()
	if err != nil {
		return nil, err
	}
	m := a.m
	b := m.newBrowser(profile)
	b.attached = true
	hi := s.Hello()
	b.Version = "Chrome/" + hi.ChromeVersion
	// No screen position script for pages that are the operator's: the probe is answered "no" before
	// any page asks.
	b.screenAsked = true
	close(b.screenKnown)
	// The operator's yes to a site is kept by group, where net.grant puts it (Manager.WallKey).
	conn, line := relay.NewLine(func(e cdp.Event) { m.onEvent(b, e) }, a.markDispatch)
	b.conn = conn
	p := &attachProc{a: a, b: b, line: line, profile: hi.ProfileID, s: s, done: make(chan struct{}), rebound: make(chan struct{})}
	b.proc = p
	line.Bind(s)
	s.SetObserver(&attachObserver{a: a, b: b})
	a.mu.Lock()
	a.cur = p
	a.mu.Unlock()
	go p.watch()
	m.log.Info("browser attached", "browser", b.ID, "profile", hi.ProfileName, "chrome", hi.ChromeVersion, "extension", hi.ExtVersion)
	return b, nil
}

func (a *attach) markDispatch(tab int) {
	a.mu.Lock()
	a.dispatched[tab] = time.Now()
	a.mu.Unlock()
}

// openGroup asks for the agent's own window for the group, and takes up the tabs the extension's tab
// group of that name still holds: a group whose relay dropped (the extension's worker restarted)
// opens again on the tabs it had.
func (a *attach) openGroup(ctx context.Context, g *Group) error {
	if g.Profile == EphemeralProfile {
		return ErrAttachRefused("a throwaway context")
	}
	s, err := a.sessionOf(g.Browser)
	if err != nil {
		return err
	}
	var r relay.WindowEnsured
	if err := s.Call(ctx, relay.OpWindowEnsure, relay.GroupArgs{Group: g.ID, Session: g.Labels["session_id"], Title: g.Labels["title"]}, &r); err != nil {
		return relayErr(err)
	}
	for _, tab := range r.Tabs {
		if _, err := a.prepare(ctx, g, tab, "", true); err != nil {
			a.m.log.Warn("taking up a tab of the group", "group", g.ID, "tab", tab.ID, "error", err.Error())
		}
	}
	return nil
}

// sessionOf is the relay of b, which is the hub's current one for as long as b lives.
func (a *attach) sessionOf(b *Browser) (*relay.Session, error) {
	p, ok := b.proc.(*attachProc)
	if !ok {
		return nil, errNotConnected()
	}
	select {
	case <-p.done:
		reason, _ := b.closeReason()
		if reason == "" {
			reason = "chrome disconnected"
		}
		return nil, errBrowserGone(reason)
	default:
	}
	s := p.session()
	select {
	case <-s.Done():
		return nil, errReconnecting()
	default:
	}
	return s, nil
}

func (a *attach) closeGroup(ctx context.Context, g *Group, tabs []*Tab) {
	s, err := a.sessionOf(g.Browser)
	if err != nil {
		return
	}
	if err := s.Call(ctx, relay.OpGroupClose, relay.GroupArgs{Group: g.ID}, nil); err != nil {
		a.m.log.Warn("closing a group in Chrome", "group", g.ID, "error", err.Error())
	}
}

func (a *attach) newTab(ctx context.Context, g *Group) (*Tab, error) {
	s, err := a.sessionOf(g.Browser)
	if err != nil {
		return nil, err
	}
	var r relay.TabCreated
	args := relay.TabCreateArgs{Group: g.ID, URL: "about:blank", Session: g.Labels["session_id"], Title: g.Labels["title"]}
	if err := s.Call(ctx, relay.OpTabCreate, args, &r); err != nil {
		return nil, relayErr(err)
	}
	t, err := a.prepare(ctx, g, r.Tab, "", true)
	if err != nil {
		return nil, err
	}
	if t == nil {
		return nil, fmt.Errorf("the new tab could not be prepared")
	}
	return t, nil
}

// prepare makes a tab of the extension's one of the group's: its main frame is read from the tab
// itself (the target id launch gets from Chromium is not the extension's to give), then it is set up
// as any page. claimed says the daemon asked for it (tab.create, or the group's own tab taken up),
// so the group's tab cap does not close it as it would a popup. A tab already known, or being
// prepared, is returned as it is.
func (a *attach) prepare(ctx context.Context, g *Group, tab relay.Tab, opener string, claimed bool) (*Tab, error) {
	m := a.m
	b := g.Browser
	session := relay.SessionOf(tab.ID)
	m.mu.Lock()
	if t := m.bySession[session]; t != nil {
		m.mu.Unlock()
		return t, nil
	}
	m.mu.Unlock()
	a.mu.Lock()
	if a.setting == nil {
		a.setting = map[int]chan *Tab{}
	}
	if wait := a.setting[tab.ID]; wait != nil {
		a.mu.Unlock()
		select {
		case t := <-wait:
			wait <- t
			return t, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	mine := make(chan *Tab, 1)
	a.setting[tab.ID] = mine
	a.mu.Unlock()
	var t *Tab
	defer func() {
		mine <- t
		a.mu.Lock()
		delete(a.setting, tab.ID)
		a.mu.Unlock()
	}()
	var tree struct {
		FrameTree struct {
			Frame struct {
				ID  string `json:"id"`
				URL string `json:"url"`
			} `json:"frame"`
		} `json:"frameTree"`
	}
	fctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := b.conn.Call(fctx, session, "Page.getFrameTree", nil, &tree); err != nil {
		return nil, fmt.Errorf("the tab's frame: %w", err)
	}
	info := targetInfo{TargetID: tree.FrameTree.Frame.ID, Type: "page", URL: tab.URL, Title: tab.Title}
	if info.URL == "" {
		info.URL = tree.FrameTree.Frame.URL
	}
	info.given = tab.Given
	if claimed {
		m.mu.Lock()
		b.claims[info.TargetID] = make(chan *Tab, 1)
		b.claimGroups[info.TargetID] = g
		m.mu.Unlock()
	}
	t = m.setupTab(g, session, info, opener)
	if claimed {
		m.mu.Lock()
		delete(b.claims, info.TargetID)
		delete(b.claimGroups, info.TargetID)
		m.mu.Unlock()
	}
	return t, nil
}

// forgetTab drops a tab the extension closed or the operator took back, and says so.
func (a *attach) forgetTab(t *Tab, why string) {
	m := a.m
	m.mu.Lock()
	if m.tabs[t.ID] != t {
		m.mu.Unlock()
		return
	}
	m.forgetTabLocked(t)
	m.mu.Unlock()
	m.tabGone(t)
	ev := map[string]any{"group_id": t.Group.ID, "tab_id": t.ID}
	if why != "" {
		ev["reason"] = why
	}
	m.publish("tab.closed", ev)
}

// tabID is Chrome's id of a tab the daemon knows.
func tabID(t *Tab) int {
	id, _, _ := relay.ParseSession(t.Session)
	return id
}

func (a *attach) closeTab(ctx context.Context, t *Tab) error {
	s, err := a.sessionOf(t.Group.Browser)
	if err != nil {
		return err
	}
	var r relay.TabClosed
	if err := s.Call(ctx, relay.OpTabClose, relay.TabArgs{Tab: tabID(t)}, &r); err != nil {
		return relayErr(err)
	}
	// No target event follows a tab the extension closed: the tab is forgotten here, and the
	// extension's own report of it, if any, finds nothing. A tab the operator gave is let go rather
	// than closed, and the event says so, so nobody reads it as gone from their window.
	why := ""
	if r.Released {
		why = "released"
	}
	a.forgetTab(t, why)
	return nil
}

func (a *attach) closeRefused(ctx context.Context, b *Browser, target, session string) {
	s, err := a.sessionOf(b)
	if err != nil {
		return
	}
	id, _, ok := relay.ParseSession(session)
	if !ok {
		return
	}
	_ = s.Call(ctx, relay.OpTabClose, relay.TabArgs{Tab: id}, nil)
}

func (a *attach) activate(ctx context.Context, t *Tab, quiet bool) error {
	s, err := a.sessionOf(t.Group.Browser)
	if err != nil {
		return err
	}
	return relayErr(s.Call(ctx, relay.OpTabActivate, relay.TabArgs{Tab: tabID(t), Quiet: quiet}, nil))
}

// refreshTab reads the address and title from the tab's own history: Target.getTargetInfo is a
// browser-level command, and the current history entry holds both, its title kept up to date with
// what a script sets.
func (a *attach) refreshTab(ctx context.Context, t *Tab) {
	var h struct {
		CurrentIndex int `json:"currentIndex"`
		Entries      []struct {
			URL   string `json:"url"`
			Title string `json:"title"`
		} `json:"entries"`
	}
	ictx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := t.Call(ictx, "Page.getNavigationHistory", nil, &h); err != nil {
		return
	}
	if h.CurrentIndex >= 0 && h.CurrentIndex < len(h.Entries) {
		e := h.Entries[h.CurrentIndex]
		t.infoChanged(e.URL, e.Title)
	}
}

// refresh also follows each group's page size: the operator resizes their window, takes it full
// screen or out of it, and the agent's coordinates must follow the page they now have.
func (a *attach) refresh(ctx context.Context, b *Browser) {
	var wg sync.WaitGroup
	for _, g := range b.groupList() {
		followed := false
		for _, t := range g.Tabs() {
			follow := !followed && !t.Closed()
			followed = followed || follow
			wg.Add(1)
			go func() {
				defer wg.Done()
				a.refreshTab(ctx, t)
				if follow {
					a.m.followPage(ctx, g, t.Session, false)
				}
			}()
		}
	}
	wg.Wait()
}

// Neither reads nor sets a window: the daemon never sizes one in the operator's Chrome (see
// errWindowFixed), and takes the page's own size for the group instead (Manager.followPage).
func (a *attach) windowSize(ctx context.Context, g *Group, target string) (int, int, error) {
	return 0, 0, errWindowFixed
}

func (a *attach) setWindowSize(ctx context.Context, g *Group, target string, w, h int) error {
	return errWindowFixed
}

// cancelDownload has nothing to stop: a download in the operator's Chrome is theirs, in their
// downloads list, and the daemon only keeps a copy of a finished one.
func (a *attach) cancelDownload(ctx context.Context, b *Browser, guid string) error {
	return nil
}

func (a *attach) downloadPath(ctx context.Context, b *Browser, guid string) (string, bool, error) {
	s, err := a.sessionOf(b)
	if err != nil {
		return "", false, err
	}
	var r relay.Download
	if err := s.Call(ctx, relay.OpDownloadsSearch, relay.DownloadsSearchArgs{ID: guid}, &r); err != nil {
		return "", false, relayErr(err)
	}
	if r.Path == "" {
		return "", false, fmt.Errorf("Chrome does not say where the download went")
	}
	return r.Path, false, nil
}

// CookieNames lists the cookies the operator's Chrome would send to url: every field but the value,
// which the extension never hands over.
func (m *Manager) CookieNames(ctx context.Context, g *Group, url string) ([]relay.CookieName, error) {
	a, ok := m.backend.(*attach)
	if !ok {
		return nil, wire.Errorf(wire.CodeUnsupported, "cookie names come from the operator's Chrome only")
	}
	s, err := a.sessionOf(g.Browser)
	if err != nil {
		return nil, err
	}
	var r relay.CookieNames
	if err := s.Call(ctx, relay.OpCookiesNames, relay.CookiesNamesArgs{URL: url}, &r); err != nil {
		return nil, relayErr(err)
	}
	return r.Cookies, nil
}

// Notify shows text in the extension's popup, and is how the app's "Check connection" reaches the
// browser; it answers how long the round trip took.
func (m *Manager) Notify(ctx context.Context, text string) (time.Duration, error) {
	hub := m.deps.Relay
	if hub == nil || hub.Current() == nil {
		return 0, errNotConnected()
	}
	begin := time.Now()
	if err := hub.Current().Call(ctx, relay.OpNotify, relay.NotifyArgs{Text: text}, nil); err != nil {
		return 0, relayErr(err)
	}
	return time.Since(begin), nil
}

// CallExtension runs one of the side panel's operations (relay.PanelOps) on the connected Chrome for
// the host: its credential, and a conversation's news for a notification. Anything else is refused,
// so the host's socket cannot reach the tabs around the daemon's own rules.
func (m *Manager) CallExtension(ctx context.Context, op string, args map[string]any) error {
	if !relay.PanelOps[op] {
		return wire.Errorf(wire.CodeInvalidParams, "relay.call carries the side panel's operations only, not %q", op)
	}
	hub := m.deps.Relay
	if hub == nil || hub.Current() == nil {
		return errNotConnected()
	}
	if err := hub.Current().Call(ctx, op, args, nil); err != nil {
		return relayErr(err)
	}
	return nil
}

// attachObserver hears what the operator changed in the agent's scope in Chrome.
type attachObserver struct {
	a *attach
	b *Browser
}

func (o *attachObserver) tab(id int) *Tab {
	m := o.a.m
	m.mu.Lock()
	defer m.mu.Unlock()
	t := m.bySession[relay.SessionOf(id)]
	if t == nil || t.Group.Browser != o.b {
		return nil
	}
	return t
}

func (o *attachObserver) group(id string) *Group {
	m := o.a.m
	m.mu.Lock()
	defer m.mu.Unlock()
	g := m.groups[id]
	if g == nil || g.Browser != o.b {
		return nil
	}
	return g
}

func (o *attachObserver) RelayTabs(tb relay.Tabs) {
	m := o.a.m
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	switch tb.Op {
	case relay.TabsCreated, relay.TabsJoined:
		if tb.Tab == nil {
			return
		}
		// A tab becomes the agent's only by its opener being one, or by the operator putting it in
		// an agent's group. Anything else the extension names is not the daemon's, and is never
		// taken, paused or closed.
		var g *Group
		opener := ""
		if tb.Tab.Opener != 0 {
			if ot := o.tab(tb.Tab.Opener); ot != nil {
				g, opener = ot.Group, ot.ID
			}
		}
		if g == nil && tb.Tab.Group != "" {
			g = o.group(tb.Tab.Group)
		}
		if g == nil {
			m.log.Debug("a tab outside the agent's groups", "tab", tb.Tab.ID)
			return
		}
		// A tab the daemon already has, announced again, was detached and attached afresh (the
		// extension reattaches after Chrome or the operator detached its debugger, and replays the
		// tabs it holds after a welcome). Its domains, its guard and its frames' sessions went with
		// the old attachment, so it is set up again as a new tab rather than trusted as it was.
		if old := o.tab(tb.Tab.ID); old != nil {
			if old.Group != g {
				return
			}
			if tb.Op == relay.TabsJoined && o.a.justRebound(o.b) {
				// Replayed after the relay came back: the debugger stayed attached through the drop,
				// and the tab keeps its id and setup. Only what it shows may have changed meanwhile.
				o.a.refreshTab(ctx, old)
				return
			}
			o.a.forgetTab(old, "reattached")
		}
		// A tab the operator put in the group is theirs to give: it is not closed for the cap the
		// way a popup is.
		if _, err := o.a.prepare(ctx, g, *tb.Tab, opener, tb.Op == relay.TabsJoined); err != nil {
			m.log.Warn("a tab of the group", "group", g.ID, "tab", tb.Tab.ID, "error", err.Error())
		}
	case relay.TabsRemoved, relay.TabsLeft:
		if tb.Tab == nil {
			return
		}
		if t := o.tab(tb.Tab.ID); t != nil {
			why := "closed"
			if tb.Op == relay.TabsLeft {
				why = "taken_back"
			}
			o.a.forgetTab(t, why)
		}
	case relay.TabsUpdated:
		if tb.Tab == nil {
			return
		}
		if t := o.tab(tb.Tab.ID); t != nil {
			t.infoChanged(tb.Tab.URL, tb.Tab.Title)
		}
	case relay.TabsGroupClosed:
		g := o.group(tb.Group)
		if g == nil {
			return
		}
		tabs := g.Tabs()
		m.forgetGroup(g, tabs)
		m.closeGrants(g)
		m.publish("group.closed", map[string]any{"group_id": g.ID, "browser_id": g.Browser.ID, "profile": g.Profile, "labels": g.Labels,
			"reason": "closed_in_chrome"})
	}
}

// RelayInput is the operator's own input in an agent tab: control is theirs while they keep at it,
// and the agent's calls wait, as for a person in the live view.
func (o *attachObserver) RelayInput(id int) {
	t := o.tab(id)
	if t == nil {
		return
	}
	o.a.mu.Lock()
	echo := time.Since(o.a.dispatched[id]) < inputEcho
	o.a.mu.Unlock()
	if echo {
		return
	}
	g := t.Group
	if g.RenewHuman(ChromeHolder, ChromeInputTTL) {
		return
	}
	if g.Control().Owner != OwnerAgent {
		// Paused, or a person in the live view: theirs already.
		return
	}
	_, _ = g.SetControl(OwnerHuman, ChromeHolder, ChromeInputTTL, "the operator is using this tab in Chrome")
}
