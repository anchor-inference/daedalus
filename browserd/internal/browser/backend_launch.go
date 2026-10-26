package browser

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// launch is the backend of the agent's own browsers: Chromium started on a profile of the daemon's,
// over its debugging pipe, with the whole browser the daemon's to drive.
type launch struct {
	m *Manager
}

func (l *launch) Name() string { return BackendLaunch }

func (l *launch) start(ctx context.Context, profile string) (*Browser, error) {
	m := l.m
	m.mu.Lock()
	found, ok := m.found, m.foundOK
	if !ok {
		// Installed since the daemon started? Look again rather than make the operator restart it.
		found, ok = chrome.Find(m.deps.Config.Chromium.Path)
		m.found, m.foundOK = found, ok
	}
	m.mu.Unlock()
	if !ok {
		return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": "no Chromium"},
			"no Chromium was found; install the browser (Playwright's chromium) or name one with --chromium")
	}
	b := m.newBrowser(profile)
	b.Ephemeral = profile == EphemeralProfile
	dir := m.ProfileDir(profile)
	if b.Ephemeral {
		dir = filepath.Join(m.deps.Config.StateDir, "ephemeral", b.ID)
		b.tempDir = dir
	}
	b.profileDir = dir
	b.loadScripts()
	b.downloadDir = filepath.Join(m.deps.Config.StateDir, "downloads", b.ID)
	if err := os.MkdirAll(b.downloadDir, 0o700); err != nil {
		return nil, err
	}
	proxy := ""
	if m.deps.Wall != nil {
		addr, err := m.deps.Wall.Open(b.ID)
		if err != nil {
			return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": err.Error()}, "the browser's network wall did not start: %v", err)
		}
		proxy = addr
	}
	env, err := m.browserEnv()
	if err != nil {
		m.closeWall(b)
		return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": err.Error()}, "%v", err)
	}
	cc := m.deps.Config.Chromium
	screenW, screenH := cc.Screen()
	proc, err := chrome.Start(chrome.Options{Path: found.Path, ProfileDir: dir, Proxy: proxy, Args: cc.Args, NoSandbox: cc.NoSandbox,
		Env: env, Headed: cc.Headed(), ScreenW: screenW, ScreenH: screenH}, func(e cdp.Event) { m.onEvent(b, e) })
	if err != nil {
		m.closeWall(b)
		return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": err.Error()}, "Chromium did not start: %v", err)
	}
	b.proc = proc
	b.pid = proc.Pid
	b.conn = proc.Conn
	budget := config.ChromiumStart
	if d := m.deps.Config.Chromium.StartTimeout; d > 0 {
		budget = d
	}
	begin := time.Now()
	var ver struct {
		Product   string `json:"product"`
		UserAgent string `json:"userAgent"`
	}
	if err := proc.FirstAnswer(ctx, budget, "Browser.getVersion", &ver); err != nil {
		proc.Kill()
		m.closeWall(b)
		b.removeTemp()
		reason := chrome.SandboxProblem(proc.Stderr())
		if reason != "" {
			m.mu.Lock()
			m.sandbox = reason
			m.mu.Unlock()
			return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": reason}, "%s", reason)
		}
		return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": lastLine(proc.Stderr())}, "%v", err)
	}
	took := time.Since(begin)
	// The setup calls get a budget of their own: a browser that has answered once is up, and a slow
	// first answer must not leave them too little time.
	startCtx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	b.Version = ver.Product
	b.userAgent, b.uaMetadata = userAgent(ver.Product, ver.UserAgent)
	for _, call := range []struct {
		method string
		params any
	}{
		{"Target.setDiscoverTargets", map[string]any{"discover": true}},
		// Every new page waits for its setup (size, user agent, domains) before its first script.
		{"Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": true, "flatten": true}},
		{"Browser.setDownloadBehavior", map[string]any{"behavior": "allowAndName", "downloadPath": b.downloadDir, "eventsEnabled": true}},
	} {
		if err := b.conn.Call(startCtx, "", call.method, call.params, nil); err != nil {
			proc.Kill()
			m.closeWall(b)
			b.removeTemp()
			return nil, errWith(wire.CodeUnsupported, map[string]any{"reason": err.Error()}, "Chromium refused %s: %v", call.method, err)
		}
	}
	if !b.Ephemeral {
		// Before the first group's first page, so it opens with the extensions it had.
		m.restoreExtensions(startCtx, b)
	}
	// How long the first answer took is logged so a slow start shows in the log before it ever runs
	// out of budget.
	m.log.Info("browser started", "browser", b.ID, "profile", profile, "pid", proc.Pid, "chromium", ver.Product,
		"first_answer_ms", took.Milliseconds())
	return b, nil
}

// openGroup gives a throwaway group its own browser context, with the downloads of the browser.
func (l *launch) openGroup(ctx context.Context, g *Group) error {
	b := g.Browser
	if !b.Ephemeral {
		return nil
	}
	var r struct {
		BrowserContextID string `json:"browserContextId"`
	}
	err := b.conn.Call(ctx, "", "Target.createBrowserContext", map[string]any{"disposeOnDetach": false}, &r)
	if err == nil {
		g.ContextID = r.BrowserContextID
		err = b.conn.Call(ctx, "", "Browser.setDownloadBehavior", map[string]any{"behavior": "allowAndName",
			"browserContextId": g.ContextID, "downloadPath": b.downloadDir, "eventsEnabled": true}, nil)
	}
	if err != nil {
		return fmt.Errorf("a throwaway context: %w", err)
	}
	return nil
}

func (l *launch) closeGroup(ctx context.Context, g *Group, tabs []*Tab) {
	for _, t := range tabs {
		_ = g.Browser.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": t.TargetID}, nil)
	}
	if g.ContextID != "" {
		_ = g.Browser.conn.Call(ctx, "", "Target.disposeBrowserContext", map[string]any{"browserContextId": g.ContextID}, nil)
	}
}

// newTab gives the group the browser's first blank page when it is still spare, else a new target
// in a window of its own, and waits for its auto-attach to make it a tab (attachPage).
func (l *launch) newTab(ctx context.Context, g *Group) (*Tab, error) {
	m := l.m
	b := g.Browser
	m.mu.Lock()
	if spare := b.spare; spare != nil && g.ContextID == "" {
		b.spare = nil
		m.mu.Unlock()
		t := m.setupTab(g, spare.session, spare.info, "")
		if t == nil {
			return nil, fmt.Errorf("the browser's first page could not be prepared")
		}
		return t, nil
	}
	m.mu.Unlock()
	// A window of its own for every tab. Headless that is how Chromium makes them anyway; headed, a
	// tab beside another in one window is hidden while the other is shown, and a hidden page gives
	// the operator's view no frames.
	params := map[string]any{"url": "about:blank", "newWindow": true}
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
		return t, nil
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
		return t, nil
	case <-time.After(15 * time.Second):
	case <-ctx.Done():
	}
	m.mu.Lock()
	delete(b.claims, r.TargetID)
	delete(b.claimGroups, r.TargetID)
	m.mu.Unlock()
	return nil, fmt.Errorf("the new tab never attached")
}

func (l *launch) closeTab(ctx context.Context, t *Tab) error {
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

func (l *launch) closeRefused(ctx context.Context, b *Browser, target, session string) {
	_ = b.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": target}, nil)
}

func (l *launch) activate(ctx context.Context, t *Tab, quiet bool) error {
	return t.Group.Browser.conn.Call(ctx, "", "Target.activateTarget", map[string]any{"targetId": t.TargetID}, nil)
}

func (l *launch) refreshTab(ctx context.Context, t *Tab) {
	var info struct {
		TargetInfo targetInfo `json:"targetInfo"`
	}
	ictx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := t.Group.Browser.conn.Call(ictx, "", "Target.getTargetInfo", map[string]any{"targetId": t.TargetID}, &info); err == nil {
		t.infoChanged(info.TargetInfo.URL, info.TargetInfo.Title)
	}
}

func (l *launch) refresh(ctx context.Context, b *Browser) {
	var r struct {
		TargetInfos []targetInfo `json:"targetInfos"`
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := b.conn.Call(rctx, "", "Target.getTargets", nil, &r); err != nil {
		return
	}
	for _, info := range r.TargetInfos {
		l.m.mu.Lock()
		t := l.m.byTarget[info.TargetID]
		l.m.mu.Unlock()
		if t != nil {
			t.infoChanged(info.URL, info.Title)
		}
	}
}

// windowOf is the browser window a page is in.
func (l *launch) windowOf(ctx context.Context, b *Browser, target string) (int, error) {
	var w struct {
		WindowID int `json:"windowId"`
	}
	err := b.conn.Call(ctx, "", "Browser.getWindowForTarget", map[string]any{"targetId": target}, &w)
	return w.WindowID, err
}

func (l *launch) windowSize(ctx context.Context, g *Group, target string) (int, int, error) {
	b := g.Browser
	id, err := l.windowOf(ctx, b, target)
	if err != nil {
		return 0, 0, err
	}
	var wb struct {
		Bounds struct {
			Width  int `json:"width"`
			Height int `json:"height"`
		} `json:"bounds"`
	}
	if err := b.conn.Call(ctx, "", "Browser.getWindowBounds", map[string]any{"windowId": id}, &wb); err != nil {
		return 0, 0, err
	}
	return wb.Bounds.Width, wb.Bounds.Height, nil
}

func (l *launch) setWindowSize(ctx context.Context, g *Group, target string, w, h int) error {
	b := g.Browser
	id, err := l.windowOf(ctx, b, target)
	if err != nil {
		l.m.log.Warn("window of a page", "error", err.Error())
		return err
	}
	if err := b.conn.Call(ctx, "", "Browser.setWindowBounds", map[string]any{"windowId": id,
		"bounds": map[string]any{"width": w, "height": h, "windowState": "normal"}}, nil); err != nil {
		l.m.log.Warn("sizing a page's window", "error", err.Error())
		return err
	}
	return nil
}

func (l *launch) cancelDownload(ctx context.Context, b *Browser, guid string) error {
	return b.conn.Call(ctx, "", "Browser.cancelDownload", map[string]any{"guid": guid}, nil)
}

func (l *launch) downloadPath(ctx context.Context, b *Browser, guid string) (string, bool, error) {
	return filepath.Join(b.downloadDir, guid), true, nil
}
