// Package selftest is `deskd selftest`: the daemon checks itself on the machine it runs on, end to
// end through its own socket, against a small window it opens and nothing else. The operator's lease
// is scoped to that one window, so the daemon's own rules refuse any point outside it; typing waits
// until that window has the keyboard. It changes nothing on the machine and leaves nothing behind.
package selftest

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"image"
	"image/png"
	"io"
	"log/slog"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/native"
	"github.com/ascorblack/daedalus/deskd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// prepare readies a display to test on and says which; nil needs nothing. openBackend opens the
// desktop the test window is on.
var (
	prepare     func() (cleanup func(), note string, err error)
	openBackend = native.Open
)

// The test window's content size, and the long edge the screenshots are asked for: smaller than the
// window, so every check goes through the downscale and the coordinate mapping as a model's would.
const (
	windowW, windowH = 400, 300
	testMaxEdge      = 240
	session          = "selftest"
	eventWait        = 2 * time.Second
)

// Main runs the selftest, prints its report to out and returns the exit code: 0 when every check
// passed. On macOS the test window needs the main thread, so the checks run beside it.
func Main(out io.Writer, verbose bool) int {
	if mainLoop != nil {
		result := make(chan int, 1)
		stop := make(chan struct{})
		go func() {
			result <- Run(context.Background(), out, verbose)
			close(stop)
		}()
		mainLoop(stop)
		return <-result
	}
	return Run(context.Background(), out, verbose)
}

type report struct {
	out    io.Writer
	passed int
	failed int
}

func (r *report) check(name string, err error, detail string) bool {
	if err != nil {
		r.failed++
		fmt.Fprintf(r.out, "FAIL  %-12s %v\n", name, err)
		return false
	}
	r.passed++
	fmt.Fprintf(r.out, "PASS  %-12s %s\n", name, detail)
	return true
}

func (r *report) note(format string, args ...any) { fmt.Fprintf(r.out, "      "+format+"\n", args...) }

// daemon is an in-process daemon on a temporary run directory, reached through its socket.
type daemon struct {
	host, operator *clienttest.Client
	stop           func()
}

func startDaemon(be backend.Backend, verbose bool) (*daemon, error) {
	dir, err := os.MkdirTemp("", "deskd-selftest")
	if err != nil {
		return nil, err
	}
	runDir := filepath.Join(dir, "run")
	var logOut io.Writer = io.Discard
	if verbose {
		logOut = os.Stderr
	}
	log := slog.New(slog.NewTextHandler(logOut, nil))
	ep, err := server.Prepare(runDir, defaultListen(), "deskd")
	if err != nil {
		os.RemoveAll(dir)
		return nil, err
	}
	opToken, err := server.NewToken()
	if err != nil {
		ep.Release()
		os.RemoveAll(dir)
		return nil, err
	}
	evlog := events.NewLog(1024)
	// No quiet period: a selftest paused by the operator's hand reports it rather than carrying on.
	quiet := int64(0)
	d := desk.New(desk.Config{HumanQuietMs: &quiet, ActionsPerSecond: 50, ActionBurst: 50}, be, evlog, nil, log)
	r := &rpc.Daemon{Desk: d, Events: evlog, Log: log, Instance: "selftest", StartedAt: time.Now()}
	srv := server.New(ep.Token, log, nil)
	srv.AddToken(opToken, rpc.RoleOperator)
	r.Register(srv)
	stopRun := make(chan struct{})
	go d.Run(stopRun)
	go srv.Serve(ep.Listener)
	cleanup := func() {
		close(stopRun)
		ep.Listener.Close()
		srv.Close()
		ep.Release()
		os.RemoveAll(dir)
	}
	host, err := clienttest.Dial(runDir)
	if err != nil {
		cleanup()
		return nil, err
	}
	network, address, _, err := server.ReadEndpoint(runDir)
	if err != nil {
		cleanup()
		return nil, err
	}
	op, err := clienttest.DialWith(network, address, opToken)
	if err != nil {
		host.Close()
		cleanup()
		return nil, err
	}
	return &daemon{host: host, operator: op, stop: func() {
		host.Close()
		op.Close()
		cleanup()
	}}, nil
}

func defaultListen() string {
	if runtime.GOOS == "windows" {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}

func codeOf(err error) int {
	var we *wire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return 0
}

type shotResult struct {
	desk.ScreenshotResult
	img   image.Image
	frame geom.Frame
}

func (d *daemon) screenshot(ctx context.Context, window string) (shotResult, error) {
	var s shotResult
	err := d.host.Call(ctx, "screenshot", map[string]any{"session": session, "window": window, "max_edge": testMaxEdge,
		"format": "png"}, &s.ScreenshotResult)
	if err != nil {
		return s, err
	}
	raw, err := base64.StdEncoding.DecodeString(s.Data)
	if err != nil {
		return s, err
	}
	if s.img, err = png.Decode(bytes.NewReader(raw)); err != nil {
		return s, err
	}
	s.frame = geom.Frame{Region: s.Region, Width: s.Width, Height: s.Height}
	return s, nil
}

// quadrantCentre is the centre of quadrant q of the content, in desktop units.
func quadrantCentre(c geom.Rect, q int) (float64, float64) {
	x, y := c.X+c.W/4, c.Y+c.H/4
	if q == 1 || q == 3 {
		x += c.W / 2
	}
	if q >= 2 {
		y += c.H / 2
	}
	return x, y
}

func near(a, b [3]uint8) bool {
	for i := range a {
		if math.Abs(float64(a[i])-float64(b[i])) > 40 {
			return false
		}
	}
	return true
}

func colourAt(img image.Image, x, y int) [3]uint8 {
	r, g, b, _ := img.At(x, y).RGBA()
	return [3]uint8{uint8(r >> 8), uint8(g >> 8), uint8(b >> 8)}
}

// waitFor collects the window's events until done says they are enough, or the wait runs out.
func waitFor(w Window, done func([]Event) bool) []Event {
	var got []Event
	deadline := time.Now().Add(eventWait)
	for {
		got = append(got, w.Events()...)
		if done(got) || time.Now().After(deadline) {
			return got
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func kinds(evs []Event, kind string) []Event {
	var out []Event
	for _, e := range evs {
		if e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

// Run is the selftest. It returns the exit code.
func Run(ctx context.Context, out io.Writer, verbose bool) int {
	r := &report{out: out}
	fmt.Fprintln(out, "deskd selftest")
	if opener == nil {
		fmt.Fprintln(out, "FAIL  this build cannot open a test window on this system")
		return 1
	}
	note := "the current desktop"
	if prepare != nil {
		cleanup, n, err := prepare()
		if err != nil {
			fmt.Fprintf(out, "FAIL  display      %v\n", err)
			return 1
		}
		defer cleanup()
		note = n
	}
	be, err := openBackend(ctx)
	if err != nil {
		fmt.Fprintf(out, "FAIL  backend      %v\n", err)
		return 1
	}
	defer be.Close()
	p := be.Platform()
	r.note("%s (%s, units: %s), on %s", p.SessionType, p.OS, p.Units, note)

	d, err := startDaemon(be, verbose)
	if err != nil {
		fmt.Fprintf(out, "FAIL  daemon       %v\n", err)
		return 1
	}
	defer d.stop()

	var info struct {
		Displays    []geom.Display      `json:"displays"`
		Permissions backend.Permissions `json:"permissions"`
	}
	if !r.check("info", d.operator.Call(ctx, "info", nil, &info), fmt.Sprintf("%d display(s)", len(info.Displays))) {
		return 1
	}
	for _, disp := range info.Displays {
		b := disp.Bounds
		r.note("display %s: %g×%g at (%g, %g), %g pixels a unit, scale %g%s", disp.ID, b.W, b.H, b.X, b.Y,
			disp.PixelsPerUnit, disp.Scale, map[bool]string{true: ", primary"}[disp.Primary])
	}
	perms := info.Permissions
	r.note("permissions: screen %s, accessibility %s, input monitoring %s", perms.Screen, perms.Accessibility, perms.InputMonitoring)
	missing := false
	for _, which := range []string{backend.PermScreen, backend.PermAccessibility} {
		if perms.Get(which) == backend.Denied {
			missing = true
			r.check("permission", fmt.Errorf("%s is not granted: %s", which, desk.PermissionHint(which)), "")
		}
	}
	if missing {
		fmt.Fprintf(out, "%d passed, %d failed: grant the permissions and run the selftest again\n", r.passed, r.failed)
		return 1
	}

	w, err := opener(ctx, "deskd selftest", windowW, windowH)
	if !r.check("window", err, fmt.Sprintf("opened its own %d×%d window", windowW, windowH)) {
		return 1
	}
	defer w.Close()
	// The window must be on the daemon's list before a lease can name it.
	var listed bool
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
		var apps desk.AppsResult
		if d.operator.Call(ctx, "apps.list", nil, &apps) == nil {
			for _, a := range apps.Apps {
				for _, aw := range a.Windows {
					listed = listed || aw.ID == w.ID()
				}
			}
		}
		if listed {
			break
		}
	}
	if !r.check("listed", map[bool]error{false: errors.New("the daemon never listed the test window")}[listed], "the daemon sees the window") {
		return 1
	}
	var grant struct {
		Lease desk.Lease `json:"lease"`
	}
	err = d.operator.Call(ctx, "lease.grant", map[string]any{"session": session, "ttl_ms": 120000,
		"scope": map[string]any{"kind": "window", "window": w.ID()}}, &grant)
	if !r.check("lease", err, "granted for this window only") {
		return 1
	}
	checks(ctx, r, d, w, info.Displays[0].ID)
	fmt.Fprintf(out, "%d passed, %d failed\n", r.passed, r.failed)
	if r.failed > 0 {
		return 1
	}
	return 0
}

// checks drives the window through every action and the refusals.
func checks(ctx context.Context, r *report, d *daemon, w Window, display string) {
	content, err := w.Content()
	if !r.check("content", err, fmt.Sprintf("%g×%g at (%g, %g)", content.W, content.H, content.X, content.Y)) {
		return
	}
	s, err := d.screenshot(ctx, w.ID())
	if !r.check("screenshot", err, fmt.Sprintf("%d×%d of the window, %.2f units a pixel", s.Width, s.Height, s.UnitsPerPixel[0])) {
		return
	}
	// Each quadrant's colour must be where the screenshot's own geometry says the quadrant is.
	var bad []string
	for q, want := range quadrantColors {
		x, y := quadrantCentre(content, q)
		px, py, in := s.frame.FromDesktop(x, y)
		if !in {
			bad = append(bad, fmt.Sprintf("quadrant %d is off the picture", q))
			continue
		}
		if got := colourAt(s.img, px, py); !near(got, want) {
			bad = append(bad, fmt.Sprintf("quadrant %d at (%d, %d) is %v, want %v", q, px, py, got, want))
		}
	}
	r.check("geometry", joinErr(bad), "each colour is where the screenshot's coordinates put it")

	// pixel is the screenshot pixel of a quadrant's centre; local is where a point of the picture is
	// in the window's content, for comparing with what the window received.
	pixel := func(q int) (int, int) {
		x, y := quadrantCentre(content, q)
		px, py, _ := s.frame.FromDesktop(x, y)
		return px, py
	}
	upp, _ := s.frame.UnitsPerPixel()
	tolerance := upp + 2
	act := func(params map[string]any) error {
		params["session"] = session
		return d.host.Call(ctx, "act", params, nil)
	}
	clickCheck := func(name string, q int, button string, want int, count int) {
		px, py := pixel(q)
		w.Events()
		err := act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": px, "y": py, "button": button, "count": count})
		if err != nil {
			r.check(name, err, "")
			return
		}
		evs := waitFor(w, func(e []Event) bool { return len(kinds(e, "release")) >= count })
		presses := kinds(evs, "press")
		if len(presses) != count {
			r.check(name, fmt.Errorf("the window got %d presses, want %d", len(presses), count), "")
			return
		}
		ex, ey := quadrantCentre(content, q)
		ex, ey = ex-content.X, ey-content.Y
		for _, p := range presses {
			if p.Button != want || math.Abs(p.X-ex) > tolerance || math.Abs(p.Y-ey) > tolerance {
				r.check(name, fmt.Errorf("button %d at (%.0f, %.0f), want button %d at (%.0f, %.0f) ± %.1f", p.Button, p.X, p.Y,
					want, ex, ey, tolerance), "")
				return
			}
		}
		r.check(name, nil, fmt.Sprintf("button %d ×%d landed at (%.0f, %.0f) in the window", want, count, presses[0].X, presses[0].Y))
	}
	clickCheck("click", 0, "left", 1, 1)
	clickCheck("right", 1, "right", 3, 1)
	clickCheck("middle", 2, "middle", 2, 1)
	clickCheck("double", 3, "left", 1, 2)

	// Typing, through characters the keyboard layout lacks.
	text := "Hello, мир ✓ 😀"
	w.Events()
	err = act(map[string]any{"action": "type", "text": text})
	if err == nil {
		evs := waitFor(w, func(e []Event) bool { return len([]rune(joinChars(e))) >= len([]rune(text)) })
		if got := joinChars(evs); got != text {
			err = fmt.Errorf("the window got %q, want %q", got, text)
		}
	}
	r.check("type", err, fmt.Sprintf("%q arrived whole", text))

	w.Events()
	err = act(map[string]any{"action": "key", "keys": "ctrl+a enter"})
	if err == nil {
		evs := waitFor(w, func(e []Event) bool { return len(kinds(e, "key")) >= 2 })
		ks := kinds(evs, "key")
		if len(ks) < 2 || ks[0].Key != "ctrl+a" || ks[1].Key != "enter" {
			err = fmt.Errorf("the window got keys %v, want ctrl+a then enter", keyNames(ks))
		}
	}
	r.check("keys", err, "ctrl+a and enter arrived as chords")

	px, py := pixel(0)
	w.Events()
	err = act(map[string]any{"action": "scroll", "screenshot_id": s.ID, "x": px, "y": py, "dy": 3})
	if err == nil {
		evs := waitFor(w, func(e []Event) bool { return wheelSum(e) >= 3 })
		if n := wheelSum(evs); n != 3 {
			err = fmt.Errorf("the window got %d notches down, want 3", n)
		}
	}
	r.check("scroll", err, "three notches down")

	fx, fy := pixel(0)
	tx, ty := pixel(3)
	w.Events()
	err = act(map[string]any{"action": "drag", "screenshot_id": s.ID, "path": []map[string]int{{"x": fx, "y": fy}, {"x": tx, "y": ty}}})
	if err == nil {
		evs := waitFor(w, func(e []Event) bool { return len(kinds(e, "release")) >= 1 })
		presses, releases, motions := kinds(evs, "press"), kinds(evs, "release"), kinds(evs, "motion")
		sx, sy := quadrantCentre(content, 0)
		ex, ey := quadrantCentre(content, 3)
		switch {
		case len(presses) != 1 || len(releases) != 1:
			err = fmt.Errorf("%d presses and %d releases", len(presses), len(releases))
		case len(motions) < 3:
			err = fmt.Errorf("only %d motions on the way", len(motions))
		case math.Abs(presses[0].X-(sx-content.X)) > tolerance || math.Abs(releases[0].X-(ex-content.X)) > tolerance ||
			math.Abs(presses[0].Y-(sy-content.Y)) > tolerance || math.Abs(releases[0].Y-(ey-content.Y)) > tolerance:
			err = fmt.Errorf("pressed at (%.0f, %.0f) and released at (%.0f, %.0f)", presses[0].X, presses[0].Y, releases[0].X, releases[0].Y)
		}
		if err == nil {
			r.check("drag", nil, fmt.Sprintf("pressed in the top left, %d motions, released in the bottom right", len(motions)))
		}
	}
	if err != nil {
		r.check("drag", err, "")
	}

	// The refusals: nothing outside the window, no screenshot of the whole display, no stale ids.
	err = act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": s.Width + 5, "y": 1})
	r.check("off-window", expect(err, desk.CodeOutOfScope), "a point off the window's picture is refused")
	err = d.host.Call(ctx, "screenshot", map[string]any{"session": session, "display": display}, nil)
	r.check("scope", expect(err, desk.CodeOutOfScope), "the whole display is refused under a window lease")
	if err := w.MoveBy(24, 16); err != nil {
		r.check("stale", err, "")
	} else {
		moved := false
		for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline) && !moved; time.Sleep(30 * time.Millisecond) {
			c, _ := w.Content()
			moved = c != content
		}
		ax, ay := pixel(0)
		err = act(map[string]any{"action": "click", "screenshot_id": s.ID, "x": ax, "y": ay})
		r.check("stale", expect(err, desk.CodeStale), "a screenshot from before the window moved is refused")
	}
	w.Events()
	if err := d.operator.Call(ctx, "stop", nil, nil); err != nil {
		r.check("stop", err, "")
		return
	}
	s2, err := d.screenshot(ctx, w.ID())
	if err == nil {
		err = act(map[string]any{"action": "click", "screenshot_id": s2.ID, "x": 1, "y": 1})
	}
	err = expect(err, desk.CodeNoLease)
	if err == nil {
		if evs := waitFor(w, func(e []Event) bool { return false }); len(kinds(evs, "press")) > 0 {
			err = errors.New("a press reached the window after the stop")
		}
	}
	r.check("stop", err, "after the stop nothing reaches the window")
}

func expect(err error, code int) error {
	if err == nil {
		return fmt.Errorf("was accepted, want refusal %d", code)
	}
	if c := codeOf(err); c != code {
		return fmt.Errorf("refused with %v, want %d", err, code)
	}
	return nil
}

func joinErr(bad []string) error {
	if len(bad) == 0 {
		return nil
	}
	return errors.New(strings.Join(bad, "; "))
}

func joinChars(evs []Event) string {
	var b strings.Builder
	for _, e := range evs {
		if e.Kind == "char" {
			b.WriteString(e.Text)
		}
	}
	return b.String()
}

func keyNames(evs []Event) []string {
	var out []string
	for _, e := range evs {
		out = append(out, e.Key)
	}
	return out
}

func wheelSum(evs []Event) int {
	n := 0
	for _, e := range evs {
		if e.Kind == "wheel" {
			n += e.Wheel
		}
	}
	return n
}
