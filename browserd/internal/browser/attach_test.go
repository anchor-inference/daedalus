package browser

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The hazards of a real profile (the operator's own Chrome), each held off by the attach backend
// itself and not by convention. The extension here is scripted: it answers every command, makes
// tabs when asked, and records every frame the daemon sends, so a test can say what the daemon
// never asked for.

type scripted struct {
	t    *testing.T
	link *relay.Link
	join relay.Joiner

	mu     sync.Mutex
	frames []map[string]any
	next   int
	closed bool
	// fixedWindow refuses window.bounds with bounds, as the extension does while the agent works in
	// the operator's window; inner is then the page's size, which no resize changes.
	fixedWindow bool
	inner       []int
	// reply answers a DevTools method of the test's own; ok false leaves it to the defaults.
	reply func(method string, params map[string]any) (value any, ok bool)
	// given are the tabs tab.close lets go of rather than closes, as the extension does with a tab
	// the operator gave.
	given map[int]bool
}

func (s *scripted) Send(p []byte) error {
	s.mu.Lock()
	b, err := s.join.Add(p)
	s.mu.Unlock()
	if err != nil || b == nil {
		return err
	}
	var f map[string]any
	if err := json.Unmarshal(b, &f); err != nil {
		return err
	}
	s.mu.Lock()
	s.frames = append(s.frames, f)
	s.mu.Unlock()
	// Answered on a goroutine of its own, as the extension answers whenever Chrome does.
	go s.answer(f)
	return nil
}

func (s *scripted) Close() {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
}

func (s *scripted) send(v any) {
	b, _ := json.Marshal(v)
	for _, p := range relay.Split(b, wire.MaxPayload) {
		s.link.Frame(p)
	}
}

func (s *scripted) answer(f map[string]any) {
	id, _ := f["id"].(float64)
	result := func(v any) {
		raw, _ := json.Marshal(v)
		s.send(relay.Result{T: relay.TResult, ID: int64(id), OK: true, Value: raw})
	}
	switch f["t"] {
	case relay.TPing:
		s.send(relay.Pong{T: relay.TPong, N: int64(f["n"].(float64))})
	case relay.TCDP:
		session, _ := f["session"].(string)
		s.mu.Lock()
		reply := s.reply
		s.mu.Unlock()
		if reply != nil {
			params, _ := f["params"].(map[string]any)
			if v, ok := reply(f["method"].(string), params); ok {
				result(v)
				return
			}
		}
		switch f["method"] {
		case "Page.getFrameTree":
			result(map[string]any{"frameTree": map[string]any{"frame": map[string]any{"id": "F" + session, "url": "about:blank"}}})
		case "Runtime.evaluate":
			s.mu.Lock()
			inner := s.inner
			s.mu.Unlock()
			if inner == nil {
				inner = []int{config.DefaultViewportW, config.DefaultViewportH}
			}
			result(map[string]any{"result": map[string]any{"value": inner}})
		case "Page.getNavigationHistory":
			result(map[string]any{"currentIndex": 0, "entries": []map[string]any{{"url": "about:blank", "title": ""}}})
		default:
			result(map[string]any{})
		}
	case relay.TCall:
		switch f["op"] {
		case relay.OpWindowEnsure:
			result(relay.WindowEnsured{Window: 1, Tabs: []relay.Tab{}})
		case relay.OpTabCreate:
			s.mu.Lock()
			s.next++
			n := s.next
			s.mu.Unlock()
			args, _ := f["args"].(map[string]any)
			group, _ := args["group"].(string)
			result(relay.TabCreated{Tab: relay.Tab{ID: n, Group: group, URL: "about:blank"}})
		case relay.OpWindowBounds:
			s.mu.Lock()
			fixed := s.fixedWindow
			s.mu.Unlock()
			if args, _ := f["args"].(map[string]any); fixed && args["bounds"] != nil {
				s.send(relay.Result{T: relay.TResult, ID: int64(id), Error: &relay.Error{Code: relay.ErrRefused, Message: "the agent works in the operator's window"}})
				return
			}
			result(relay.WindowBounds{Bounds: relay.Bounds{Width: config.DefaultViewportW, Height: config.DefaultViewportH}})
		case relay.OpTabClose:
			args, _ := f["args"].(map[string]any)
			tab, _ := args["tab"].(float64)
			s.mu.Lock()
			given := s.given[int(tab)]
			s.mu.Unlock()
			result(relay.TabClosed{Released: given})
		default:
			result(map[string]any{})
		}
	}
}

// seen lists the frames the daemon sent of type t.
func (s *scripted) seen(t string) []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []map[string]any
	for _, f := range s.frames {
		if f["t"] == t {
			out = append(out, f)
		}
	}
	return out
}

func (s *scripted) calls(op string) int {
	n := 0
	for _, f := range s.seen(relay.TCall) {
		if f["op"] == op {
			n++
		}
	}
	return n
}

func attachManager(t *testing.T) (*Manager, *scripted, *relay.Hub) {
	t.Helper()
	dir := t.TempDir()
	cfg := &config.Config{Env: "user", StateDir: filepath.Join(dir, "state"), Backend: config.BackendAttach, Limits: config.DefaultLimits()}
	cfg.Limits.IdleClose = time.Millisecond
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	hub := relay.NewHub(relay.Options{Log: log})
	m := New(Deps{Config: cfg, Log: log, Relay: hub})
	s := &scripted{t: t, join: relay.Joiner{Max: relay.MaxToExtension}}
	s.link = hub.Open(s, relay.TransportNative, "")
	s.send(relay.Hello{T: relay.THello, Proto: relay.Proto, ChromeVersion: "141.0", ProfileID: "p", ProfileName: "Home"})
	deadline := time.Now().Add(5 * time.Second)
	for hub.Current() == nil {
		if time.Now().After(deadline) {
			t.Fatal("the relay was not accepted")
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Cleanup(m.Shutdown)
	return m, s, hub
}

func openAttached(t *testing.T, m *Manager, group string) (*Group, *Tab) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	g, tab, _, err := m.Open(ctx, OpenParams{GroupID: group, Profile: "project-a"})
	if err != nil {
		t.Fatal(err)
	}
	return g, tab
}

// The daemon reaches the operator's Chrome only through the tabs it made and calls the extension
// makes: no command to the browser, no browser-wide auto-attach or downloads, no user agent, no
// script the daemon would put in every page, and nothing written where a profile would be.
func TestAttachNeverReachesTheBrowser(t *testing.T) {
	m, s, _ := attachManager(t)
	g, tab := openAttached(t, m, "s-1")
	if g.Browser.Profile != UserProfile || !g.Browser.Attached() || g.Browser.Pid() != 0 {
		t.Fatalf("browser %+v", g.Browser.View())
	}
	if tab.Session != "x1" || tab.TargetID != "Fx1" {
		t.Fatalf("tab %s %s", tab.Session, tab.TargetID)
	}
	if s.calls(relay.OpWindowEnsure) != 1 || s.calls(relay.OpTabCreate) != 1 {
		t.Fatalf("calls %v", s.seen(relay.TCall))
	}
	if n := len(s.seen(relay.TCDP)); n < 5 {
		t.Fatalf("only %d commands prepared the tab", n)
	}
	for _, f := range s.seen(relay.TCDP) {
		method, _ := f["method"].(string)
		if err := relay.Allowed(method); err != nil {
			t.Errorf("the daemon sent %s: %v", method, err)
		}
		if session, _ := f["session"].(string); !strings.HasPrefix(session, "x") {
			t.Errorf("a command to %q", session)
		}
		switch method {
		case "Emulation.setUserAgentOverride", "Page.addScriptToEvaluateOnNewDocument", "Input.dispatchMouseEvent":
			t.Errorf("the daemon sent %s while preparing a tab of the operator's Chrome", method)
		}
	}
	if _, err := os.Stat(filepath.Join(m.deps.Config.StateDir, "profiles")); !os.IsNotExist(err) {
		t.Fatalf("a profile directory was made: %v", err)
	}
	if _, err := os.Stat(filepath.Join(m.deps.Config.StateDir, "downloads")); !os.IsNotExist(err) {
		t.Fatalf("a download directory was made: %v", err)
	}
	if p := m.Profiles(); len(p) != 0 {
		t.Fatalf("profiles %v", p)
	}
}

// A tab the extension names that is neither opened by an agent tab nor put in an agent's group is
// the operator's: it is never taken, paused or closed.
func TestAttachLeavesOtherTabsAlone(t *testing.T) {
	m, s, hub := attachManager(t)
	g, _ := openAttached(t, m, "s-1")
	before := len(s.frames)
	obs := &attachObserver{a: m.backend.(*attach), b: g.Browser}
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsCreated, Tab: &relay.Tab{ID: 900, URL: "https://mail.example/"}})
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsCreated, Tab: &relay.Tab{ID: 901, Opener: 77, URL: "https://bank.example/"}})
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsCreated, Tab: &relay.Tab{ID: 902, Group: "someone-else"}})
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsRemoved, Tab: &relay.Tab{ID: 903}})
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsGroupClosed, Group: "someone-else"})
	time.Sleep(100 * time.Millisecond)
	s.mu.Lock()
	after := s.frames[before:]
	s.mu.Unlock()
	for _, f := range after {
		if f["t"] != relay.TPing {
			t.Errorf("the daemon answered a tab not its own with %v", f)
		}
	}
	if n := len(g.Tabs()); n != 1 {
		t.Fatalf("the group has %d tabs", n)
	}
	if hub.Current() == nil {
		t.Fatal("the relay ended")
	}
	// A popup of the agent's tab is the agent's.
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsCreated, Tab: &relay.Tab{ID: 904, Opener: 1, Group: "s-1"}})
	if n := len(g.Tabs()); n != 2 {
		t.Fatalf("the popup was not taken: %d tabs", n)
	}
	// And one the operator put in the group.
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsJoined, Tab: &relay.Tab{ID: 905, Group: "s-1"}})
	if n := len(g.Tabs()); n != 3 {
		t.Fatalf("the joined tab was not taken: %d tabs", n)
	}
	// Taken back, it goes without a word to Chrome.
	closes := s.calls(relay.OpTabClose)
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsLeft, Tab: &relay.Tab{ID: 905}})
	if n := len(g.Tabs()); n != 2 || s.calls(relay.OpTabClose) != closes {
		t.Fatalf("after the operator took a tab back: %d tabs, %d closes", n, s.calls(relay.OpTabClose))
	}
	// The operator closing the group in Chrome ends the daemon's group without closing anything.
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsGroupClosed, Group: "s-1"})
	if _, err := m.Group("s-1"); err == nil || s.calls(relay.OpGroupClose) != 0 {
		t.Fatalf("after the group closed in Chrome: %v, %d group.close", err, s.calls(relay.OpGroupClose))
	}
}

// The operator's Chrome is never closed for idling, never measured as the daemon's cost and never
// ended for the memory limit; the daemon stopping lets go of it without closing the agent's tabs.
func TestAttachNeverClosesTheBrowser(t *testing.T) {
	m, s, hub := attachManager(t)
	g, _ := openAttached(t, m, "s-1")
	time.Sleep(5 * time.Millisecond)
	m.CloseIdle(time.Now().Add(time.Hour))
	time.Sleep(50 * time.Millisecond)
	if len(m.Browsers()) != 1 || s.calls(relay.OpGroupClose) != 0 {
		t.Fatalf("idle: %d browsers, %d group.close", len(m.Browsers()), s.calls(relay.OpGroupClose))
	}
	if st := m.Sample(procstat.NewSampler(), time.Now()); len(st.Browsers) != 0 {
		t.Fatalf("the operator's Chrome measured as the daemon's: %+v", st.Browsers)
	}
	if err := m.ClearProfile("project-a", false); codeOf(err) != wire.CodeForbidden {
		t.Fatalf("profile.clear: %v", err)
	}
	if err := m.ClearProfile("project-a", true); codeOf(err) != wire.CodeForbidden {
		t.Fatalf("profile.delete: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, _, _, err := m.Open(ctx, OpenParams{GroupID: "e-1", Profile: EphemeralProfile}); codeOf(err) != wire.CodeForbidden {
		t.Fatalf("a throwaway group: %v", err)
	}
	m.Shutdown()
	if len(m.Browsers()) != 0 {
		t.Fatal("the browser outlived the shutdown")
	}
	if n := s.calls(relay.OpGroupClose); n != 0 {
		t.Fatalf("shutdown closed %d groups in the operator's Chrome", n)
	}
	if hub.Current() == nil {
		t.Fatal("the daemon's shutdown of its browsers ended the relay; the hub's own Close does")
	}
	if !g.Closed() {
		t.Fatal("the group outlived its browser")
	}
}

// browser.close lets go: the agent's groups are closed through the extension, and the relay stays for
// the next group.
func TestAttachCloseBrowserDetaches(t *testing.T) {
	m, s, hub := attachManager(t)
	g, _ := openAttached(t, m, "s-1")
	if _, err := m.CloseBrowser(g.Browser.ID, "closed"); err != nil {
		t.Fatal(err)
	}
	if s.calls(relay.OpGroupClose) != 1 || hub.Current() == nil {
		t.Fatalf("%d group.close, relay %v", s.calls(relay.OpGroupClose), hub.Current() != nil)
	}
	if _, err := m.Group("s-1"); codeOf(err) != CodeBrowserGone {
		t.Fatalf("the group after its browser closed: %v", err)
	}
	g2, tab := openAttached(t, m, "s-1")
	if g2.Browser == g.Browser || tab == nil {
		t.Fatal("the group did not open again on the same relay")
	}
}

// The relay's end is the browser's: its groups answer 1108 with why.
func TestAttachRelayLost(t *testing.T) {
	m, s, _ := attachManager(t)
	g, _ := openAttached(t, m, "s-1")
	s.link.Closed()
	select {
	case <-g.Browser.gone:
	case <-time.After(5 * time.Second):
		t.Fatal("the browser outlived its relay")
	}
	_, err := m.Group("s-1")
	if codeOf(err) != CodeBrowserGone || !strings.Contains(err.Error(), "chrome disconnected") {
		t.Fatalf("after the relay was lost: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _, _, err = m.Open(ctx, OpenParams{GroupID: "s-1", Profile: "project-a"})
	if codeOf(err) != wire.CodeUnsupported || !strings.Contains(fmt.Sprint(err), "not connected") {
		t.Fatalf("an open with no Chrome: %v", err)
	}
}

// The operator's own input in an agent tab gives them control; the echo of the daemon's own input
// does not.
func TestAttachOperatorInput(t *testing.T) {
	m, _, _ := attachManager(t)
	g, tab := openAttached(t, m, "s-1")
	a := m.backend.(*attach)
	obs := &attachObserver{a: a, b: g.Browser}
	a.markDispatch(1)
	obs.RelayInput(1)
	if c := g.Control(); c.Owner != OwnerAgent {
		t.Fatalf("the daemon's own input took control: %+v", c)
	}
	a.mu.Lock()
	a.dispatched[1] = time.Now().Add(-time.Second)
	a.mu.Unlock()
	obs.RelayInput(1)
	c := g.Control()
	if c.Owner != OwnerHuman || c.Holder != ChromeHolder || time.Until(c.Until) > ChromeInputTTL {
		t.Fatalf("after the operator's input: %+v", c)
	}
	if err := g.Gate(context.Background(), nil, 10*time.Millisecond); codeOf(err) != CodeHumanDriving {
		t.Fatalf("the agent while the operator types: %v", err)
	}
	if _, err := g.SetControl(OwnerPaused, "", 0, "reading"); err != nil {
		t.Fatal(err)
	}
	obs.RelayInput(1)
	if g.Control().Owner != OwnerPaused {
		t.Fatal("input took control from a pause")
	}
	_ = tab
}

// sizings counts the window.bounds calls that asked for a size.
func (s *scripted) sizings() int {
	n := 0
	for _, f := range s.seen(relay.TCall) {
		if args, _ := f["args"].(map[string]any); f["op"] == relay.OpWindowBounds && args["bounds"] != nil {
			n++
		}
	}
	return n
}

// lockedLog is a log written from the daemon's goroutines and read by the test.
type lockedLog struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (l *lockedLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *lockedLog) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.String()
}

// In the operator's own window the extension refuses to size it: the daemon asks once for the
// group, measures the frame from what it read, and neither asks again for the next tabs nor warns.
func TestAttachOperatorsWindowIsAskedOnce(t *testing.T) {
	m, s, _ := attachManager(t)
	logged := &lockedLog{}
	m.log = slog.New(slog.NewTextHandler(logged, &slog.HandlerOptions{Level: slog.LevelDebug}))
	s.mu.Lock()
	s.fixedWindow = true
	// The operator's window holds a page of its own size, never the group's viewport.
	s.inner = []int{1500, 900}
	s.mu.Unlock()
	g, _ := openAttached(t, m, "s-1")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for i := 0; i < 2; i++ {
		if _, err := m.NewTab(ctx, g, "", true); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := m.Resize(ctx, g.ID, Viewport{W: 1000, H: 700}); err != nil {
		t.Fatal(err)
	}
	if n := s.sizings(); n != 1 {
		t.Fatalf("the window was asked to be sized %d times", n)
	}
	g.Browser.mu.Lock()
	known := g.Browser.frameKnown
	g.Browser.mu.Unlock()
	if !known {
		t.Fatal("the frame was never taken as measured")
	}
	out := logged.String()
	if strings.Contains(out, "level=WARN") || strings.Count(out, "operator's window") != 1 {
		t.Fatalf("the log:\n%s", out)
	}
	// A group opened again asks afresh: the operator may have moved the agent to a window of its own.
	s.mu.Lock()
	s.fixedWindow = false
	s.mu.Unlock()
	if _, err := m.CloseGroup(ctx, g.ID); err != nil {
		t.Fatal(err)
	}
	openAttached(t, m, "s-1")
	if n := s.sizings(); n != 2 {
		t.Fatalf("the reopened group's window was asked to be sized %d times in all", n)
	}
}

// A tab whose debugger the extension lost is known as such: listed as detached, nothing waits on it,
// and the tab the extension announces after attaching again is a fresh one.
func TestAttachDetachedTab(t *testing.T) {
	m, s, _ := attachManager(t)
	g, tab := openAttached(t, m, "s-1")
	if _, gone := tab.Detached(); gone || tab.View()["detached"] != nil {
		t.Fatalf("a new tab is detached: %v", tab.View())
	}
	s.send(relay.Event{T: relay.TEvent, Session: tab.Session, Method: "Inspector.detached", Params: json.RawMessage(`{"reason":"canceled_by_user"}`)})
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, gone := tab.Detached(); gone {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the tab never knew it was detached")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if reason, _ := tab.Detached(); reason != "canceled_by_user" || tab.View()["detached"] != true {
		t.Fatalf("detached %q, view %v", reason, tab.View())
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := tab.WaitLifecycle(ctx, "never", "load")
	var we *wire.Error
	if !errors.As(err, &we) || we.Code != CodeNoSuchTab || ctx.Err() != nil {
		t.Fatalf("waiting on a detached tab: %v", err)
	}
	if data, _ := we.Data.(map[string]any); data["tab_id"] != tab.ID || data["reason"] != "detached" {
		t.Fatalf("the error's data: %v", we.Data)
	}
	obs := &attachObserver{a: m.backend.(*attach), b: g.Browser}
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsJoined, Tab: &relay.Tab{ID: 1, Group: "s-1"}})
	tabs := g.Tabs()
	if len(tabs) != 1 || tabs[0] == tab {
		t.Fatalf("after the tab was attached again: %v", tabs)
	}
	if _, gone := tabs[0].Detached(); gone {
		t.Fatal("the tab attached again is still detached")
	}
}

// The operator's Chrome pauses every tab's document responses, the guard on or off, so a PDF never
// reaches Chrome's viewer; SyncGuard keeps that pattern when the guard's changes.
func TestAttachPausesDocumentResponses(t *testing.T) {
	m, s, _ := attachManager(t)
	_, tab := openAttached(t, m, "s-1")
	var stages []string
	for _, f := range s.seen(relay.TCDP) {
		if f["method"] != "Fetch.enable" {
			continue
		}
		params, _ := f["params"].(map[string]any)
		patterns, _ := params["patterns"].([]any)
		for _, p := range patterns {
			pm, _ := p.(map[string]any)
			stages = append(stages, fmt.Sprint(pm["resourceType"], "/", pm["requestStage"]))
		}
	}
	if len(stages) != 1 || stages[0] != "Document/Response" {
		t.Fatalf("Fetch.enable patterns %v", stages)
	}
	if st, _ := tab.Value(fetchKey{}).(fetchState); st != (fetchState{pdf: true}) {
		t.Fatalf("the tab's fetch state %+v", st)
	}
	before := len(s.seen(relay.TCDP))
	m.SyncGuard(context.Background())
	for _, f := range s.seen(relay.TCDP)[before:] {
		if f["method"] == "Fetch.disable" || f["method"] == "Fetch.enable" {
			t.Fatalf("an unchanged guard sent %v", f["method"])
		}
	}
}

func TestPDFResponses(t *testing.T) {
	for raw, want := range map[string]bool{
		`{"requestId":"1","responseStatusCode":200}`:           true,
		`{"requestId":"1","responseErrorReason":"Failed"}`:     true,
		`{"requestId":"1","request":{"url":"https://a.test"}}`: false,
	} {
		if atResponse(json.RawMessage(raw)) != want {
			t.Errorf("atResponse(%s) != %v", raw, want)
		}
	}
	for ct, want := range map[string]bool{"application/pdf": true, "Application/PDF; charset=binary": true, "application/x-pdf": true,
		"text/html": false, "": false} {
		if isPDF(ct) != want {
			t.Errorf("isPDF(%q) != %v", ct, want)
		}
	}
	for _, c := range []struct{ disposition, url, want string }{
		{`attachment; filename="Invoice 12.pdf"`, "https://a.test/x", "Invoice 12.pdf"},
		{`inline; filename*=UTF-8''%D0%A1%D1%87%D1%91%D1%82.pdf`, "https://a.test/x", "Счёт.pdf"},
		{"", "https://a.test/docs/Annual%20report.pdf?v=2", "Annual report.pdf"},
		{"", "https://a.test/download", "download.pdf"},
		{"", "https://a.test/", "document.pdf"},
	} {
		if got := pdfName(c.disposition, c.url); got != c.want {
			t.Errorf("pdfName(%q, %q) = %q, want %q", c.disposition, c.url, got, c.want)
		}
	}
}

// keeperStub keeps the files the PDF path hands it, in memory.
type keeperStub struct {
	mu   sync.Mutex
	kept [][]byte
}

func (k *keeperStub) TabEvent(t *Tab, e cdp.Event)         {}
func (k *keeperStub) BrowserEvent(b *Browser, e cdp.Event) {}
func (k *keeperStub) TabGone(t *Tab)                       {}
func (k *keeperStub) KeepFile(t *Tab, name, url, mime string, data []byte) (Kept, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.kept = append(k.kept, data)
	return Kept{ID: fmt.Sprintf("d%d", len(k.kept)), Name: name, Size: int64(len(data)), Mime: mime}, nil
}

func (k *keeperStub) count() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return len(k.kept)
}

// methods lists the DevTools methods the daemon sent to a session, in order.
func (s *scripted) methods(session string) []string {
	var out []string
	for _, f := range s.seen(relay.TCDP) {
		if f["session"] == session {
			out = append(out, fmt.Sprint(f["method"]))
		}
	}
	return out
}

func pdfPaused(tab *Tab, length string) json.RawMessage {
	headers := `[{"name":"Content-Type","value":"application/pdf"}`
	if length != "" {
		headers += `,{"name":"Content-Length","value":"` + length + `"}`
	}
	return json.RawMessage(`{"requestId":"r1","frameId":"` + tab.TargetID + `","request":{"url":"https://a.test/report.pdf"},"responseStatusCode":200,"responseHeaders":` + headers + `]}`)
}

func waitTabs(t *testing.T, g *Group, n int) []*Tab {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if tabs := g.Tabs(); len(tabs) == n {
			return tabs
		}
		if time.Now().After(deadline) {
			t.Fatalf("the group has %d tabs, not %d", len(g.Tabs()), n)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// A tab the operator gave is known for it: listed as given, never paused for a PDF, and let go by
// tab.close, which the extension answers as released.
func TestAttachGivenTab(t *testing.T) {
	m, s, _ := attachManager(t)
	g, own := openAttached(t, m, "s-1")
	s.mu.Lock()
	s.given = map[int]bool{50: true}
	s.mu.Unlock()
	obs := &attachObserver{a: m.backend.(*attach), b: g.Browser}
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsJoined, Tab: &relay.Tab{ID: 50, Group: "s-1", URL: "https://mail.example/", Given: true}})
	tabs := waitTabs(t, g, 2)
	given := tabs[1]
	if !given.Given || given.View()["given"] != true || own.Given || own.View()["given"] != nil {
		t.Fatalf("given %v, the agent's own %v", given.View(), own.View())
	}
	for _, method := range s.methods(given.Session) {
		if method == "Fetch.enable" {
			t.Fatal("the operator's tab pauses its document responses for PDFs")
		}
	}
	if st, _ := given.Value(fetchKey{}).(fetchState); st.pdf {
		t.Fatalf("the given tab's fetch state %+v", st)
	}
	// A tab announced afresh after a reattach is the agent's when the extension does not say given.
	obs.RelayTabs(relay.Tabs{T: relay.TTabs, Op: relay.TabsJoined, Tab: &relay.Tab{ID: 1, Group: "s-1"}})
	for _, tab := range waitTabs(t, g, 2) {
		if tab.Session == own.Session && tab.Given {
			t.Fatal("a reattached tab of the agent's became given")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := m.CloseTab(ctx, given); err != nil {
		t.Fatal(err)
	}
	if len(g.Tabs()) != 1 {
		t.Fatalf("after letting go of the given tab: %d tabs", len(g.Tabs()))
	}
}

// A PDF is taken out of a response only in a tab of the agent's while the agent drives; the operator's
// own tab, and any tab while control is theirs, gets it in Chrome as before.
func TestPDFIsKeptOnlyWhileTheAgentDrives(t *testing.T) {
	m, s, _ := attachManager(t)
	k := &keeperStub{}
	m.Listen(k)
	g, tab := openAttached(t, m, "s-1")
	s.mu.Lock()
	s.reply = func(method string, params map[string]any) (any, bool) {
		if method == "Fetch.getResponseBody" {
			return map[string]any{"body": "JVBERi0xLjcK", "base64Encoded": true}, true
		}
		return nil, false
	}
	s.mu.Unlock()
	if _, err := g.SetControl(OwnerPaused, "", 0, "the operator reads"); err != nil {
		t.Fatal(err)
	}
	m.responsePaused(tab, pdfPaused(tab, "8"))
	if k.count() != 0 || !slices.Contains(s.methods(tab.Session), "Fetch.continueRequest") {
		t.Fatalf("while paused: kept %d, sent %v", k.count(), s.methods(tab.Session))
	}
	if _, err := g.SetControl(OwnerAgent, "", 0, ""); err != nil {
		t.Fatal(err)
	}
	given := &Tab{ID: "t9", Group: g, TargetID: tab.TargetID, Session: tab.Session, Given: true}
	m.responsePaused(given, pdfPaused(given, "8"))
	if k.count() != 0 {
		t.Fatal("a PDF of the operator's own tab was kept")
	}
	m.responsePaused(tab, pdfPaused(tab, "8"))
	if k.count() != 1 || !slices.Contains(s.methods(tab.Session), "Fetch.failRequest") {
		t.Fatalf("while the agent drives: kept %d, sent %v", k.count(), s.methods(tab.Session))
	}
}

// A PDF of no stated length is read as a stream a piece at a time, and given up past the limit
// without reading the rest; a body taken as a stream cannot go back to Chrome, so the request fails.
func TestPDFOfNoStatedLengthIsStreamed(t *testing.T) {
	m, s, _ := attachManager(t)
	k := &keeperStub{}
	m.Listen(k)
	_, tab := openAttached(t, m, "s-1")
	piece := strings.Repeat("x", 1000)
	var mu sync.Mutex
	reads, pieces := 0, 3
	s.mu.Lock()
	s.reply = func(method string, params map[string]any) (any, bool) {
		mu.Lock()
		defer mu.Unlock()
		switch method {
		case "Fetch.getResponseBody":
			t.Error("a body of no stated length was read whole")
			return map[string]any{}, true
		case "Fetch.takeResponseBodyAsStream":
			return map[string]any{"stream": "st1"}, true
		case "IO.read":
			reads++
			return map[string]any{"data": piece, "eof": reads >= pieces}, true
		}
		return nil, false
	}
	s.mu.Unlock()
	m.responsePaused(tab, pdfPaused(tab, ""))
	if k.count() != 1 || len(k.kept[0]) != 3000 {
		t.Fatalf("kept %d files", k.count())
	}
	if !slices.Contains(s.methods(tab.Session), "IO.close") {
		t.Fatal("the stream was left open")
	}
	m.mu.Lock()
	m.lim.MaxDownloadBytes = 2500
	m.mu.Unlock()
	mu.Lock()
	reads, pieces = 0, 1000
	mu.Unlock()
	before := len(s.methods(tab.Session))
	m.responsePaused(tab, pdfPaused(tab, ""))
	mu.Lock()
	n := reads
	mu.Unlock()
	sent := s.methods(tab.Session)[before:]
	if k.count() != 1 || n != 3 || !slices.Contains(sent, "Fetch.failRequest") || slices.Contains(sent, "Fetch.continueRequest") {
		t.Fatalf("past the limit: kept %d, %d reads, sent %v", k.count(), n, sent)
	}
}
