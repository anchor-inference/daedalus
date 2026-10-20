package selftest

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"time"
	"unicode"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/x11"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

func init() {
	opener = openX11Window
	prepare = prepareX11
	openBackend = func(ctx context.Context) (backend.Backend, error) {
		x, err := x11.Open(ctx, os.Getenv("DISPLAY"))
		if err != nil {
			return nil, err
		}
		return x, nil
	}
}

// prepareX11 makes sure there is an X display to test on. With none, or on a Wayland session whose
// X11 server would show the test only a part of the screen, it starts a virtual display (Xvfb) of its
// own, so the selftest never needs the operator's screen at all.
func prepareX11() (func(), string, error) {
	wayland := os.Getenv("XDG_SESSION_TYPE") == "wayland" || os.Getenv("WAYLAND_DISPLAY") != ""
	if os.Getenv("DISPLAY") != "" && !wayland {
		return func() {}, "display " + os.Getenv("DISPLAY"), nil
	}
	bin, err := exec.LookPath("Xvfb")
	if err != nil {
		return nil, "", errors.New("there is no X display and no Xvfb to start one")
	}
	n := 0
	for i := 90; i < 190; i++ {
		_, e1 := os.Stat(fmt.Sprintf("/tmp/.X11-unix/X%d", i))
		_, e2 := os.Stat(fmt.Sprintf("/tmp/.X%d-lock", i))
		if errors.Is(e1, os.ErrNotExist) && errors.Is(e2, os.ErrNotExist) {
			n = i
			break
		}
	}
	if n == 0 {
		return nil, "", errors.New("no free X display number for Xvfb")
	}
	cmd := exec.Command(bin, ":"+strconv.Itoa(n), "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-noreset")
	if err := cmd.Start(); err != nil {
		return nil, "", fmt.Errorf("starting Xvfb: %w", err)
	}
	stop := func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}
	sock := filepath.Join("/tmp/.X11-unix", "X"+strconv.Itoa(n))
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(50 * time.Millisecond) {
		if _, err := os.Stat(sock); err == nil {
			break
		}
		if time.Now().After(deadline) {
			stop()
			return nil, "", errors.New("Xvfb did not open its display")
		}
	}
	prev, had := os.LookupEnv("DISPLAY")
	os.Setenv("DISPLAY", ":"+strconv.Itoa(n))
	note := "a virtual display :" + strconv.Itoa(n) + " the selftest started"
	if wayland {
		note += " (this is a Wayland session, which computer use does not drive yet)"
	}
	return func() {
		stop()
		if had {
			os.Setenv("DISPLAY", prev)
		} else {
			os.Unsetenv("DISPLAY")
		}
	}, note, nil
}

type x11Window struct {
	conn *xgb.Conn
	root xproto.Window
	win  xproto.Window
	w, h int
	gcs  [4]xproto.Gcontext

	mu     sync.Mutex
	events []Event
	keymap map[xproto.Keycode][]xproto.Keysym
	mapped chan struct{}
	done   chan struct{}
}

// pixelFor is a colour as a pixel of a true-colour visual with the given masks.
func pixelFor(c [3]uint8, red, green, blue uint32) uint32 {
	put := func(v uint8, mask uint32) uint32 {
		if mask == 0 {
			return 0
		}
		shift := 0
		for mask>>shift&1 == 0 {
			shift++
		}
		width := 0
		for mask>>(shift+width)&1 == 1 {
			width++
		}
		return (uint32(v) >> (8 - min(8, width))) << shift
	}
	return put(c[0], red) | put(c[1], green) | put(c[2], blue)
}

func openX11Window(ctx context.Context, title string, w, h int) (Window, error) {
	conn, err := xgb.NewConnDisplay(os.Getenv("DISPLAY"))
	if err != nil {
		return nil, err
	}
	screen := xproto.Setup(conn).DefaultScreen(conn)
	var red, green, blue uint32
	for _, d := range screen.AllowedDepths {
		for _, v := range d.Visuals {
			if v.VisualId == screen.RootVisual {
				red, green, blue = v.RedMask, v.GreenMask, v.BlueMask
			}
		}
	}
	win, err := xproto.NewWindowId(conn)
	if err != nil {
		conn.Close()
		return nil, err
	}
	xw := &x11Window{conn: conn, root: screen.Root, win: win, w: w, h: h, mapped: make(chan struct{}), done: make(chan struct{})}
	mask := uint32(xproto.EventMaskExposure | xproto.EventMaskButtonPress | xproto.EventMaskButtonRelease |
		xproto.EventMaskPointerMotion | xproto.EventMaskKeyPress | xproto.EventMaskStructureNotify)
	if err := xproto.CreateWindowChecked(conn, screen.RootDepth, win, screen.Root, 60, 60, uint16(w), uint16(h), 0,
		xproto.WindowClassInputOutput, screen.RootVisual, xproto.CwBackPixel|xproto.CwEventMask,
		[]uint32{screen.BlackPixel, mask}).Check(); err != nil {
		conn.Close()
		return nil, err
	}
	setString := func(prop, typ xproto.Atom, v string) {
		xproto.ChangeProperty(conn, xproto.PropModeReplace, win, prop, typ, 8, uint32(len(v)), []byte(v))
	}
	setString(xproto.AtomWmName, xproto.AtomString, title)
	setString(xproto.AtomWmClass, xproto.AtomString, "deskd-selftest\x00Deskd-selftest\x00")
	for i, c := range quadrantColors {
		gc, err := xproto.NewGcontextId(conn)
		if err != nil {
			conn.Close()
			return nil, err
		}
		xproto.CreateGC(conn, gc, xproto.Drawable(win), xproto.GcForeground, []uint32{pixelFor(c, red, green, blue)})
		xw.gcs[i] = gc
	}
	if err := xw.loadKeymap(); err != nil {
		conn.Close()
		return nil, err
	}
	go xw.loop()
	if err := xproto.MapWindowChecked(conn, win).Check(); err != nil {
		xw.Close()
		return nil, err
	}
	select {
	case <-xw.mapped:
	case <-time.After(5 * time.Second):
		xw.Close()
		return nil, errors.New("the test window was never mapped")
	}
	xproto.ConfigureWindow(conn, win, xproto.ConfigWindowStackMode, []uint32{xproto.StackModeAbove})
	if err := xproto.SetInputFocusChecked(conn, xproto.InputFocusParent, win, xproto.TimeCurrentTime).Check(); err != nil {
		xw.Close()
		return nil, err
	}
	xw.paint()
	return xw, nil
}

func (xw *x11Window) loadKeymap() error {
	setup := xproto.Setup(xw.conn)
	lo, hi := setup.MinKeycode, setup.MaxKeycode
	r, err := xproto.GetKeyboardMapping(xw.conn, lo, byte(int(hi)-int(lo)+1)).Reply()
	if err != nil {
		return err
	}
	m := map[xproto.Keycode][]xproto.Keysym{}
	per := int(r.KeysymsPerKeycode)
	for i := 0; i+per <= len(r.Keysyms); i += per {
		m[lo+xproto.Keycode(i/per)] = r.Keysyms[i : i+per]
	}
	xw.mu.Lock()
	xw.keymap = m
	xw.mu.Unlock()
	return nil
}

func (xw *x11Window) paint() {
	hw, hh := xw.w/2, xw.h/2
	rects := [4]xproto.Rectangle{
		{X: 0, Y: 0, Width: uint16(hw), Height: uint16(hh)},
		{X: int16(hw), Y: 0, Width: uint16(xw.w - hw), Height: uint16(hh)},
		{X: 0, Y: int16(hh), Width: uint16(hw), Height: uint16(xw.h - hh)},
		{X: int16(hw), Y: int16(hh), Width: uint16(xw.w - hw), Height: uint16(xw.h - hh)},
	}
	for i, r := range rects {
		xproto.PolyFillRectangle(xw.conn, xproto.Drawable(xw.win), xw.gcs[i], []xproto.Rectangle{r})
	}
	xproto.GetInputFocus(xw.conn).Reply() // a round trip, so the drawing is done when paint returns
}

func (xw *x11Window) record(e Event) {
	xw.mu.Lock()
	xw.events = append(xw.events, e)
	xw.mu.Unlock()
}

// loop reads the window's events. A mapping change is read at once, in the order the events came:
// a character the daemon binds to a spare keycode arrives between two of them.
func (xw *x11Window) loop() {
	defer close(xw.done)
	mappedOnce := false
	for {
		ev, err := xw.conn.WaitForEvent()
		if ev == nil && err == nil {
			return
		}
		switch e := ev.(type) {
		case xproto.MapNotifyEvent:
			if !mappedOnce {
				mappedOnce = true
				close(xw.mapped)
			}
		case xproto.ExposeEvent:
			if e.Count == 0 {
				xw.paint()
			}
		case xproto.MappingNotifyEvent:
			_ = xw.loadKeymap()
		case xproto.ButtonPressEvent:
			switch e.Detail {
			case 1, 2, 3:
				xw.record(Event{Kind: "press", X: float64(e.EventX), Y: float64(e.EventY), Button: buttonOf(e.Detail)})
			case 4:
				xw.record(Event{Kind: "wheel", Wheel: -1})
			case 5:
				xw.record(Event{Kind: "wheel", Wheel: 1})
			}
		case xproto.ButtonReleaseEvent:
			if e.Detail >= 1 && e.Detail <= 3 {
				xw.record(Event{Kind: "release", X: float64(e.EventX), Y: float64(e.EventY), Button: buttonOf(e.Detail)})
			}
		case xproto.MotionNotifyEvent:
			xw.record(Event{Kind: "motion", X: float64(e.EventX), Y: float64(e.EventY)})
		case xproto.KeyPressEvent:
			xw.key(e)
		}
	}
}

// buttonOf is X11's button number in the selftest's terms (left 1, middle 2, right 3), which are
// the same numbers.
func buttonOf(detail xproto.Button) int { return int(detail) }

func (xw *x11Window) key(e xproto.KeyPressEvent) {
	xw.mu.Lock()
	syms := xw.keymap[e.Detail]
	xw.mu.Unlock()
	if len(syms) == 0 {
		return
	}
	shift := e.State&xproto.ModMaskShift != 0
	ks := uint32(syms[0])
	if shift && len(syms) > 1 && syms[1] != 0 {
		ks = uint32(syms[1])
	}
	switch ks {
	case 0xff0d:
		xw.record(Event{Kind: "key", Key: "enter"})
		return
	case 0xff09:
		xw.record(Event{Kind: "key", Key: "tab"})
		return
	}
	if ks >= 0xffe1 && ks <= 0xffee {
		return // a modifier on its own
	}
	r := x11.KeysymRune(ks)
	if r == 0 {
		return
	}
	if shift && (len(syms) < 2 || syms[1] == 0) {
		r = unicode.ToUpper(r)
	}
	prefix := ""
	if e.State&xproto.ModMaskControl != 0 {
		prefix += "ctrl+"
	}
	if e.State&xproto.ModMask1 != 0 {
		prefix += "alt+"
	}
	if e.State&xproto.ModMask4 != 0 {
		if shift {
			prefix += "shift+"
		}
		prefix += "super+"
	}
	if prefix != "" {
		xw.record(Event{Kind: "key", Key: prefix + string(unicode.ToLower(r))})
		return
	}
	xw.record(Event{Kind: "char", Text: string(r)})
}

func (xw *x11Window) ID() string { return fmt.Sprintf("0x%x", uint32(xw.win)) }

func (xw *x11Window) Content() (geom.Rect, error) {
	t, err := xproto.TranslateCoordinates(xw.conn, xw.win, xw.root, 0, 0).Reply()
	if err != nil {
		return geom.Rect{}, err
	}
	return geom.Rect{X: float64(t.DstX), Y: float64(t.DstY), W: float64(xw.w), H: float64(xw.h)}, nil
}

func (xw *x11Window) Events() []Event {
	xw.mu.Lock()
	defer xw.mu.Unlock()
	out := xw.events
	xw.events = nil
	return out
}

func (xw *x11Window) MoveBy(dx, dy float64) error {
	g, err := xproto.GetGeometry(xw.conn, xproto.Drawable(xw.win)).Reply()
	if err != nil {
		return err
	}
	return xproto.ConfigureWindowChecked(xw.conn, xw.win, xproto.ConfigWindowX|xproto.ConfigWindowY,
		[]uint32{uint32(int32(g.X) + int32(dx)), uint32(int32(g.Y) + int32(dy))}).Check()
}

func (xw *x11Window) Close() error {
	xproto.DestroyWindow(xw.conn, xw.win)
	xproto.GetInputFocus(xw.conn).Reply()
	xw.conn.Close()
	<-xw.done
	return nil
}
