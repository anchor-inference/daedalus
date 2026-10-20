// Package relaytest is a fake of the Daedalus extension for the tests: a relay that fronts a real
// Chromium the way the extension fronts the operator's Chrome. It connects to a daemon of the
// attach backend over relay.open, as the bridge does, and gives the daemon only what
// chrome.debugger gives an extension: one debuggee per tab it made or let in ("x<tabId>"), the flat
// child sessions under each ("x<tabId>/<sessionId>"), and the browser-level operations as calls it
// carries out itself. Every DevTools method the daemon sends is held to the relay's allowlist, so
// the Browser domain, Target above a tab and the cookie methods are refused here as the extension
// refuses them, and a test that passes over it proves the daemon never needed them.
package relaytest

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Options say which Chromium the fake fronts and which daemon it connects to.
type Options struct {
	// RunDir is the daemon's run directory.
	RunDir string
	// Chromium is the browser to run; empty finds one as the daemon would.
	Chromium string
	// Dir holds the fake's profile and its downloads.
	Dir string
	// Proxy is a network wall's address for the browser, so the tests' pages are reached the way the
	// launch backend reaches them.
	Proxy string
	Args  []string
	// Hello replaces fields of the fake's hello when set.
	ProfileID, ProfileName string
}

// Seen is a frame the daemon sent the fake.
type Seen struct {
	T       string
	Session string
	Method  string
	Op      string
	Args    json.RawMessage
}

type tab struct {
	id      int
	target  string
	session string // the real flat session the fake holds on it
	group   string
}

// Fake is one fake extension.
type Fake struct {
	o        Options
	proc     *chrome.Process
	conn     *cdp.Conn
	client   *clienttest.Client
	channel  uint32
	version  string
	download string

	smu sync.Mutex // one relay frame's pieces at a time on the channel

	mu        sync.Mutex
	next      int
	tabs      map[int]*tab
	byTarget  map[string]int
	bySession map[string]int // the tabs' real sessions and their children's, to the tab
	groups    map[string]bool
	seen      []Seen
	noPong    bool
	checks    map[int64]chan relay.CheckOK
	byes      []relay.Bye
	welcome   chan relay.Welcome
	closed    chan struct{}
}

// Start runs the browser, connects to the daemon as a relay and waits for its welcome.
func Start(o Options) (*Fake, error) {
	found, ok := chrome.Find(o.Chromium)
	if !ok {
		return nil, errors.New("no Chromium")
	}
	f := &Fake{o: o, tabs: map[int]*tab{}, byTarget: map[string]int{}, bySession: map[string]int{}, groups: map[string]bool{},
		checks: map[int64]chan relay.CheckOK{}, welcome: make(chan relay.Welcome, 1), closed: make(chan struct{}), next: 100}
	f.download = filepath.Join(o.Dir, "downloads")
	if err := os.MkdirAll(f.download, 0o700); err != nil {
		return nil, err
	}
	proc, err := chrome.Start(chrome.Options{Path: found.Path, ProfileDir: filepath.Join(o.Dir, "profile"), Proxy: o.Proxy, Args: o.Args,
		ScreenW: 2560, ScreenH: 1440}, f.onEvent)
	if err != nil {
		return nil, err
	}
	f.proc, f.conn = proc, proc.Conn
	var ver struct {
		Product string `json:"product"`
	}
	if err := proc.FirstAnswer(context.Background(), 40*time.Second, "Browser.getVersion", &ver); err != nil {
		proc.Kill()
		return nil, err
	}
	f.version = ver.Product[strings.IndexByte(ver.Product, '/')+1:]
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	// The fake's own view of the browser, as the extension has chrome.tabs: which pages appear, who
	// opened them, and where Chrome saves a download.
	for _, c := range []struct {
		method string
		params any
	}{
		{"Target.setDiscoverTargets", map[string]any{"discover": true}},
		{"Browser.setDownloadBehavior", map[string]any{"behavior": "allowAndName", "downloadPath": f.download, "eventsEnabled": true}},
	} {
		if err := f.conn.Call(ctx, "", c.method, c.params, nil); err != nil {
			proc.Kill()
			return nil, err
		}
	}
	if err := f.Connect(); err != nil {
		proc.Kill()
		return nil, err
	}
	return f, nil
}

// Connect opens a relay to the daemon and sends hello; it returns once welcomed. A fake whose relay
// was closed connects again with it.
func (f *Fake) Connect() error {
	c, err := clienttest.Dial(f.o.RunDir)
	if err != nil {
		return err
	}
	var r struct {
		Channel uint32 `json:"channel"`
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Call(ctx, "relay.open", map[string]any{"transport": relay.TransportNative}, &r); err != nil {
		c.Close()
		return err
	}
	f.mu.Lock()
	f.client, f.channel = c, r.Channel
	f.closed = make(chan struct{})
	closed := f.closed
	f.mu.Unlock()
	go f.read(c, r.Channel, closed)
	id, name := f.o.ProfileID, f.o.ProfileName
	if id == "" {
		id = "fake-profile"
	}
	if name == "" {
		name = "Fake Chrome"
	}
	if err := f.Send(relay.Hello{T: relay.THello, Proto: relay.Proto, ExtVersion: "0.0.0-test", ChromeVersion: f.version, Platform: "linux",
		ProfileID: id, ProfileName: name}); err != nil {
		return err
	}
	select {
	case <-f.welcome:
		return nil
	case <-closed:
		return errors.New("the daemon closed the relay instead of welcoming it")
	case <-time.After(10 * time.Second):
		return errors.New("no welcome")
	}
}

// Send sends one frame to the daemon.
func (f *Fake) Send(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	f.mu.Lock()
	c, ch := f.client, f.channel
	f.mu.Unlock()
	f.smu.Lock()
	defer f.smu.Unlock()
	for _, p := range relay.Split(b, wire.MaxPayload) {
		if err := c.Send(ch, p); err != nil {
			return err
		}
	}
	return nil
}

func (f *Fake) read(c *clienttest.Client, channel uint32, closed chan struct{}) {
	defer close(closed)
	join := relay.Joiner{Max: relay.MaxToExtension}
	for {
		select {
		case fr, ok := <-c.Frames:
			if !ok {
				return
			}
			if fr.Channel != channel {
				continue
			}
			if len(fr.Payload) == 0 {
				return
			}
			b, err := join.Add(fr.Payload)
			if err != nil || b == nil {
				continue
			}
			f.frame(b)
		case <-c.Closed():
			return
		}
	}
}

// Closed is closed when the daemon has closed the current relay.
func (f *Fake) Closed() <-chan struct{} {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closed
}

func (f *Fake) frame(b []byte) {
	t, err := relay.Type(b)
	if err != nil {
		return
	}
	switch t {
	case relay.TWelcome:
		var w relay.Welcome
		_ = json.Unmarshal(b, &w)
		select {
		case f.welcome <- w:
		default:
		}
	case relay.TCDP:
		var c relay.CDP
		if json.Unmarshal(b, &c) != nil {
			return
		}
		f.record(Seen{T: t, Session: c.Session, Method: c.Method})
		f.cdp(c)
	case relay.TCall:
		var c struct {
			ID   int64           `json:"id"`
			Op   string          `json:"op"`
			Args json.RawMessage `json:"args"`
		}
		if json.Unmarshal(b, &c) != nil {
			return
		}
		f.record(Seen{T: t, Op: c.Op, Args: c.Args})
		go f.call(c.ID, c.Op, c.Args)
	case relay.TPing:
		var p relay.Ping
		_ = json.Unmarshal(b, &p)
		f.record(Seen{T: t})
		f.mu.Lock()
		quiet := f.noPong
		f.mu.Unlock()
		if !quiet {
			_ = f.Send(relay.Pong{T: relay.TPong, N: p.N})
		}
	case relay.TCheckOK:
		var ok relay.CheckOK
		_ = json.Unmarshal(b, &ok)
		f.mu.Lock()
		ch := f.checks[ok.N]
		f.mu.Unlock()
		if ch != nil {
			ch <- ok
		}
	case relay.TBye:
		var bye relay.Bye
		_ = json.Unmarshal(b, &bye)
		f.mu.Lock()
		f.byes = append(f.byes, bye)
		f.mu.Unlock()
	}
}

func (f *Fake) record(s Seen) {
	f.mu.Lock()
	f.seen = append(f.seen, s)
	f.mu.Unlock()
}

func (f *Fake) answer(id int64, value any, err error) {
	r := relay.Result{T: relay.TResult, ID: id, OK: err == nil}
	if err != nil {
		r.Error = toError(err)
	} else if value != nil {
		r.Value, _ = json.Marshal(value)
	}
	_ = f.Send(r)
}

// toError is a failure as the extension reports it: Chrome's own DevTools error as its JSON text.
func toError(err error) *relay.Error {
	var re *relay.Error
	if errors.As(err, &re) {
		return re
	}
	var ce *cdp.Error
	if errors.As(err, &ce) {
		b, _ := json.Marshal(ce)
		return &relay.Error{Code: relay.ErrCDP, Message: string(b)}
	}
	return &relay.Error{Code: relay.ErrFailed, Message: err.Error()}
}

// cdp forwards one command the moment it arrives, as chrome.debugger.sendCommand does.
func (f *Fake) cdp(c relay.CDP) {
	if err := relay.Allowed(c.Method); err != nil {
		f.answer(c.ID, nil, &relay.Error{Code: relay.ErrRefused, Message: err.Error()})
		return
	}
	id, child, ok := relay.ParseSession(c.Session)
	f.mu.Lock()
	t := f.tabs[id]
	real := ""
	if ok && t != nil {
		real = t.session
		if child != "" {
			if f.bySession[child] != id {
				real = ""
			} else {
				real = child
			}
		}
	}
	f.mu.Unlock()
	if real == "" {
		f.answer(c.ID, nil, &relay.Error{Code: relay.ErrNoTab, Message: "no such debuggee " + c.Session})
		return
	}
	var params any
	if len(c.Params) > 0 {
		params = c.Params
	}
	reply := f.conn.Start(context.Background(), real, c.Method, params)
	go func() {
		r := <-reply
		if r.Err != nil {
			f.answer(c.ID, nil, r.Err)
			return
		}
		f.answer(c.ID, r.Result, nil)
	}()
}

// onEvent turns the browser's events into what the extension sends: a tab's and its children's
// events under their relay sessions, and the tabs that appear, change and go.
func (f *Fake) onEvent(e cdp.Event) {
	if e.Session == "" {
		f.browserEvent(e)
		return
	}
	f.mu.Lock()
	id, ok := f.bySession[e.Session]
	var t *tab
	if ok {
		t = f.tabs[id]
	}
	f.mu.Unlock()
	if t == nil {
		return
	}
	session := relay.SessionOf(id)
	if e.Session != t.session {
		session += "/" + e.Session
	}
	if e.Method == "Target.attachedToTarget" {
		var p struct {
			SessionID string `json:"sessionId"`
		}
		if json.Unmarshal(e.Params, &p) == nil && p.SessionID != "" {
			f.mu.Lock()
			f.bySession[p.SessionID] = id
			f.mu.Unlock()
		}
	}
	_ = f.Send(relay.Event{T: relay.TEvent, Session: session, Method: e.Method, Params: e.Params})
	if e.Method == "Target.detachedFromTarget" {
		var p struct {
			SessionID string `json:"sessionId"`
		}
		if json.Unmarshal(e.Params, &p) == nil {
			f.mu.Lock()
			delete(f.bySession, p.SessionID)
			f.mu.Unlock()
		}
	}
}

type targetInfo struct {
	TargetID string `json:"targetId"`
	Type     string `json:"type"`
	URL      string `json:"url"`
	Title    string `json:"title"`
	OpenerID string `json:"openerId"`
}

func (f *Fake) browserEvent(e cdp.Event) {
	var p struct {
		TargetInfo targetInfo `json:"targetInfo"`
		TargetID   string     `json:"targetId"`
	}
	if json.Unmarshal(e.Params, &p) != nil {
		return
	}
	switch e.Method {
	case "Target.targetCreated":
		info := p.TargetInfo
		if info.Type != "page" || info.OpenerID == "" {
			return
		}
		f.mu.Lock()
		opener, ok := f.byTarget[info.OpenerID]
		var group string
		if ok {
			group = f.tabs[opener].group
		}
		f.mu.Unlock()
		if !ok {
			return
		}
		// A popup of an agent tab joins its group, as chrome.tabs.onCreated with an openerTabId of
		// one of them does in the extension.
		go func() {
			t, err := f.attach(info.TargetID, group)
			if err != nil {
				return
			}
			_ = f.Send(relay.Tabs{T: relay.TTabs, Op: relay.TabsCreated, Tab: &relay.Tab{ID: t.id, Group: group, Opener: opener, URL: info.URL, Title: info.Title}})
		}()
	case "Target.targetInfoChanged":
		f.mu.Lock()
		id, ok := f.byTarget[p.TargetInfo.TargetID]
		f.mu.Unlock()
		if ok {
			_ = f.Send(relay.Tabs{T: relay.TTabs, Op: relay.TabsUpdated, Tab: &relay.Tab{ID: id, URL: p.TargetInfo.URL, Title: p.TargetInfo.Title}})
		}
	case "Target.targetDestroyed":
		f.mu.Lock()
		id, ok := f.byTarget[p.TargetID]
		if ok {
			t := f.tabs[id]
			delete(f.tabs, id)
			delete(f.byTarget, p.TargetID)
			delete(f.bySession, t.session)
		}
		f.mu.Unlock()
		if ok {
			_ = f.Send(relay.Tabs{T: relay.TTabs, Op: relay.TabsRemoved, Tab: &relay.Tab{ID: id}})
		}
	}
}

// attach takes a page as a debuggee of the group: chrome.debugger.attach({tabId}).
func (f *Fake) attach(target, group string) (*tab, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var r struct {
		SessionID string `json:"sessionId"`
	}
	if err := f.conn.Call(ctx, "", "Target.attachToTarget", map[string]any{"targetId": target, "flatten": true}, &r); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.next++
	t := &tab{id: f.next, target: target, session: r.SessionID, group: group}
	f.tabs[t.id] = t
	f.byTarget[target] = t.id
	f.bySession[r.SessionID] = t.id
	return t, nil
}

func (f *Fake) groupTabs(group string) []*tab {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []*tab
	for _, t := range f.tabs {
		if t.group == group {
			out = append(out, t)
		}
	}
	return out
}

func (f *Fake) tab(id int) *tab {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.tabs[id]
}

// call carries out one browser-level operation with the fake's own hold on the browser, as the
// extension does with chrome.tabs, chrome.windows, chrome.downloads and chrome.cookies.
func (f *Fake) call(id int64, op string, raw json.RawMessage) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	v, err := f.do(ctx, op, raw)
	f.answer(id, v, err)
}

func (f *Fake) do(ctx context.Context, op string, raw json.RawMessage) (any, error) {
	noTab := func(id int) error {
		return &relay.Error{Code: relay.ErrNoTab, Message: fmt.Sprintf("no tab %d of the agent's", id)}
	}
	switch op {
	case relay.OpWindowEnsure:
		var a relay.GroupArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		f.mu.Lock()
		f.groups[a.Group] = true
		f.mu.Unlock()
		out := relay.WindowEnsured{Window: 1, Tabs: []relay.Tab{}}
		for _, t := range f.groupTabs(a.Group) {
			out.Tabs = append(out.Tabs, relay.Tab{ID: t.id, Group: a.Group})
		}
		return out, nil
	case relay.OpTabCreate:
		var a relay.TabCreateArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		var r struct {
			TargetID string `json:"targetId"`
		}
		if err := f.conn.Call(ctx, "", "Target.createTarget", map[string]any{"url": "about:blank", "newWindow": true}, &r); err != nil {
			return nil, err
		}
		t, err := f.attach(r.TargetID, a.Group)
		if err != nil {
			return nil, err
		}
		return relay.TabCreated{Tab: relay.Tab{ID: t.id, Group: a.Group, URL: "about:blank"}}, nil
	case relay.OpTabClose, relay.OpTabActivate:
		var a relay.TabArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		t := f.tab(a.Tab)
		if t == nil {
			return nil, noTab(a.Tab)
		}
		method := "Target.closeTarget"
		if op == relay.OpTabActivate {
			method = "Target.activateTarget"
		}
		return map[string]any{}, f.conn.Call(ctx, "", method, map[string]any{"targetId": t.target}, nil)
	case relay.OpWindowBounds:
		var a relay.WindowBoundsArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		tabs := f.groupTabs(a.Group)
		if len(tabs) == 0 {
			return nil, &relay.Error{Code: relay.ErrFailed, Message: "the group has no window yet"}
		}
		var first relay.Bounds
		for i, t := range tabs {
			var w struct {
				WindowID int `json:"windowId"`
			}
			if err := f.conn.Call(ctx, "", "Browser.getWindowForTarget", map[string]any{"targetId": t.target}, &w); err != nil {
				return nil, err
			}
			if a.Bounds != nil {
				if err := f.conn.Call(ctx, "", "Browser.setWindowBounds", map[string]any{"windowId": w.WindowID,
					"bounds": map[string]any{"width": a.Bounds.Width, "height": a.Bounds.Height, "windowState": "normal"}}, nil); err != nil {
					return nil, err
				}
			}
			if i == 0 {
				var wb struct {
					Bounds relay.Bounds `json:"bounds"`
				}
				if err := f.conn.Call(ctx, "", "Browser.getWindowBounds", map[string]any{"windowId": w.WindowID}, &wb); err != nil {
					return nil, err
				}
				first = wb.Bounds
			}
		}
		return relay.WindowBounds{Bounds: first}, nil
	case relay.OpGroupClose:
		var a relay.GroupArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		for _, t := range f.groupTabs(a.Group) {
			_ = f.conn.Call(ctx, "", "Target.closeTarget", map[string]any{"targetId": t.target}, nil)
		}
		f.mu.Lock()
		delete(f.groups, a.Group)
		f.mu.Unlock()
		return map[string]any{}, nil
	case relay.OpDownloadsSearch:
		var a relay.DownloadsSearchArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		path := filepath.Join(f.download, filepath.Base(a.ID))
		fi, err := os.Stat(path)
		if err != nil {
			return nil, &relay.Error{Code: relay.ErrFailed, Message: "no such download"}
		}
		return relay.Download{Path: path, State: "complete", Bytes: fi.Size()}, nil
	case relay.OpCookiesNames:
		var a relay.CookiesNamesArgs
		if err := json.Unmarshal(raw, &a); err != nil {
			return nil, err
		}
		return f.cookieNames(ctx, a.URL)
	case relay.OpNotify, relay.OpPanelGrant, relay.OpPanelEvent:
		return map[string]any{}, nil
	}
	return nil, &relay.Error{Code: relay.ErrRefused, Message: "no operation " + op}
}

// cookieNames is chrome.cookies.getAll({url}) reduced to names and flags, as the extension does.
func (f *Fake) cookieNames(ctx context.Context, raw string) (any, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" {
		return nil, &relay.Error{Code: relay.ErrFailed, Message: "not a URL"}
	}
	var r struct {
		Cookies []struct {
			Name     string  `json:"name"`
			Domain   string  `json:"domain"`
			Path     string  `json:"path"`
			Expires  float64 `json:"expires"`
			Secure   bool    `json:"secure"`
			HTTPOnly bool    `json:"httpOnly"`
			Session  bool    `json:"session"`
			SameSite string  `json:"sameSite"`
		} `json:"cookies"`
	}
	if err := f.conn.Call(ctx, "", "Storage.getCookies", nil, &r); err != nil {
		return nil, err
	}
	host := strings.ToLower(u.Hostname())
	out := relay.CookieNames{Cookies: []relay.CookieName{}}
	for _, c := range r.Cookies {
		d := strings.TrimPrefix(strings.ToLower(c.Domain), ".")
		if host != d && !strings.HasSuffix(host, "."+d) {
			continue
		}
		out.Cookies = append(out.Cookies, relay.CookieName{Name: c.Name, Domain: c.Domain, Path: c.Path, Expires: c.Expires,
			Secure: c.Secure, HTTPOnly: c.HTTPOnly, SameSite: c.SameSite, Session: c.Session})
	}
	return out, nil
}

// Seen lists what the daemon sent, oldest first.
func (f *Fake) Seen() []Seen {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Seen(nil), f.seen...)
}

// Byes lists the bye frames the daemon sent.
func (f *Fake) Byes() []relay.Bye {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]relay.Bye(nil), f.byes...)
}

// QuietPongs stops (or resumes) answering pings, as a suspended worker would.
func (f *Fake) QuietPongs(quiet bool) {
	f.mu.Lock()
	f.noPong = quiet
	f.mu.Unlock()
}

// Check sends the popup's check and waits for the daemon's check_ok.
func (f *Fake) Check(n int64, timeout time.Duration) (relay.CheckOK, error) {
	ch := make(chan relay.CheckOK, 1)
	f.mu.Lock()
	f.checks[n] = ch
	f.mu.Unlock()
	if err := f.Send(relay.Check{T: relay.TCheck, N: n}); err != nil {
		return relay.CheckOK{}, err
	}
	select {
	case ok := <-ch:
		return ok, nil
	case <-time.After(timeout):
		return relay.CheckOK{}, errors.New("no check_ok")
	}
}

// TabOf is the fake's tab id of a target (a tab's main frame id), or 0.
func (f *Fake) TabOf(target string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.byTarget[target]
}

// Conn is the fake's own hold on the browser, for a test that plays the operator.
func (f *Fake) Conn() *cdp.Conn { return f.conn }

// Disconnect closes the relay, as Chrome closing the native port does; the browser stays.
func (f *Fake) Disconnect() {
	f.mu.Lock()
	c, closed := f.client, f.closed
	f.mu.Unlock()
	if c != nil {
		c.Close()
		<-closed
	}
}

// Close ends the relay and the browser.
func (f *Fake) Close() {
	f.Disconnect()
	f.proc.Close(3 * time.Second)
}
