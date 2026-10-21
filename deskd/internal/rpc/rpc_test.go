package rpc_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image/jpeg"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

const session = "s1"

type harness struct {
	t         *testing.T
	fake      *fake.Fake
	host, op  *clienttest.Client
	auditPath string
	ctx       context.Context
}

// start serves the fake desktop on a real socket, with the operator's token beside the host's, and
// Daedalus's own app on the protected list.
func start(t *testing.T, cfg desk.Config) *harness {
	t.Helper()
	dir, err := os.MkdirTemp("", "deskd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	listen := "unix"
	if runtime.GOOS == "windows" {
		listen = "tcp:127.0.0.1:0"
	}
	ep, err := server.Prepare(filepath.Join(dir, "run"), listen, "deskd")
	if err != nil {
		t.Fatal(err)
	}
	opToken, _ := server.NewToken()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	auditPath := filepath.Join(dir, "audit.jsonl")
	al, err := audit.Open(auditPath, 0)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Protected = append(cfg.Protected, desk.Matcher{BundleID: "com.example.daedalus"})
	fk := fake.New(fake.DefaultScene())
	evlog := events.NewLog(1024)
	d := desk.New(cfg, fk, evlog, al, log)
	daemon := &rpc.Daemon{Desk: d, Events: evlog, Log: log, Instance: "test", StartedAt: time.Now(), Fake: fk}
	srv := server.New(ep.Token, log, nil)
	srv.AddToken(opToken, rpc.RoleOperator)
	daemon.Register(srv)
	stop := make(chan struct{})
	go d.Run(stop)
	go srv.Serve(ep.Listener)
	host, err := clienttest.Dial(ep.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	network, address, _, _ := server.ReadEndpoint(ep.RunDir)
	op, err := clienttest.DialWith(network, address, opToken)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		host.Close()
		op.Close()
		close(stop)
		ep.Listener.Close()
		srv.Close()
		ep.Release()
		al.Close()
	})
	return &harness{t: t, fake: fk, host: host, op: op, auditPath: auditPath, ctx: context.Background()}
}

func code(err error) int {
	var we *wire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return 0
}

func (h *harness) grant(scope map[string]any, extra ...map[string]any) desk.Lease {
	h.t.Helper()
	p := map[string]any{"session": session, "scope": scope}
	for _, e := range extra {
		for k, v := range e {
			p[k] = v
		}
	}
	var res struct {
		Lease desk.Lease `json:"lease"`
	}
	if err := h.op.Call(h.ctx, "lease.grant", p, &res); err != nil {
		h.t.Fatalf("grant %v: %v", scope, err)
	}
	return res.Lease
}

func (h *harness) shot(p map[string]any) desk.ScreenshotResult {
	h.t.Helper()
	p["session"] = session
	var res desk.ScreenshotResult
	if err := h.host.Call(h.ctx, "screenshot", p, &res); err != nil {
		h.t.Fatalf("screenshot %v: %v", p, err)
	}
	return res
}

func (h *harness) act(p map[string]any) (desk.ActResult, error) {
	p["session"] = session
	var res desk.ActResult
	err := h.host.Call(h.ctx, "act", p, &res)
	return res, err
}

func (h *harness) simulate(s map[string]any) {
	h.t.Helper()
	if err := h.op.Call(h.ctx, "fake.simulate", s, nil); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) subscribe(c *clienttest.Client) {
	h.t.Helper()
	if err := c.Call(h.ctx, "events.subscribe", map[string]any{}, nil); err != nil {
		h.t.Fatal(err)
	}
}

func TestEachTokenHasItsOwnRole(t *testing.T) {
	h := start(t, desk.Config{})
	var hostHello, opHello map[string]any
	_ = json.Unmarshal(h.host.Hello, &hostHello)
	_ = json.Unmarshal(h.op.Hello, &opHello)
	if hostHello["role"] != "host" || opHello["role"] != "operator" || hostHello["protocol"] != 1.0 {
		t.Fatalf("hellos %v %v", hostHello, opHello)
	}
	// The host can never give itself the desktop, nor lift a pause, nor raise a system prompt.
	for method, params := range map[string]any{
		"lease.grant":         map[string]any{"session": session, "scope": map[string]any{"kind": "desktop"}},
		"resume":              map[string]any{},
		"permissions.request": map[string]any{"which": "screen"},
		"fake.simulate":       map[string]any{},
	} {
		if err := h.host.Call(h.ctx, method, params, nil); code(err) != desk.CodeOperatorOnly {
			t.Errorf("host %s: %v", method, err)
		}
	}
	// And the operator's token is not a way around the lease for the agent's actions.
	for _, method := range []string{"screenshot", "act", "cursor", "apps.focus"} {
		if err := h.op.Call(h.ctx, method, map[string]any{"session": session}, nil); code(err) != desk.CodeOperatorOnly {
			t.Errorf("operator %s: %v", method, err)
		}
	}
	var info map[string]any
	if err := h.host.Call(h.ctx, "info", nil, &info); err != nil || info["backend"] != "fake" {
		t.Fatalf("info %v %v", info, err)
	}
}

func TestNothingWithoutALease(t *testing.T) {
	h := start(t, desk.Config{})
	err := h.host.Call(h.ctx, "screenshot", map[string]any{"session": session}, nil)
	if code(err) != desk.CodeNoLease {
		t.Fatalf("screenshot without a lease: %v", err)
	}
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); code(err) != desk.CodeNoLease {
		t.Fatalf("wait without a lease: %v", err)
	}
	h.grant(map[string]any{"kind": "desktop"})
	err = h.host.Call(h.ctx, "screenshot", map[string]any{"session": "someone-else"}, nil)
	var we *wire.Error
	if !errors.As(err, &we) || we.Code != desk.CodeNoLease || !strings.Contains(string(mustJSON(we.Data)), "other_session") {
		t.Fatalf("another session: %v", err)
	}
	if len(h.fake.Inputs()) != 0 {
		t.Fatal("input reached the desktop")
	}
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}

func TestAWindowScreenshotMapsBackToTheDesktop(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "window", "window": "10"})
	// The editor's window is 800×600 points on a Retina display: 1600×1200 pixels, fitted to 800.
	s := h.shot(map[string]any{"max_edge": 800})
	if s.Width != 800 || s.Height != 600 || s.Window != "10" || s.Display != "1" || s.PixelsPerUnit != 2 ||
		s.Region != (geom.Rect{X: 100, Y: 100, W: 800, H: 600}) || s.UnitsPerPixel != [2]float64{1, 1} {
		t.Fatalf("geometry %+v", s)
	}
	raw, _ := base64.StdEncoding.DecodeString(s.Data)
	img, err := jpeg.Decode(strings.NewReader(string(raw)))
	if err != nil {
		t.Fatal(err)
	}
	if r, g, _, _ := img.At(400, 300).RGBA(); r>>8 < 200 || g>>8 > 60 {
		t.Errorf("the editor is not red in its own screenshot")
	}
	res, err := h.act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": 400, "y": 300})
	if err != nil {
		t.Fatal(err)
	}
	if res.Target == nil || res.Target.Window != "10" || res.Target.App.BundleID != "com.example.editor" || res.ActionsLeft != 199 {
		t.Errorf("result %+v", res)
	}
	in := h.fake.Inputs()
	want := []fake.Input{{Kind: "move", X: 500.5, Y: 400.5}, {Kind: "button", Button: "left", Down: true, X: 500.5, Y: 400.5, Window: "10"},
		{Kind: "button", Button: "left", X: 500.5, Y: 400.5, Window: "10"}}
	if len(in) != 3 || in[0] != want[0] || in[1] != want[1] || in[2] != want[2] {
		t.Fatalf("inputs %+v", in)
	}
	var cur desk.CursorResult
	if err := h.host.Call(h.ctx, "cursor", map[string]any{"session": session, "screenshot_id": s.ID}, &cur); err != nil ||
		cur.InScreenshot == nil || *cur.InScreenshot != [2]int{400, 300} {
		t.Fatalf("cursor %+v %v", cur, err)
	}
}

func TestADisplayLeaseKeepsToItsDisplayAndAwayFromDaedalus(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "display", "display": "1"})
	s := h.shot(map[string]any{})
	if s.Width != 1568 || s.Height != 980 {
		t.Fatalf("a 2880×1800 display fitted to %d×%d", s.Width, s.Height)
	}
	px := func(x, y float64) map[string]any {
		f := geom.Frame{Region: s.Region, Width: s.Width, Height: s.Height}
		a, b, _ := f.FromDesktop(x, y)
		return map[string]any{"action": "click", "screenshot_id": s.ID, "x": a, "y": b}
	}
	// Daedalus's own window: the agent must never press its approval buttons.
	if _, err := h.act(px(1100, 400)); code(err) != desk.CodeProtected {
		t.Fatalf("a click on Daedalus: %v", err)
	}
	if _, err := h.act(px(300, 300)); err != nil {
		t.Fatalf("a click on the editor: %v", err)
	}
	if err := h.host.Call(h.ctx, "screenshot", map[string]any{"session": session, "display": "2"}, nil); code(err) != desk.CodeOutOfScope {
		t.Fatalf("the other display: %v", err)
	}
	in := h.fake.Inputs()
	for _, e := range in {
		if e.Window == "11" {
			t.Fatalf("input reached Daedalus: %+v", in)
		}
	}
	// A grant cannot be scoped to Daedalus either.
	err := h.op.Call(h.ctx, "lease.grant", map[string]any{"session": session, "scope": map[string]any{"kind": "window", "window": "11"}}, nil)
	if code(err) != desk.CodeProtected {
		t.Fatalf("a lease on Daedalus: %v", err)
	}
}

func TestAnElevatedWindowIsReportedUnreachable(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "desktop"})
	s := h.shot(map[string]any{"display": "2"}) // 1920×1080 left of the primary, fitted to 1568
	f := geom.Frame{Region: s.Region, Width: s.Width, Height: s.Height}
	x, y, _ := f.FromDesktop(-450, 500)
	if _, err := h.act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": x, "y": y}); code(err) != desk.CodeUnreachable {
		t.Fatalf("a click on an elevated window: %v", err)
	}
	var apps desk.AppsResult
	if err := h.host.Call(h.ctx, "apps.list", map[string]any{"session": session}, &apps); err != nil {
		t.Fatal(err)
	}
	for _, a := range apps.Apps {
		if a.BundleID == "com.example.daedalus" {
			t.Error("the host was shown Daedalus")
		}
		if a.ID == "pid:504" && a.Reachable {
			t.Error("the elevated app was listed reachable")
		}
	}
	if err := h.op.Call(h.ctx, "apps.list", nil, &apps); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, a := range apps.Apps {
		found = found || (a.BundleID == "com.example.daedalus" && a.Protected)
	}
	if !found {
		t.Error("the operator was not shown Daedalus as protected")
	}
}

func TestScreenshotIdsGoStale(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "app", "app": "com.example.editor"})
	click := func(id string) error {
		_, err := h.act(map[string]any{"action": "click", "screenshot_id": id, "x": 10, "y": 10})
		return err
	}
	reason := func(err error) string {
		var we *wire.Error
		if !errors.As(err, &we) || we.Code != desk.CodeStale {
			return "not stale: " + err.Error()
		}
		return we.Data.(map[string]any)["reason"].(string)
	}
	if r := reason(click("s-nonsense")); r != "unknown" {
		t.Errorf("an unknown id: %s", r)
	}
	s := h.shot(map[string]any{"window": "10"})
	h.simulate(map[string]any{"move_window": "10", "dx": 5})
	if r := reason(click(s.ID)); r != "window_moved" {
		t.Errorf("after the window moved: %s", r)
	}
	s = h.shot(map[string]any{"window": "10"})
	h.simulate(map[string]any{"displays": []geom.Display{{ID: "1", Bounds: geom.Rect{W: 1280, H: 800}, Primary: true, PixelsPerUnit: 2, Scale: 2}}})
	if r := reason(click(s.ID)); r != "displays_changed" {
		t.Errorf("after the displays changed: %s", r)
	}
	s = h.shot(map[string]any{"window": "10"})
	h.grant(map[string]any{"kind": "app", "app": "com.example.editor"})
	if r := reason(click(s.ID)); r != "unknown" {
		t.Errorf("under a new lease: %s", r)
	}
	// A pixel without its picture, and a picture without its pixel, are refused before anything.
	if _, err := h.act(map[string]any{"action": "click", "x": 1, "y": 1}); code(err) != wire.CodeInvalidParams {
		t.Errorf("a point with no screenshot id: %v", err)
	}
	if _, err := h.act(map[string]any{"action": "click", "x": 1}); code(err) != wire.CodeInvalidParams {
		t.Errorf("x without y: %v", err)
	}
	if err := h.host.Call(h.ctx, "act", map[string]any{"session": session, "action": "wait", "ms": 1, "colour": "red"}, nil); code(err) != wire.CodeInvalidParams {
		t.Errorf("an unknown parameter: %v", err)
	}
	if len(h.fake.Inputs()) != 0 {
		t.Fatal("a refused action reached the desktop")
	}
}

func TestTypingAndKeysGoOnlyToTheWindowWithTheKeyboard(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "window", "window": "10"})
	if _, err := h.act(map[string]any{"action": "type", "text": "héllo\nwörld"}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.act(map[string]any{"action": "key", "keys": "ctrl+s"}); err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range h.fake.Inputs() {
		switch e.Kind {
		case "type":
			got = append(got, "type:"+e.Text)
		case "key":
			got = append(got, map[bool]string{true: "down:", false: "up:"}[e.Down]+e.Key)
		}
	}
	want := "type:héllo down:enter up:enter type:wörld down:ctrl down:s up:s up:ctrl"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %s", strings.Join(got, " "))
	}
	// The keyboard moves to Daedalus: no key may follow it there.
	h.simulate(map[string]any{"focus": "11"})
	if _, err := h.act(map[string]any{"action": "type", "text": "allow"}); code(err) != desk.CodeProtected {
		t.Fatalf("typing into Daedalus: %v", err)
	}
	h.simulate(map[string]any{"focus": "12"})
	if _, err := h.act(map[string]any{"action": "key", "keys": "enter"}); code(err) != desk.CodeOutOfScope {
		t.Fatalf("a key to a window outside the lease: %v", err)
	}
	if len(h.fake.Inputs()) != 0 {
		t.Fatal("a refused key reached the desktop")
	}
	// The audit has the length of what was typed, never the text, and no printable key.
	b, _ := os.ReadFile(h.auditPath)
	log := string(b)
	if strings.Contains(log, "llo") || strings.Contains(log, "allow") || !strings.Contains(log, `"text_length":11`) ||
		!strings.Contains(log, `"keys":["ctrl+s"]`) {
		t.Fatalf("audit:\n%s", log)
	}
}

func TestTheRateAndTheLeaseLimitActions(t *testing.T) {
	h := start(t, desk.Config{ActionsPerSecond: 1, ActionBurst: 2})
	h.grant(map[string]any{"kind": "window", "window": "10"})
	s := h.shot(map[string]any{})
	move := func() error {
		_, err := h.act(map[string]any{"action": "move", "screenshot_id": s.ID, "x": 5, "y": 5})
		return err
	}
	if move() != nil || move() != nil {
		t.Fatal("the burst was refused")
	}
	err := move()
	var we *wire.Error
	if !errors.As(err, &we) || we.Code != desk.CodeRate || we.Data.(map[string]any)["limit"] != "per_second" {
		t.Fatalf("past the rate: %v", err)
	}
	h.grant(map[string]any{"kind": "window", "window": "10"}, map[string]any{"max_actions": 1})
	s = h.shot(map[string]any{})
	if err := move(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1100 * time.Millisecond)
	if err := move(); !errors.As(err, &we) || we.Code != desk.CodeRate || we.Data.(map[string]any)["limit"] != "lease_actions" {
		t.Fatalf("past the lease's actions: %v", err)
	}
}

func TestTheOperatorsHandPausesTheAgent(t *testing.T) {
	quiet := int64(300)
	h := start(t, desk.Config{HumanQuietMs: &quiet})
	h.subscribe(h.op)
	h.grant(map[string]any{"kind": "window", "window": "10"})
	h.simulate(map[string]any{"human_input": true, "dx": 40, "dy": 0})
	if _, err := h.op.WaitEvent("human.input", "", 3*time.Second); err != nil {
		t.Fatal(err)
	}
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); code(err) != desk.CodeHumanDriving {
		t.Fatalf("while the operator drives: %v", err)
	}
	// Quiet for the configured time: the agent may go on.
	if _, err := h.op.WaitEvent("human.resumed", "", 3*time.Second); err != nil {
		t.Fatal(err)
	}
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); err != nil {
		t.Fatalf("after the quiet period: %v", err)
	}
}

func TestTheOperatorsHandCutsALongWaitShort(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "window", "window": "10"})
	done := make(chan error, 1)
	began := time.Now()
	go func() {
		_, err := h.act(map[string]any{"action": "wait", "ms": 5000})
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	h.simulate(map[string]any{"human_input": true, "dx": 30})
	select {
	case err := <-done:
		if code(err) != desk.CodeHumanDriving || time.Since(began) > 2*time.Second {
			t.Fatalf("the wait ended with %v after %s", err, time.Since(began))
		}
	case <-time.After(4 * time.Second):
		t.Fatal("the wait went on under the operator's hand")
	}
}

func TestOnlyTheOperatorResumesWhenThereIsNoQuietPeriod(t *testing.T) {
	quiet := int64(0)
	h := start(t, desk.Config{HumanQuietMs: &quiet})
	h.subscribe(h.op)
	h.grant(map[string]any{"kind": "window", "window": "10"})
	// A key the operator presses, with the pointer left alone: only the system's last-input time
	// shows it, and only once it is later than the daemon's own last input by more than the grace.
	time.Sleep(400 * time.Millisecond)
	h.simulate(map[string]any{"human_input": true})
	if _, err := h.op.WaitEvent("human.input", "", 3*time.Second); err != nil {
		t.Fatal(err)
	}
	time.Sleep(500 * time.Millisecond)
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); code(err) != desk.CodeHumanDriving {
		t.Fatalf("still paused: %v", err)
	}
	var res map[string]bool
	if err := h.op.Call(h.ctx, "resume", nil, &res); err != nil || !res["resumed"] {
		t.Fatalf("resume %v %v", res, err)
	}
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); err != nil {
		t.Fatalf("after resume: %v", err)
	}
}

func TestStopCutsAnActionShortAndLetsGoOfTheButton(t *testing.T) {
	h := start(t, desk.Config{})
	h.subscribe(h.op)
	h.fake.InputDelay = 30 * time.Millisecond
	h.grant(map[string]any{"kind": "window", "window": "10"})
	s := h.shot(map[string]any{})
	done := make(chan error, 1)
	go func() {
		_, err := h.act(map[string]any{"action": "drag", "screenshot_id": s.ID,
			"path": []map[string]int{{"x": 10, "y": 10}, {"x": 700, "y": 500}}})
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	if err := h.op.Call(h.ctx, "stop", nil, nil); err != nil {
		t.Fatal(err)
	}
	var err error
	select {
	case err = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the drag did not stop")
	}
	if code(err) != desk.CodeNoLease {
		t.Fatalf("the stopped drag: %v", err)
	}
	in := h.fake.Inputs()
	last := in[len(in)-1]
	if last.Kind != "button" || last.Down || len(in) > 40 {
		t.Fatalf("after the stop: %d inputs, last %+v", len(in), last)
	}
	e, err := h.op.WaitEvent("lease.ended", "", time.Second)
	if err != nil || !strings.Contains(string(e.Data), `"reason":"stopped"`) {
		t.Fatalf("lease.ended %s %v", e.Data, err)
	}
	if _, err := h.op.WaitEvent("stopped", "", time.Second); err != nil {
		t.Fatal(err)
	}
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); code(err) != desk.CodeNoLease {
		t.Fatalf("after the stop: %v", err)
	}
}

func TestAnExpiredLeaseEndsAndAHostMayGiveItsOwnBack(t *testing.T) {
	h := start(t, desk.Config{})
	h.subscribe(h.host)
	h.grant(map[string]any{"kind": "desktop"}, map[string]any{"ttl_ms": 1000})
	e, err := h.host.WaitEvent("lease.ended", "", 3*time.Second)
	if err != nil || !strings.Contains(string(e.Data), `"reason":"expired"`) {
		t.Fatalf("expiry %s %v", e.Data, err)
	}
	var we *wire.Error
	if _, err := h.act(map[string]any{"action": "wait", "ms": 1}); !errors.As(err, &we) || we.Data.(map[string]any)["reason"] != "expired" {
		t.Fatalf("after expiry: %v", err)
	}
	h.grant(map[string]any{"kind": "desktop"})
	if err := h.host.Call(h.ctx, "lease.revoke", map[string]any{}, nil); code(err) != wire.CodeInvalidParams {
		t.Fatalf("a revoke naming no session: %v", err)
	}
	var res map[string]bool
	if err := h.host.Call(h.ctx, "lease.revoke", map[string]any{"session": session}, &res); err != nil || !res["revoked"] {
		t.Fatalf("revoke %v %v", res, err)
	}
	var st struct {
		Lease desk.LeaseStatus `json:"lease"`
	}
	if err := h.host.Call(h.ctx, "lease.status", nil, &st); err != nil || st.Lease.Active || st.Lease.LastEnded != "revoked" {
		t.Fatalf("status %+v %v", st, err)
	}
}

func TestALockedScreenAndAMissingPermissionAreSaidSo(t *testing.T) {
	h := start(t, desk.Config{})
	h.subscribe(h.op)
	h.grant(map[string]any{"kind": "desktop"})
	h.simulate(map[string]any{"secure": backend.Secure{Locked: true, Reason: "lock_screen"}})
	if err := h.host.Call(h.ctx, "screenshot", map[string]any{"session": session}, nil); code(err) != desk.CodeSecure {
		t.Fatalf("a locked screen: %v", err)
	}
	if _, err := h.act(map[string]any{"action": "key", "keys": "enter"}); code(err) != desk.CodeSecure {
		t.Fatalf("a key on a locked screen: %v", err)
	}
	h.simulate(map[string]any{"secure": backend.Secure{}, "permissions": backend.Permissions{Screen: backend.Denied,
		Accessibility: backend.Granted, InputMonitoring: backend.NotApplicable}})
	err := h.host.Call(h.ctx, "screenshot", map[string]any{"session": session}, nil)
	var we *wire.Error
	if !errors.As(err, &we) || we.Code != desk.CodeNoPermission || we.Data.(map[string]any)["which"] != "screen" ||
		!strings.Contains(we.Data.(map[string]any)["hint"].(string), "remove Daedalus from the list") {
		t.Fatalf("no screen permission: %v %v", err, we)
	}
	if _, err := h.op.WaitEvent("permissions.changed", "", 4*time.Second); err != nil {
		t.Fatal(err)
	}
	var res struct {
		State string `json:"state"`
	}
	if err := h.op.Call(h.ctx, "permissions.request", map[string]any{"which": "screen"}, &res); err != nil || res.State != "granted" {
		t.Fatalf("request %+v %v", res, err)
	}
	h.shot(map[string]any{})
}

func TestFocusAndAppsFollowTheScope(t *testing.T) {
	h := start(t, desk.Config{})
	h.grant(map[string]any{"kind": "app", "app": "com.example.editor"})
	if err := h.host.Call(h.ctx, "apps.focus", map[string]any{"session": session, "app": "com.example.terminal"}, nil); code(err) != desk.CodeOutOfScope {
		t.Fatalf("focusing an app outside the lease: %v", err)
	}
	if err := h.host.Call(h.ctx, "apps.focus", map[string]any{"session": session, "window": "11"}, nil); code(err) != desk.CodeProtected {
		t.Fatalf("focusing Daedalus: %v", err)
	}
	h.simulate(map[string]any{"focus": "12"})
	if err := h.host.Call(h.ctx, "apps.focus", map[string]any{"session": session, "app": "com.example.editor"}, nil); err != nil {
		t.Fatal(err)
	}
	var apps desk.AppsResult
	if err := h.host.Call(h.ctx, "apps.list", map[string]any{"session": session}, &apps); err != nil {
		t.Fatal(err)
	}
	if apps.Foreground == nil || apps.Foreground.ID != "10" || !apps.Foreground.InScope || apps.Apps[0].ID != "com.example.editor" {
		t.Fatalf("after focus: %+v", apps)
	}
}

func TestTheOperatorPreviewsWithoutALease(t *testing.T) {
	h := start(t, desk.Config{})
	var res desk.PreviewResult
	// No lease, no display named: the primary display, a 2880×1800 picture fitted to 480.
	if err := h.op.Call(h.ctx, "preview", map[string]any{}, &res); err != nil {
		t.Fatal(err)
	}
	if res.Display != "1" || res.Width != 480 || res.Height != 300 || res.Window != "" {
		t.Fatalf("display preview %+v", res)
	}
	raw, err := base64.StdEncoding.DecodeString(res.Image)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := jpeg.Decode(strings.NewReader(string(raw))); err != nil {
		t.Fatalf("not a JPEG: %v", err)
	}
	// The terminal's 1000×700 window on the 1080p display, at its own edge.
	if err := h.op.Call(h.ctx, "preview", map[string]any{"window": "12", "max_edge": 200}, &res); err != nil {
		t.Fatal(err)
	}
	if res.Display != "2" || res.Window != "12" || res.Width != 200 || res.Height != 140 {
		t.Fatalf("window preview %+v", res)
	}
	raw, _ = base64.StdEncoding.DecodeString(res.Image)
	img, err := jpeg.Decode(strings.NewReader(string(raw)))
	if err != nil {
		t.Fatal(err)
	}
	if r, g, _, _ := img.At(100, 70).RGBA(); g>>8 < 150 || r>>8 > 60 {
		t.Errorf("the terminal is not green in its own preview")
	}
	// The edge is capped, not refused: a picker asking for more gets the largest preview there is.
	if err := h.op.Call(h.ctx, "preview", map[string]any{"display": "2", "max_edge": 5000}, &res); err != nil || res.Width != desk.MaxPreviewEdge {
		t.Fatalf("capped preview %+v %v", res, err)
	}
	// Daedalus's own window is never pictured on its own.
	if err := h.op.Call(h.ctx, "preview", map[string]any{"window": "11"}, nil); code(err) != desk.CodeProtected {
		t.Fatalf("previewing Daedalus: %v", err)
	}
	for _, params := range []map[string]any{{"display": "9"}, {"window": "99"}} {
		if err := h.op.Call(h.ctx, "preview", params, nil); code(err) != wire.CodeNotFound {
			t.Errorf("preview %v: %v", params, err)
		}
	}
	if err := h.op.Call(h.ctx, "preview", map[string]any{"display": "1", "window": "10"}, nil); code(err) != wire.CodeInvalidParams {
		t.Errorf("both a display and a window: %v", err)
	}
	if err := h.op.Call(h.ctx, "preview", map[string]any{"max_edge": 10}, nil); code(err) != wire.CodeInvalidParams {
		t.Errorf("a tiny edge: %v", err)
	}
	// The host sees the screen only through a lease's screenshots.
	if err := h.host.Call(h.ctx, "preview", map[string]any{}, nil); code(err) != desk.CodeOperatorOnly {
		t.Errorf("host preview: %v", err)
	}
	h.simulate(map[string]any{"secure": backend.Secure{Locked: true, Reason: "lock_screen"}})
	if err := h.op.Call(h.ctx, "preview", map[string]any{}, nil); code(err) != desk.CodeSecure {
		t.Errorf("a preview of the lock screen: %v", err)
	}
}
