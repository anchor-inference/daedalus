package browser

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

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
		switch f["method"] {
		case "Page.getFrameTree":
			result(map[string]any{"frameTree": map[string]any{"frame": map[string]any{"id": "F" + session, "url": "about:blank"}}})
		case "Runtime.evaluate":
			result(map[string]any{"result": map[string]any{"value": []int{config.DefaultViewportW, config.DefaultViewportH}}})
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
			result(relay.WindowBounds{Bounds: relay.Bounds{Width: config.DefaultViewportW, Height: config.DefaultViewportH}})
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
