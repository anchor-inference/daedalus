package browser

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// NetworkBufferBytes and NetworkResourceBytes bound what Chromium keeps of a tab's response bodies:
// in all, and of one response. A JSON answer of an API is far below the second; a response past it
// is never kept, and the oldest go first when the first is reached.
const (
	NetworkBufferBytes   = 8 << 20
	NetworkResourceBytes = 1 << 20
)

// strayWait is how long a page that belongs to no known group (a popup opened without an opener)
// waits for a claim before it is given to its browser's most recently used group.
const strayWait = time.Second

// attachPage takes a page Chromium attached to: it finds the page's group, prepares it, and lets it
// run. A page's group is the tab that opened it, else its throwaway context, else the only group of
// its browser; a page none of these name waits for NewTab to claim it.
func (m *Manager) attachPage(b *Browser, session string, info targetInfo) {
	m.mu.Lock()
	var g *Group
	opener := ""
	if cg := b.claimGroups[info.TargetID]; cg != nil {
		g = cg
	}
	if g == nil && info.OpenerID != "" {
		if t := m.byTarget[info.OpenerID]; t != nil {
			g, opener = t.Group, t.ID
		}
	}
	if g == nil && info.BrowserContextID != "" {
		g = b.contexts[info.BrowserContextID]
	}
	if g == nil && !b.Ephemeral && len(b.groups) == 1 {
		for _, only := range b.groups {
			g = only
		}
	}
	if g == nil {
		if b.spare == nil && len(b.groups) == 0 && len(b.claims) == 0 {
			// The blank page every browser starts with: kept, and given to the first group.
			b.spare = &pageAttach{session: session, info: info}
			m.mu.Unlock()
			return
		}
		b.early[info.TargetID] = pageAttach{session: session, info: info}
		m.mu.Unlock()
		time.AfterFunc(strayWait, func() { m.adoptStray(b, info.TargetID) })
		return
	}
	m.mu.Unlock()
	m.setupTab(g, session, info, opener)
}

// adoptStray gives an unclaimed page to the most recently used group of its browser, or closes it
// when the browser has none.
func (m *Manager) adoptStray(b *Browser, target string) {
	m.mu.Lock()
	pa, ok := b.early[target]
	if !ok {
		m.mu.Unlock()
		return
	}
	delete(b.early, target)
	var best *Group
	for _, g := range b.groups {
		if g.ContextID == "" && (best == nil || g.lastActive().After(best.lastActive())) {
			best = g
		}
	}
	m.mu.Unlock()
	if best == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = b.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": target}, nil)
		return
	}
	m.setupTab(best, pa.session, pa.info, "")
}

func (g *Group) lastActive() time.Time {
	g.cmu.Lock()
	defer g.cmu.Unlock()
	return g.lastActivity
}

// setupTab prepares a page for the group and lets it run: the group's viewport, the user agent, the
// events the daemon follows. It publishes the tab, and hands it to a NewTab waiting for it.
func (m *Manager) setupTab(g *Group, session string, info targetInfo, opener string) *Tab {
	b := g.Browser
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	m.mu.Lock()
	claimed := b.claims[info.TargetID] != nil
	if !claimed && len(g.tabs) >= m.lim.MaxTabsPerGroup {
		m.mu.Unlock()
		// A popup past the cap is closed: the agent's tabs are the agent's to manage.
		_ = b.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": info.TargetID}, nil)
		m.publish("tab.refused", map[string]any{"group_id": g.ID, "url": info.URL, "reason": "tab_cap"})
		return nil
	}
	m.tabSeq++
	t := &Tab{ID: "t" + strconv.Itoa(m.tabSeq), Group: g, TargetID: info.TargetID, Session: session, Opener: opener,
		CreatedAt: time.Now().UTC(), url: info.URL, title: info.Title, life: map[string]bool{}, wake: make(chan struct{})}
	m.settingUp[session] = t
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		delete(m.settingUp, session)
		m.mu.Unlock()
	}()
	// The page's size is its window's: in the pinned Chromium the screencast shows the window whatever
	// Emulation says, so an emulated viewport would put every click beside what the frame shows.
	m.sizeWindow(ctx, b, info.TargetID, g.ViewportNow())
	var replies []<-chan error
	var methods []string
	for _, call := range []struct {
		method string
		params any
	}{
		{"Page.enable", nil},
		{"Page.setLifecycleEventsEnabled", map[string]any{"enabled": true}},
		// For the page's requests as the agent reads them (page.network), the status of its document
		// and the challenge of a site that asks for a password. Chromium keeps the most recent
		// responses' bodies within these bounds, so the one the agent asks for is there to read; the
		// rest are never copied out of the browser.
		{"Network.enable", map[string]any{"maxTotalBufferSize": NetworkBufferBytes, "maxResourceBufferSize": NetworkResourceBytes}},
		// The page's console and its uncaught errors, and the browser's own words about it (a
		// resource that failed to load, a blocked script), for page.logs. Measured on the pinned
		// build: enabling Runtime runs no getter of the page's (an accessor is previewed as such), so
		// a page cannot tell it is on the way it could with older builds.
		{"Runtime.enable", nil},
		{"Log.enable", nil},
		{"Emulation.setUserAgentOverride", map[string]any{"userAgent": b.userAgent, "userAgentMetadata": b.uaMetadata}},
		// The frames of other sites, each in a process of its own, are attached to as they come, so
		// the page model can read them (Manager.attachFrame).
		{"Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": true, "flatten": true}},
	} {
		replies = append(replies, b.conn.Send(ctx, session, call.method, call.params))
		methods = append(methods, call.method)
	}
	// Before the page runs, so its first navigation (a popup's) meets the guard too.
	guarded := false
	if method, params, ok := m.guardSetup(); ok {
		replies = append(replies, b.conn.Send(ctx, session, method, params))
		methods = append(methods, method)
		guarded = true
	}
	replies = append(replies, b.conn.Send(ctx, session, "Runtime.runIfWaitingForDebugger", nil))
	methods = append(methods, "Runtime.runIfWaitingForDebugger")
	for i, r := range replies {
		if err := <-r; err != nil {
			m.log.Warn("page setup", "method", methods[i], "error", err.Error())
		}
	}
	m.measureFrame(ctx, b, session, info.TargetID, g.ViewportNow())
	m.mu.Lock()
	if _, ok := m.groups[g.ID]; !ok {
		m.mu.Unlock()
		return nil
	}
	if guarded {
		t.values = map[any]any{guardKey{}: true}
	}
	m.tabs[t.ID] = t
	m.bySession[session] = t
	m.byTarget[info.TargetID] = t
	g.tabs = append(g.tabs, t)
	if g.active == nil || opener != "" {
		g.active = t
	}
	ch := b.claims[info.TargetID]
	delete(b.claims, info.TargetID)
	delete(b.claimGroups, info.TargetID)
	m.mu.Unlock()
	if ch != nil {
		ch <- t
	}
	m.publish("tab.created", map[string]any{"group_id": g.ID, "tab": t.View()})
	return t
}

// NewTab opens a tab in the group and makes it the active one; with url set, it navigates there.
func (m *Manager) NewTab(ctx context.Context, g *Group, url string, checkCap bool) (*Tab, error) {
	b := g.Browser
	m.mu.Lock()
	if checkCap && len(g.tabs) >= m.lim.MaxTabsPerGroup {
		m.mu.Unlock()
		return nil, errLimit("tabs", m.lim.MaxTabsPerGroup, "this group has %d tabs open, the most it may; close one", len(g.tabs))
	}
	if spare := b.spare; spare != nil && g.ContextID == "" {
		b.spare = nil
		m.mu.Unlock()
		t := m.setupTab(g, spare.session, spare.info, "")
		if t == nil {
			return nil, fmt.Errorf("the browser's first page could not be prepared")
		}
		g.SetActive(t)
		return m.afterNewTab(ctx, t, url)
	}
	m.mu.Unlock()
	params := map[string]any{"url": "about:blank"}
	if g.ContextID != "" {
		params["browserContextId"] = g.ContextID
	}
	var r struct {
		TargetID string `json:"targetId"`
	}
	if err := b.conn.Call(ctx, "", "Target.createTarget", params, &r); err != nil {
		return nil, fmt.Errorf("a new tab: %w", err)
	}
	m.mu.Lock()
	if t := m.byTarget[r.TargetID]; t != nil {
		m.mu.Unlock()
		g.SetActive(t)
		return m.afterNewTab(ctx, t, url)
	}
	ch := make(chan *Tab, 1)
	b.claims[r.TargetID] = ch
	b.claimGroups[r.TargetID] = g
	pa, early := b.early[r.TargetID]
	delete(b.early, r.TargetID)
	m.mu.Unlock()
	if early {
		go m.setupTab(g, pa.session, pa.info, "")
	}
	select {
	case t := <-ch:
		if t == nil {
			return nil, fmt.Errorf("the new tab could not be prepared")
		}
		g.SetActive(t)
		return m.afterNewTab(ctx, t, url)
	case <-time.After(15 * time.Second):
	case <-ctx.Done():
	}
	m.mu.Lock()
	delete(b.claims, r.TargetID)
	delete(b.claimGroups, r.TargetID)
	m.mu.Unlock()
	return nil, fmt.Errorf("the new tab never attached")
}

func (m *Manager) afterNewTab(ctx context.Context, t *Tab, url string) (*Tab, error) {
	if url != "" {
		if _, err := m.Navigate(ctx, t, url, 30*time.Second); err != nil {
			return t, err
		}
	}
	return t, nil
}

// CloseTab closes one tab.
func (m *Manager) CloseTab(ctx context.Context, t *Tab) error {
	if err := t.Group.Browser.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": t.TargetID}, nil); err != nil {
		return err
	}
	// The target's end arrives as an event; waiting for it makes the reply mean the tab is gone.
	select {
	case <-t.Wake():
	case <-time.After(5 * time.Second):
	}
	return nil
}

// SelectTab makes t the group's active tab and brings it forward.
func (m *Manager) SelectTab(ctx context.Context, t *Tab) error {
	t.Group.SetActive(t)
	return t.Group.Browser.conn.Call(ctx, "", "Target.activateTarget", map[string]any{"targetId": t.TargetID}, nil)
}

// NavResult is what a navigation gives back.
type NavResult struct {
	URL   string `json:"url"`
	Title string `json:"title"`
	Error string `json:"error,omitempty"`
}

// Navigate loads url in t and waits for its load event, at most timeout. A page that failed to load
// (no such host, refused) is a result with its error, not an error: the agent reads it and decides.
func (m *Manager) Navigate(ctx context.Context, t *Tab, url string, timeout time.Duration) (NavResult, error) {
	if d := t.Dialog(); d != nil {
		return NavResult{}, ErrDialogOpen(d)
	}
	// Judged before the navigation is made, so a refusal is the agent's 1102 with its reason rather
	// than the proxy's error page. The proxy refuses the same destinations on its own either way.
	if m.deps.Wall != nil {
		if err := m.deps.Wall.Navigation(ctx, t.Group.Browser.ID, url); err != nil {
			return NavResult{}, err
		}
	}
	var r struct {
		LoaderID  string `json:"loaderId"`
		ErrorText string `json:"errorText"`
	}
	nctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if err := t.Call(nctx, "Page.navigate", map[string]any{"url": url}, &r); err != nil {
		return NavResult{}, err
	}
	if r.ErrorText != "" {
		return m.result(ctx, t, r.ErrorText), nil
	}
	if r.LoaderID != "" {
		if err := t.WaitLifecycle(nctx, r.LoaderID, "load"); err != nil {
			if nctx.Err() != nil && ctx.Err() == nil {
				// Slow pages are common; what has loaded is usable, and the agent is told.
				return m.result(ctx, t, "the page was still loading after "+timeout.String()), nil
			}
			return NavResult{}, err
		}
	}
	return m.result(ctx, t, ""), nil
}

func (m *Manager) result(ctx context.Context, t *Tab, errText string) NavResult {
	var info struct {
		TargetInfo targetInfo `json:"targetInfo"`
	}
	ictx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := t.Group.Browser.conn.Call(ictx, "", "Target.getTargetInfo", map[string]any{"targetId": t.TargetID}, &info); err == nil {
		t.infoChanged(info.TargetInfo.URL, info.TargetInfo.Title)
	}
	return NavResult{URL: t.URL(), Title: t.Title(), Error: errText}
}

// History goes back (delta -1) or forward (+1), or reloads (0).
func (m *Manager) History(ctx context.Context, t *Tab, delta int, timeout time.Duration) (NavResult, error) {
	if d := t.Dialog(); d != nil {
		return NavResult{}, ErrDialogOpen(d)
	}
	nctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	t.mu.Lock()
	before := t.loader
	t.mu.Unlock()
	if delta == 0 {
		if err := t.Call(nctx, "Page.reload", nil, nil); err != nil {
			return NavResult{}, err
		}
	} else {
		var h struct {
			CurrentIndex int `json:"currentIndex"`
			Entries      []struct {
				ID int `json:"id"`
			} `json:"entries"`
		}
		if err := t.Call(nctx, "Page.getNavigationHistory", nil, &h); err != nil {
			return NavResult{}, err
		}
		i := h.CurrentIndex + delta
		if i < 0 || i >= len(h.Entries) {
			return NavResult{}, wire.Errorf(wire.CodeForbidden, "there is no page to go %s to", map[bool]string{true: "back", false: "forward"}[delta < 0])
		}
		if err := t.Call(nctx, "Page.navigateToHistoryEntry", map[string]any{"entryId": h.Entries[i].ID}, nil); err != nil {
			return NavResult{}, err
		}
	}
	// A new document announces itself with a new loader; a page restored from the back-forward cache
	// or moved within one document does not, and is done at once.
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	settle := time.NewTimer(300 * time.Millisecond)
	defer settle.Stop()
	for {
		t.mu.Lock()
		changed, loaded := t.loader != before, t.life["load"]
		wake := t.wake
		t.mu.Unlock()
		if changed && loaded {
			break
		}
		select {
		case <-wake:
			continue
		case <-settle.C:
			if !changed {
				return m.result(ctx, t, ""), nil
			}
			continue
		case <-deadline.C:
			return m.result(ctx, t, "the page was still loading after "+timeout.String()), nil
		case <-ctx.Done():
			return NavResult{}, ctx.Err()
		}
	}
	return m.result(ctx, t, ""), nil
}

// RefreshTargets reads every page's address and title from Chromium. A title a page sets from a
// script announces itself in no event (targetInfoChanged fires for navigations), so lists read it
// fresh, and a watched browser is read once a second.
func (m *Manager) RefreshTargets(ctx context.Context, b *Browser) {
	var r struct {
		TargetInfos []targetInfo `json:"targetInfos"`
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := b.conn.Call(rctx, "", "Target.getTargets", nil, &r); err != nil {
		return
	}
	for _, info := range r.TargetInfos {
		m.mu.Lock()
		t := m.byTarget[info.TargetID]
		m.mu.Unlock()
		if t != nil {
			t.infoChanged(info.URL, info.Title)
		}
	}
}

// RunTitles reads the titles of watched browsers once a second until stop is closed.
func (m *Manager) RunTitles(stop <-chan struct{}) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
		}
		for _, b := range m.Browsers() {
			if m.deps.Busy != nil && m.deps.Busy(b) {
				m.RefreshTargets(context.Background(), b)
			}
		}
	}
}

// Resize makes every open page of the group vp CSS pixels and remembers that as the group's size.
// The picture the operator watches is this window drawn into the pane. A page left at the size it
// was opened (1280×800) in a taller pane leaves an empty band under the picture, and scaling the
// picture to cover the band would crop sides the agent can still click. The agent's reads use this
// same size, so the page it sees is the page in the pane.
func (m *Manager) Resize(ctx context.Context, groupID string, vp Viewport) (Viewport, error) {
	if vp.W < config.MinViewport || vp.W > config.MaxViewport || vp.H < config.MinViewport || vp.H > config.MaxViewport {
		return Viewport{}, wire.Errorf(wire.CodeInvalidParams, "viewport sides must be %d-%d", config.MinViewport, config.MaxViewport)
	}
	g, err := m.Group(groupID)
	if err != nil {
		return Viewport{}, err
	}
	if g.ViewportNow() == vp {
		return vp, nil
	}
	for _, t := range g.Tabs() {
		if t.Closed() {
			continue
		}
		m.sizeWindow(ctx, g.Browser, t.TargetID, vp)
	}
	g.setViewport(vp)
	return vp, nil
}

// sizeWindow gives a page's window the size that makes its viewport vp, once the browser knows how
// much of a window its frame takes (measureFrame).
func (m *Manager) sizeWindow(ctx context.Context, b *Browser, target string, vp Viewport) {
	var w struct {
		WindowID int `json:"windowId"`
	}
	if err := b.conn.Call(ctx, "", "Browser.getWindowForTarget", map[string]any{"targetId": target}, &w); err != nil {
		m.log.Warn("window of a page", "error", err.Error())
		return
	}
	b.mu.Lock()
	dx, dy := b.frameW, b.frameH
	b.mu.Unlock()
	if err := b.conn.Call(ctx, "", "Browser.setWindowBounds", map[string]any{"windowId": w.WindowID,
		"bounds": map[string]any{"width": vp.W + dx, "height": vp.H + dy, "windowState": "normal"}}, nil); err != nil {
		m.log.Warn("sizing a page's window", "error", err.Error())
	}
}

// measureFrame learns, on a browser's first page, how much of a window is not page: a window of
// 1280×800 in --headless=new holds a page of 1280×657. A window's new size reaches its page a moment
// after it is set, and a size read in between is the old one, so the page's size is read until it
// holds still, compared with the window's own, and the result checked on the page once resized.
func (m *Manager) measureFrame(ctx context.Context, b *Browser, session, target string, vp Viewport) {
	b.mu.Lock()
	known := b.frameKnown
	b.mu.Unlock()
	if known {
		return
	}
	var w struct {
		WindowID int `json:"windowId"`
	}
	if err := b.conn.Call(ctx, "", "Browser.getWindowForTarget", map[string]any{"targetId": target}, &w); err != nil {
		return
	}
	for attempt := 0; attempt < 3; attempt++ {
		inner, ok := m.settledInner(ctx, b, session)
		if !ok {
			return
		}
		var wb struct {
			Bounds struct {
				Width  int `json:"width"`
				Height int `json:"height"`
			} `json:"bounds"`
		}
		if err := b.conn.Call(ctx, "", "Browser.getWindowBounds", map[string]any{"windowId": w.WindowID}, &wb); err != nil {
			return
		}
		if inner[0] == vp.W && inner[1] == vp.H {
			b.mu.Lock()
			b.frameW, b.frameH = wb.Bounds.Width-vp.W, wb.Bounds.Height-vp.H
			b.frameKnown = true
			b.mu.Unlock()
			return
		}
		b.mu.Lock()
		b.frameW, b.frameH = max(0, wb.Bounds.Width-inner[0]), max(0, wb.Bounds.Height-inner[1])
		b.mu.Unlock()
		m.sizeWindow(ctx, b, target, vp)
	}
	m.log.Warn("a page's size never matched its window", "browser", b.ID)
}

// settledInner reads a page's size until three reads 50 ms apart agree.
func (m *Manager) settledInner(ctx context.Context, b *Browser, session string) ([2]int, bool) {
	var last [2]int
	same := 0
	for i := 0; i < 40; i++ {
		var r struct {
			Result struct {
				Value []int `json:"value"`
			} `json:"result"`
		}
		err := b.conn.Call(ctx, session, "Runtime.evaluate", map[string]any{"expression": "[innerWidth, innerHeight]",
			"returnByValue": true}, &r)
		if err != nil || len(r.Result.Value) != 2 {
			return last, false
		}
		cur := [2]int{r.Result.Value[0], r.Result.Value[1]}
		if cur == last {
			same++
			if same >= 2 {
				return cur, true
			}
		} else {
			same = 0
		}
		last = cur
		select {
		case <-time.After(50 * time.Millisecond):
		case <-ctx.Done():
			return last, false
		}
	}
	return last, true
}
