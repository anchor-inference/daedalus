package rpc_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/ax"
	"github.com/ascorblack/daedalus/deskd/internal/ax/axfake"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// axChildEnv makes the test binary the fake accessibility tool when the daemon runs it as its child.
const axChildEnv = "DESKD_RPC_TEST_AX_CHILD"

// raceQuickExit spares every run of the fake tool the second a race-enabled binary otherwise waits
// before it exits.
const raceQuickExit = "GORACE=atexit_sleep_ms=0"

func TestMain(m *testing.M) {
	if os.Getenv(axChildEnv) == "1" {
		os.Exit(axfake.Main(os.Args[1:], os.Stdout))
	}
	os.Exit(m.Run())
}

// axHarness is the fake desktop with the fake tool reading it, as `--ax-bin fake` serves it.
type axHarness struct {
	*harness
	stateDir string
	toolLog  string
	mu       sync.Mutex
	tweak    func(*axfake.Scene)
}

func startAX(t *testing.T, cfg desk.Config, scene fake.Scene) *axHarness {
	t.Helper()
	a := &axHarness{}
	a.harness = startScene(t, cfg, scene, func(d *desk.Desk, fk *fake.Fake, dir string) {
		self, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		a.stateDir = filepath.Join(dir, "state", "ax")
		a.toolLog = filepath.Join(dir, "tool.jsonl")
		sceneFile := filepath.Join(dir, "axscene.json")
		var seen int64
		d.SetAX(ax.Open(context.Background(), ax.Config{Path: self, StateDir: a.stateDir,
			Env: []string{axChildEnv + "=1", axfake.SceneEnv + "=" + sceneFile, raceQuickExit},
			Before: func() error {
				sc := fk.AXScene(a.toolLog)
				a.mu.Lock()
				if a.tweak != nil {
					a.tweak(&sc)
				}
				a.mu.Unlock()
				b, _ := json.Marshal(sc)
				return os.WriteFile(sceneFile, b, 0o600)
			},
			After: func() { seen = recordToolActions(fk, a.toolLog, seen) }}))
	})
	return a
}

// recordToolActions hands the fake tool's actions to the fake desktop, as the daemon's fake mode
// does.
func recordToolActions(fk *fake.Fake, path string, offset int64) int64 {
	b, err := os.ReadFile(path)
	if err != nil || int64(len(b)) < offset {
		return offset
	}
	for _, line := range strings.Split(string(b[offset:]), "\n") {
		var l axfake.LogLine
		if json.Unmarshal([]byte(line), &l) == nil && l.Action != "" {
			fk.RecordAX(l.Action, l.Ref, l.Text, l.Window)
		}
	}
	return int64(len(b))
}

func (a *axHarness) setTweak(f func(*axfake.Scene)) {
	a.mu.Lock()
	a.tweak = f
	a.mu.Unlock()
}

func (a *axHarness) toolRuns() []axfake.LogLine {
	b, _ := os.ReadFile(a.toolLog)
	var out []axfake.LogLine
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var l axfake.LogLine
		if json.Unmarshal([]byte(line), &l) == nil {
			out = append(out, l)
		}
	}
	return out
}

func (a *axHarness) call(method string, p map[string]any, out any) error {
	p["session"] = session
	return a.host.Call(a.ctx, method, p, out)
}

func (a *axHarness) snapshot(p map[string]any) desk.AXSnapshotResult {
	a.t.Helper()
	var res desk.AXSnapshotResult
	if err := a.call("ax.snapshot", p, &res); err != nil {
		a.t.Fatalf("ax.snapshot %v: %v", p, err)
	}
	return res
}

// refOf finds the reference of the element named name.
func refOf(t *testing.T, n ax.Node, name string) string {
	t.Helper()
	var walk func(n ax.Node) string
	walk = func(n ax.Node) string {
		if n.Name != nil && *n.Name == name {
			return n.Ref
		}
		for _, c := range n.Children {
			if r := walk(c); r != "" {
				return r
			}
		}
		return ""
	}
	r := walk(n)
	if r == "" {
		t.Fatalf("no element %q", name)
	}
	return r
}

func nodeOf(n ax.Node, name string) *ax.Node {
	if n.Name != nil && *n.Name == name {
		return &n
	}
	for _, c := range n.Children {
		if r := nodeOf(c, name); r != nil {
			return r
		}
	}
	return nil
}

func axData(t *testing.T, err error) (int, map[string]any) {
	t.Helper()
	var we *wire.Error
	if !errors.As(err, &we) {
		t.Fatalf("not a refusal: %v", err)
	}
	m, _ := we.Data.(map[string]any)
	return we.Code, m
}

func TestTheGrantedWindowsTreeComesWithReferencesAndNoSecret(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	var info map[string]any
	if err := a.host.Call(a.ctx, "info", nil, &info); err != nil {
		t.Fatal(err)
	}
	if st, _ := info["ax"].(map[string]any); st["available"] != true || st["version"] != ax.PinnedVersion {
		t.Fatalf("info.ax: %v", info["ax"])
	}
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{"bounds": true})
	if res.Window != "10" || !res.Complete || res.SnapshotID == "" || res.Tree.Role != "window" {
		t.Fatalf("the snapshot: %+v", res)
	}
	pw := nodeOf(res.Tree, "Password")
	if pw == nil || pw.Value != nil || !pw.Secure || pw.Ref == "" {
		t.Fatalf("the password field: %+v", pw)
	}
	if save := nodeOf(res.Tree, "Save"); save == nil || save.Bounds == nil || save.Bounds.W != 80 {
		t.Fatalf("the Save button: %+v", save)
	}
	if st := nodeOf(res.Tree, "Saved an hour ago"); st == nil || st.Ref != "" {
		t.Fatalf("static text got a reference: %+v", st)
	}
	// The audit records the call, never the text of the screen.
	b, _ := os.ReadFile(a.auditPath)
	if !strings.Contains(string(b), `"event":"ax.snapshot"`) || strings.Contains(string(b), "notes.txt") ||
		strings.Contains(string(b), "Password") || strings.Contains(string(b), "hunter2") {
		t.Fatalf("the audit: %s", b)
	}
	// ax.get on the password field never asks the tool for its value.
	before := len(a.toolRuns())
	var got desk.AXGetResult
	if err := a.call("ax.get", map[string]any{"ref": pw.Ref, "property": "value"}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Value != nil || !got.Secure || len(a.toolRuns()) != before {
		t.Fatalf("the password's value: %+v (tool runs %d → %d)", got, before, len(a.toolRuns()))
	}
	// Other properties read the element.
	if err := a.call("ax.get", map[string]any{"ref": refOf(t, res.Tree, "Title"), "property": "value"}, &got); err != nil ||
		got.Value != "notes" {
		t.Fatalf("the title's value: %+v %v", got, err)
	}
	if err := a.call("ax.get", map[string]any{"ref": refOf(t, res.Tree, "Save"), "property": "enabled"}, &got); err != nil ||
		got.Value != true {
		t.Fatalf("the Save button's enabled: %+v %v", got, err)
	}
}

func TestAnElementActionReachesTheElementAndCountsAsAnAction(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	lease := a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	var act desk.AXActResult
	if err := a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Save"), "action": "click"}, &act); err != nil {
		t.Fatal(err)
	}
	if act.Delivery != desk.Delivered || !act.Verified || act.Window != "10" || act.ActionsLeft != lease.MaxActions-1 {
		t.Fatalf("the click: %+v", act)
	}
	// A secure field's value does not come back in the read-back, though the tool wrote it.
	if err := a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Password"), "action": "set_value", "text": "s3cret"}, &act); err != nil {
		t.Fatal(err)
	}
	if act.PostState == nil || act.PostState.Value != nil {
		t.Fatalf("the password's read-back: %+v", act.PostState)
	}
	in := a.fake.Inputs()
	if len(in) != 2 || in[0].Kind != "ax" || in[0].Action != "click" || in[0].Window != "10" || in[1].Text != "s3cret" {
		t.Fatalf("what reached the desktop: %+v", in)
	}
	b, _ := os.ReadFile(a.auditPath)
	if !strings.Contains(string(b), `"event":"ax.act"`) || !strings.Contains(string(b), `"role_name":"button"`) ||
		strings.Contains(string(b), "s3cret") || !strings.Contains(string(b), `"text_length":6`) {
		t.Fatalf("the audit: %s", b)
	}
}

func TestNothingOfTheTreeWithoutALeaseAndNoReferenceOutlivesOne(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	if err := a.call("ax.snapshot", map[string]any{"window": "10"}, nil); code(err) != desk.CodeNoLease {
		t.Fatalf("a snapshot with no lease: %v", err)
	}
	if err := a.op.Call(a.ctx, "ax.snapshot", map[string]any{"session": session, "window": "10"}, nil); code(err) != desk.CodeOperatorOnly {
		t.Fatalf("the operator's token: %v", err)
	}
	old := a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	save := refOf(t, res.Tree, "Save")
	home := filepath.Join(a.stateDir, old.ID)
	if _, err := os.Stat(home); err != nil {
		t.Fatalf("the lease's state root: %v", err)
	}
	if err := a.host.Call(a.ctx, "lease.revoke", map[string]any{"session": session}, nil); err != nil {
		t.Fatal(err)
	}
	if err := a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil); code(err) != desk.CodeNoLease {
		t.Fatalf("an action after the revoke: %v", err)
	}
	a.grant(map[string]any{"kind": "window", "window": "10"})
	err := a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil)
	if c, data := axData(t, err); c != desk.CodeStaleRef || data["reason"] != "unknown" || data["delivery"] != desk.NotDelivered {
		t.Fatalf("a reference from the last lease: %v %v", err, data)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(home); os.IsNotExist(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the ended lease's state root, with the screen's text, is still there")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if in := a.fake.Inputs(); len(in) != 0 {
		t.Fatalf("a refused action reached the desktop: %+v", in)
	}
}

func TestAReferenceTheDaemonDidNotHandOutIsRefused(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	snap := res.SnapshotID
	for ref, want := range map[string]int{
		"@sdeadbeef:e1":             desk.CodeStaleRef,      // a snapshot the daemon never took
		"@" + snap + ":e99":         desk.CodeStaleRef,      // an element the snapshot never had
		"--headed":                  wire.CodeInvalidParams, // not a reference at all
		"@" + snap + ":e1 --headed": wire.CodeInvalidParams,
	} {
		err := a.call("ax.act", map[string]any{"ref": ref, "action": "click"}, nil)
		if c, data := axData(t, err); c != want || data["delivery"] != desk.NotDelivered {
			t.Errorf("%q: %v %v", ref, err, data)
		}
	}
	if in := a.fake.Inputs(); len(in) != 0 {
		t.Fatalf("a refused reference reached the desktop: %+v", in)
	}
	for _, run := range a.toolRuns() {
		if run.Action != "" {
			t.Fatalf("the tool ran an action for a refused reference: %+v", run)
		}
	}
}

func TestWindowsOutsideTheLeaseProtectedOrElevatedAreRefusedBeforeTheToolRuns(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	if c, _ := axData(t, a.call("ax.snapshot", map[string]any{"window": "12"}, nil)); c != desk.CodeOutOfScope {
		t.Fatalf("another window under a window lease: %d", c)
	}
	if c, _ := axData(t, a.call("ax.find", map[string]any{"window": "12", "role": "button"}, nil)); c != desk.CodeOutOfScope {
		t.Fatalf("a find in another window: %d", c)
	}
	a.grant(map[string]any{"kind": "desktop"})
	if c, _ := axData(t, a.call("ax.snapshot", map[string]any{"window": "11"}, nil)); c != desk.CodeProtected {
		t.Fatalf("Daedalus's own window: %d", c)
	}
	if c, _ := axData(t, a.call("ax.snapshot", map[string]any{"window": "13"}, nil)); c != desk.CodeUnreachable {
		t.Fatalf("an elevated window: %d", c)
	}
	if err := a.call("ax.snapshot", map[string]any{}, nil); code(err) != wire.CodeInvalidParams {
		t.Fatalf("no window under a desktop lease: %v", err)
	}
	for _, run := range a.toolRuns() {
		for _, arg := range run.Args {
			if arg == fake.AXToolID("11") || arg == fake.AXToolID("12") || arg == fake.AXToolID("13") {
				t.Fatalf("the tool was asked about a refused window: %+v", run)
			}
		}
	}
	// An app lease reaches the app's window in the background, without bringing it forward.
	a.grant(map[string]any{"kind": "app", "app": "com.example.terminal"})
	res := a.snapshot(map[string]any{"window": "12"})
	var act desk.AXActResult
	if err := a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Clear"), "action": "click"}, &act); err != nil {
		t.Fatalf("a background window of the leased app: %v", err)
	}
	var apps desk.AppsResult
	if err := a.host.Call(a.ctx, "apps.list", map[string]any{"session": session}, &apps); err != nil || apps.Foreground == nil ||
		apps.Foreground.ID != "10" {
		t.Fatalf("the element action moved the keyboard: %+v %v", apps.Foreground, err)
	}
}

func TestAReferenceIsJudgedByItsWindowWhenItActs(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "display", "display": "1"})
	res := a.snapshot(map[string]any{"window": "10"})
	save := refOf(t, res.Tree, "Save")
	// The window goes to the other display, outside the lease: its references go with it.
	a.simulate(map[string]any{"move_window": "10", "dx": -1900})
	if c, _ := axData(t, a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil)); c != desk.CodeOutOfScope {
		t.Fatalf("a reference whose window left the lease: %d", c)
	}
	if in := a.fake.Inputs(); len(in) != 0 {
		t.Fatalf("it reached the desktop: %+v", in)
	}
}

func TestThePauseTheLockAndTheBudgetHoldForElementActions(t *testing.T) {
	a := startAX(t, desk.Config{HumanQuietMs: new(int64)}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"}, map[string]any{"max_actions": 2})
	res := a.snapshot(map[string]any{})
	save := refOf(t, res.Tree, "Save")
	a.simulate(map[string]any{"human_input": true, "dx": 30})
	waitPaused(t, a.harness)
	err := a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil)
	if c, data := axData(t, err); c != desk.CodeHumanDriving || data["delivery"] != desk.NotDelivered {
		t.Fatalf("an action while the operator's hand is on the machine: %v %v", err, data)
	}
	// Reading is not acting: the tree is still there while paused, as screenshots are.
	a.snapshot(map[string]any{})
	if err := a.op.Call(a.ctx, "resume", map[string]any{}, nil); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	err = a.call("ax.act", map[string]any{"ref": save, "action": "click"}, nil)
	if c, data := axData(t, err); c != desk.CodeRate || data["limit"] != "lease_actions" {
		t.Fatalf("past the lease's actions: %v %v", err, data)
	}
	a.fake.Inputs()
	a.simulate(map[string]any{"secure": map[string]any{"locked": true, "reason": "lock_screen"}})
	err = a.call("ax.snapshot", map[string]any{}, nil)
	if c, data := axData(t, err); c != desk.CodeSecure && c != desk.CodeNoLease {
		t.Fatalf("a snapshot on a locked screen: %v %v", err, data)
	}
	err = a.call("ax.snapshot", map[string]any{}, nil)
	if c, data := axData(t, err); c != desk.CodeNoLease || data["reason"] != desk.EndedLocked {
		t.Fatalf("after the lock: %v %v", err, data)
	}
}

func waitPaused(t *testing.T, h *harness) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		var st struct {
			Human desk.HumanState `json:"human"`
		}
		if err := h.host.Call(h.ctx, "lease.status", nil, &st); err == nil && st.Human.Paused {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("the agent was never paused")
}

func TestTheToolsOwnRefusalsBecomeTheContractsCodes(t *testing.T) {
	scene := fake.DefaultScene()
	trees := fake.DefaultTrees()
	name := func(s string) *string { return &s }
	tree := trees["10"]
	tree.Children = append(tree.Children,
		axfake.Node{Role: "button", Name: name("Stale"), AvailableActions: []string{"press"}, Error: "STALE_REF"},
		axfake.Node{Role: "button", Name: name("Twin"), AvailableActions: []string{"press"}, Error: "AMBIGUOUS_TARGET"},
		axfake.Node{Role: "textfield", Name: name("Terminal"), AvailableActions: []string{"focus"}, Error: "POLICY_DENIED"},
		axfake.Node{Role: "checkbox", Name: name("Stubborn"), AvailableActions: []string{"toggle"}, Error: "ACTION_FAILED",
			Delivery: "delivered_unverified"},
		axfake.Node{Role: "button", Name: name("Locked away"), AvailableActions: []string{"press"}, Error: "PERM_DENIED"},
		axfake.Node{Role: "button", Name: name("Slow"), AvailableActions: []string{"press"}, Error: "APP_UNRESPONSIVE",
			Delivery: "delivery_uncertain"},
		axfake.Node{Role: "checkbox", Name: name("Already"), States: []string{"checked"}, AvailableActions: []string{"toggle"},
			Delivery: "not_delivered"},
	)
	scene.Windows[0].AX = tree
	a := startAX(t, desk.Config{ActionsPerSecond: 1000, ActionBurst: 1000}, scene)
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	for _, c := range []struct {
		element, action string
		code            int
		reason          string
		delivery        string
	}{
		{"Stale", "click", desk.CodeStaleRef, "changed", desk.NotDelivered},
		{"Twin", "click", desk.CodeAmbiguous, "", desk.NotDelivered},
		{"Terminal", "type", wire.CodeUnsupported, "needs_pointer", desk.NotDelivered},
		{"Stubborn", "toggle", desk.CodeAXFailed, "contradicted", desk.DeliveryUnknown},
		{"Locked away", "click", desk.CodeNoPermission, "", desk.NotDelivered},
		{"Slow", "click", desk.CodeAXFailed, "unresponsive", desk.DeliveryUnknown},
	} {
		p := map[string]any{"ref": refOf(t, res.Tree, c.element), "action": c.action}
		if c.action == "type" {
			p["text"] = "ls"
		}
		err := a.call("ax.act", p, nil)
		got, data := axData(t, err)
		if got != c.code || (c.reason != "" && data["reason"] != c.reason) || data["delivery"] != c.delivery {
			t.Errorf("%s: %v %v", c.element, err, data)
		}
		if c.element == "Stubborn" && data["post_state"] == nil {
			t.Errorf("a contradicted action carries no read-back: %v", data)
		}
	}
	var act desk.AXActResult
	if err := a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Already"), "action": "check"}, &act); err != nil ||
		act.Delivery != desk.NotDelivered {
		t.Fatalf("a box already checked: %+v %v", act, err)
	}
}

func TestAMissingOrBrokenToolLeavesTheRestOfTheDesktopWorking(t *testing.T) {
	h := startScene(t, desk.Config{}, fake.DefaultScene(), func(d *desk.Desk, fk *fake.Fake, dir string) {
		d.SetAX(ax.Open(context.Background(), ax.Config{Path: filepath.Join(dir, "agent-desktop"), StateDir: filepath.Join(dir, "ax")}))
	})
	var info map[string]any
	if err := h.host.Call(h.ctx, "info", nil, &info); err != nil {
		t.Fatal(err)
	}
	if st, _ := info["ax"].(map[string]any); st["available"] != false || st["reason"] != ax.ReasonMissing {
		t.Fatalf("info.ax: %v", info["ax"])
	}
	h.grant(map[string]any{"kind": "window", "window": "10"})
	err := h.host.Call(h.ctx, "ax.snapshot", map[string]any{"session": session}, nil)
	if c, data := axData(t, err); c != desk.CodeAXUnavailable || data["reason"] != ax.ReasonMissing {
		t.Fatalf("a snapshot with no tool: %v %v", err, data)
	}
	s := h.shot(map[string]any{})
	if _, err := h.act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": 10, "y": 10}); err != nil {
		t.Fatalf("a click with no tool: %v", err)
	}

	// A tool that crashes on a call: that call is unavailable, the next one tries again.
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	a.setTweak(func(s *axfake.Scene) { s.Crash = true })
	err = a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Save"), "action": "click"}, nil)
	if c, data := axData(t, err); c != desk.CodeAXUnavailable || data["reason"] != ax.ReasonCrashed || data["delivery"] != desk.DeliveryUnknown {
		t.Fatalf("an action whose tool crashed: %v %v", err, data)
	}
	a.setTweak(nil)
	a.snapshot(map[string]any{})
}

func TestAStopKillsTheToolInTheMiddleOfAnAction(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{})
	a.setTweak(func(s *axfake.Scene) { s.HangMs = 20000 })
	done := make(chan error, 1)
	start := time.Now()
	go func() {
		done <- a.call("ax.act", map[string]any{"ref": refOf(t, res.Tree, "Save"), "action": "click"}, nil)
	}()
	time.Sleep(300 * time.Millisecond)
	if err := a.op.Call(a.ctx, "stop", map[string]any{}, nil); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		c, data := axData(t, err)
		if c != desk.CodeNoLease || data["delivery"] != desk.DeliveryUnknown {
			t.Fatalf("an action cut short by the stop: %v %v", err, data)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the stop did not end the tool")
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("the stopped action took %v", d)
	}
}

func TestTheToolNeverSeesARealInputFlagOrTheHostsTextAsOne(t *testing.T) {
	a := startAX(t, desk.Config{ActionsPerSecond: 1000, ActionBurst: 1000}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{"depth": "skeleton"})
	title := refOf(t, res.Tree, "Title")
	for _, p := range []map[string]any{
		{"ref": title, "action": "set_value", "text": "--headed"},
		{"ref": title, "action": "type", "text": "--force"},
	} {
		var act desk.AXActResult
		if err := a.call("ax.act", p, &act); err != nil {
			t.Fatalf("%v: %v", p, err)
		}
	}
	var found desk.AXFindResult
	if err := a.call("ax.find", map[string]any{"name": "--headed"}, &found); err != nil {
		t.Fatalf("a find for a name like a flag: %v", err)
	}
	in := a.fake.Inputs()
	if len(in) != 2 || in[0].Text != "--headed" || in[1].Text != "--force" {
		t.Fatalf("the texts did not arrive as text: %+v", in)
	}
	for _, run := range a.toolRuns() {
		if len(run.Env) != 1 || run.Env[0] != "AGENT_DESKTOP_HOME" || !strings.HasPrefix(run.Home, a.stateDir) {
			t.Fatalf("the child's environment: %+v", run)
		}
		for _, arg := range run.Args {
			if arg == "--" {
				break
			}
			if arg == "--headed" || arg == "--force" {
				t.Fatalf("a real-input flag reached the tool: %q", run.Args)
			}
		}
	}
}

func TestWindowsAreMatchedToTheToolsByProcessAndPlace(t *testing.T) {
	scene := fake.DefaultScene()
	name := func(s string) *string { return &s }
	twin := func(id string) fake.SceneWindow {
		return fake.SceneWindow{Window: backend.Window{ID: id, Title: "tab", Bounds: geom.Rect{X: 200, Y: 150, W: 600, H: 400},
			App: backend.App{ID: "com.example.tabs", Name: "Tabs", PID: 700, BundleID: "com.example.tabs"}},
			AX: &axfake.Node{Role: "window", Children: []axfake.Node{{Role: "button", Name: name("Go"), AvailableActions: []string{"press"}}}}}
	}
	plain := scene.Windows[2]
	plain.ID, plain.AX = "14", nil
	plain.App = backend.App{ID: "com.example.canvas", Name: "Canvas", PID: 701, BundleID: "com.example.canvas"}
	scene.Windows = append(scene.Windows, twin("20"), twin("21"), plain)
	a := startAX(t, desk.Config{}, scene)
	a.grant(map[string]any{"kind": "app", "app": "com.example.tabs"})
	err := a.call("ax.snapshot", map[string]any{"window": "20"}, nil)
	if c, data := axData(t, err); c != desk.CodeAmbiguous || data["window"] != "20" {
		t.Fatalf("two lookalike windows of one process: %v %v", err, data)
	}
	a.grant(map[string]any{"kind": "app", "app": "com.example.canvas"})
	err = a.call("ax.snapshot", map[string]any{"window": "14"}, nil)
	if c, data := axData(t, err); c != desk.CodeAXUnavailable || data["reason"] != ax.ReasonNoWindow {
		t.Fatalf("a window with no tree: %v %v", err, data)
	}
	// A surface that answers from a window the daemon cannot place is refused.
	a.grant(map[string]any{"kind": "window", "window": "10"})
	a.setTweak(func(s *axfake.Scene) {
		for i := range s.Windows {
			s.Windows[i].SurfaceWindow = "w-1-org-a11y-popup"
		}
	})
	err = a.call("ax.snapshot", map[string]any{"surface": "menu"}, nil)
	if c, data := axData(t, err); c != desk.CodeOutOfScope || data["reason"] != "surface_window" {
		t.Fatalf("a surface from an unknown window: %v %v", err, data)
	}
	// The match is kept while the window stays where it was, and made again when it moves.
	a.setTweak(nil)
	count := func() int {
		n := 0
		for _, r := range a.toolRuns() {
			if len(r.Args) > 0 && r.Args[0] == "list-windows" {
				n++
			}
		}
		return n
	}
	a.snapshot(map[string]any{})
	before := count()
	a.snapshot(map[string]any{})
	if count() != before {
		t.Fatal("the window was matched again though it did not move")
	}
	a.simulate(map[string]any{"move_window": "10", "dx": 50})
	a.snapshot(map[string]any{})
	if count() != before+1 {
		t.Fatal("a moved window was not matched again")
	}
}

func TestADrilledRegionAndAFindAddToTheirSnapshot(t *testing.T) {
	a := startAX(t, desk.Config{}, fake.DefaultScene())
	a.grant(map[string]any{"kind": "window", "window": "10"})
	res := a.snapshot(map[string]any{"depth": "skeleton"})
	var found desk.AXFindResult
	if err := a.call("ax.find", map[string]any{"role": "button", "name": "save", "limit": 5}, &found); err != nil {
		t.Fatal(err)
	}
	if len(found.Matches) != 1 || found.Matches[0].Ref == "" || found.Window != "10" {
		t.Fatalf("the find: %+v", found)
	}
	var act desk.AXActResult
	if err := a.call("ax.act", map[string]any{"ref": found.Matches[0].Ref, "action": "click"}, &act); err != nil {
		t.Fatalf("a found reference: %v", err)
	}
	var none desk.AXFindResult
	if err := a.call("ax.find", map[string]any{"role": "slider"}, &none); err != nil || len(none.Matches) != 0 || len(none.RolesPresent) == 0 {
		t.Fatalf("a role that is not there: %+v %v", none, err)
	}
	drill := a.snapshot(map[string]any{"root": refOf(t, res.Tree, "Title")})
	if drill.SnapshotID != res.SnapshotID {
		t.Fatalf("a drill is part of its snapshot: %s %s", drill.SnapshotID, res.SnapshotID)
	}
	if err := a.call("ax.snapshot", map[string]any{"root": refOf(t, res.Tree, "Title"), "window": "10"}, nil); code(err) != wire.CodeInvalidParams {
		t.Fatalf("a root with a window: %v", err)
	}
}
