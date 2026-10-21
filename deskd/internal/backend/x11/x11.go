// Package x11 is the desktop on an X11 display, through the X protocol in pure Go: XTest for input,
// GetImage for capture, RandR for the displays, the screen saver extension for the idle time and the
// locked state, and the EWMH properties a window manager publishes for windows and focus. Without a
// window manager (a bare virtual display) it falls back to the window tree and the input focus.
package x11

import (
	"context"
	"errors"
	"fmt"
	"image"
	"io"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/randr"
	"github.com/jezek/xgb/screensaver"
	"github.com/jezek/xgb/xproto"
	"github.com/jezek/xgb/xtest"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// scratchSettle is how long a character bound to a spare keycode stays bound after it was typed. The
// application reads the key press later than the server sends it and looks the keycode up in the
// keymap it holds then; unbinding at once would let it look up the restored, empty keycode.
const scratchSettle = 40 * time.Millisecond

// X is one connection to a display.
type X struct {
	conn   *xgb.Conn
	root   xproto.Window
	screen *xproto.ScreenInfo
	format pixelFormat
	pad    int

	hasRandr, hasSaver bool

	atomMu sync.Mutex
	atoms  map[string]xproto.Atom

	// typing serialises the keymap's spare keycodes between calls.
	typing sync.Mutex
	// bound are spare keycodes bound for a key that is still down (Key with down true).
	bound map[uint32]uint8
}

func init() {
	// The library logs to standard error by default, among other things that it found no
	// .Xauthority and is trying without one, which is the normal case on a local display.
	xgb.Logger = log.New(io.Discard, "", 0)
}

// Open connects to display ("" is $DISPLAY) and checks for XTest, without which there is no input.
func Open(ctx context.Context, display string) (*X, error) {
	conn, err := xgb.NewConnDisplay(display)
	if err != nil {
		return nil, fmt.Errorf("connecting to the X display: %w", err)
	}
	if err := xtest.Init(conn); err != nil {
		conn.Close()
		return nil, fmt.Errorf("the X server has no XTEST extension, so input cannot be sent: %w", err)
	}
	setup := xproto.Setup(conn)
	screen := setup.DefaultScreen(conn)
	x := &X{conn: conn, root: screen.Root, screen: screen, atoms: map[string]xproto.Atom{}, bound: map[uint32]uint8{}}
	x.hasRandr = randr.Init(conn) == nil
	x.hasSaver = screensaver.Init(conn) == nil
	// The pixel layout of the root window's images.
	x.format.lsbFirst = setup.ImageByteOrder == xproto.ImageOrderLSBFirst
	for _, d := range screen.AllowedDepths {
		for _, v := range d.Visuals {
			if v.VisualId == screen.RootVisual {
				x.format.red, x.format.green, x.format.blue = v.RedMask, v.GreenMask, v.BlueMask
			}
		}
	}
	for _, f := range setup.PixmapFormats {
		if f.Depth == screen.RootDepth {
			x.format.bytesPerPixel = int(f.BitsPerPixel) / 8
			x.pad = int(f.ScanlinePad)
		}
	}
	if x.format.red == 0 || x.format.bytesPerPixel == 0 {
		conn.Close()
		return nil, errors.New("the X server's root visual is not a true-colour one")
	}
	return x, nil
}

func (x *X) Platform() backend.Platform {
	return backend.Platform{OS: "linux", SessionType: backend.SessionX11, Units: backend.UnitsPixels}
}

func (x *X) Close() error {
	x.conn.Close()
	return nil
}

func (x *X) atom(name string) xproto.Atom {
	x.atomMu.Lock()
	defer x.atomMu.Unlock()
	if a, ok := x.atoms[name]; ok {
		return a
	}
	r, err := xproto.InternAtom(x.conn, false, uint16(len(name)), name).Reply()
	if err != nil {
		return 0
	}
	x.atoms[name] = r.Atom
	return r.Atom
}

func (x *X) atomName(a xproto.Atom) string {
	r, err := xproto.GetAtomName(x.conn, a).Reply()
	if err != nil {
		return ""
	}
	return r.Name
}

// Displays are the RandR monitors, or the CRTCs where the server predates monitors, or the screen.
func (x *X) Displays(ctx context.Context) ([]geom.Display, error) {
	var out []geom.Display
	if x.hasRandr {
		if r, err := randr.GetMonitors(x.conn, x.root, true).Reply(); err == nil {
			for i, m := range r.Monitors {
				name := x.atomName(m.Name)
				if name == "" {
					name = strconv.Itoa(i)
				}
				out = append(out, geom.Display{ID: name, Name: name, Primary: m.Primary, PixelsPerUnit: 1, Scale: 1,
					Bounds: geom.Rect{X: float64(m.X), Y: float64(m.Y), W: float64(m.Width), H: float64(m.Height)}})
			}
		}
	}
	if len(out) == 0 {
		out = append(out, geom.Display{ID: "screen", Name: "screen", Primary: true, PixelsPerUnit: 1, Scale: 1,
			Bounds: geom.Rect{W: float64(x.screen.WidthInPixels), H: float64(x.screen.HeightInPixels)}})
	}
	primary := -1
	for i, d := range out {
		if d.Primary {
			primary = i
			break
		}
	}
	if primary < 0 {
		out[0].Primary = true
	} else if primary > 0 {
		out[0], out[primary] = out[primary], out[0]
	}
	return out, nil
}

func (x *X) Permissions(ctx context.Context) backend.Permissions {
	return backend.Permissions{Screen: backend.NotApplicable, Accessibility: backend.NotApplicable,
		InputMonitoring: backend.NotApplicable}
}

// RequestPermission has nothing to ask: X11 grants every client the whole display.
func (x *X) RequestPermission(ctx context.Context, which string) error { return nil }

// Capture reads region of the root window.
func (x *X) Capture(ctx context.Context, d geom.Display, region geom.Rect) (*image.RGBA, error) {
	rx, ry := geom.Snap(region.X), geom.Snap(region.Y)
	w, h := int(region.W+0.5), int(region.H+0.5)
	if w <= 0 || h <= 0 || w > 0x7fff || h > 0x7fff {
		return nil, fmt.Errorf("cannot capture a %d×%d region", w, h)
	}
	// The reply of one request holds at most what the length field counts; a large display is read in
	// bands so no reply nears it.
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	bandRows := max(1, (16<<20)/(w*4))
	for y := 0; y < h; y += bandRows {
		bh := min(bandRows, h-y)
		r, err := xproto.GetImage(x.conn, xproto.ImageFormatZPixmap, xproto.Drawable(x.root), int16(rx), int16(ry+y),
			uint16(w), uint16(bh), 0xffffffff).Reply()
		if err != nil {
			return nil, fmt.Errorf("capturing the screen: %w", err)
		}
		band, err := toRGBA(r.Data, w, bh, x.pad, x.format)
		if err != nil {
			return nil, err
		}
		copy(img.Pix[y*img.Stride:], band.Pix)
	}
	return img, nil
}

func (x *X) property(win xproto.Window, name string, typ xproto.Atom, longs uint32) ([]byte, xproto.Atom, error) {
	a := x.atom(name)
	if a == 0 {
		return nil, 0, errors.New("no atom " + name)
	}
	r, err := xproto.GetProperty(x.conn, false, win, a, typ, 0, longs).Reply()
	if err != nil {
		return nil, 0, err
	}
	return r.Value, r.Type, nil
}

func (x *X) windowList(name string) []xproto.Window {
	v, _, err := x.property(x.root, name, xproto.AtomWindow, 1<<16)
	if err != nil {
		return nil
	}
	out := make([]xproto.Window, 0, len(v)/4)
	for i := 0; i+4 <= len(v); i += 4 {
		out = append(out, xproto.Window(xgb.Get32(v[i:])))
	}
	return out
}

func (x *X) cardinal(win xproto.Window, name string) (uint32, bool) {
	v, _, err := x.property(win, name, xproto.AtomCardinal, 1)
	if err != nil || len(v) < 4 {
		return 0, false
	}
	return xgb.Get32(v), true
}

func (x *X) title(win xproto.Window) string {
	if v, _, err := x.property(win, "_NET_WM_NAME", x.atom("UTF8_STRING"), 1024); err == nil && len(v) > 0 {
		return string(v)
	}
	if v, _, err := x.property(win, "WM_NAME", xproto.AtomAny, 1024); err == nil {
		return string(v)
	}
	return ""
}

// app is the identity behind a window: WM_CLASS, and the process from _NET_WM_PID with its
// executable.
func (x *X) app(win xproto.Window) backend.App {
	var a backend.App
	if v, _, err := x.property(win, "WM_CLASS", xproto.AtomString, 256); err == nil && len(v) > 0 {
		parts := strings.Split(strings.TrimRight(string(v), "\x00"), "\x00")
		a.WMClass = strings.Join(parts, ".")
		a.Name = parts[len(parts)-1]
	}
	if pid, ok := x.cardinal(win, "_NET_WM_PID"); ok && pid > 0 {
		a.PID = int(pid)
		if exe, err := os.Readlink(filepath.Join("/proc", strconv.Itoa(a.PID), "exe")); err == nil {
			a.ExePath = exe
			if a.Name == "" {
				a.Name = filepath.Base(exe)
			}
		}
	}
	switch {
	case a.PID > 0:
		a.ID = "pid:" + strconv.Itoa(a.PID)
	case a.WMClass != "":
		a.ID = "class:" + a.WMClass
	default:
		a.ID = fmt.Sprintf("win:0x%x", uint32(win))
	}
	return a
}

// geometry is a window's rectangle in root coordinates, widened by the frame a window manager
// publishes around it, so a capture shows the title bar the operator sees.
func (x *X) geometry(win xproto.Window) (geom.Rect, bool) {
	g, err := xproto.GetGeometry(x.conn, xproto.Drawable(win)).Reply()
	if err != nil {
		return geom.Rect{}, false
	}
	t, err := xproto.TranslateCoordinates(x.conn, win, x.root, 0, 0).Reply()
	if err != nil {
		return geom.Rect{}, false
	}
	r := geom.Rect{X: float64(t.DstX), Y: float64(t.DstY), W: float64(g.Width), H: float64(g.Height)}
	if v, _, err := x.property(win, "_NET_FRAME_EXTENTS", xproto.AtomCardinal, 4); err == nil && len(v) >= 16 {
		left, right := float64(xgb.Get32(v)), float64(xgb.Get32(v[4:]))
		top, bottom := float64(xgb.Get32(v[8:])), float64(xgb.Get32(v[12:]))
		r = geom.Rect{X: r.X - left, Y: r.Y - top, W: r.W + left + right, H: r.H + top + bottom}
	}
	return r, true
}

func (x *X) hidden(win xproto.Window) bool {
	v, _, err := x.property(win, "_NET_WM_STATE", xproto.AtomAtom, 64)
	if err != nil {
		return false
	}
	h := x.atom("_NET_WM_STATE_HIDDEN")
	for i := 0; i+4 <= len(v); i += 4 {
		if xproto.Atom(xgb.Get32(v[i:])) == h {
			return true
		}
	}
	return false
}

// topLevels are the application windows bottom to top: the window manager's stacking list, or,
// with no window manager, the mapped children of the root.
func (x *X) topLevels() []xproto.Window {
	if ws := x.windowList("_NET_CLIENT_LIST_STACKING"); len(ws) > 0 {
		return ws
	}
	if ws := x.windowList("_NET_CLIENT_LIST"); len(ws) > 0 {
		return ws
	}
	tree, err := xproto.QueryTree(x.conn, x.root).Reply()
	if err != nil {
		return nil
	}
	var out []xproto.Window
	for _, c := range tree.Children {
		attr, err := xproto.GetWindowAttributes(x.conn, c).Reply()
		if err != nil || attr.MapState != xproto.MapStateViewable || attr.OverrideRedirect || attr.Class != xproto.WindowClassInputOutput {
			continue
		}
		out = append(out, c)
	}
	return out
}

func (x *X) focusWindow() xproto.Window {
	if v := x.windowList("_NET_ACTIVE_WINDOW"); len(v) > 0 && v[0] != 0 {
		return v[0]
	}
	f, err := xproto.GetInputFocus(x.conn).Reply()
	if err != nil {
		return 0
	}
	return f.Focus
}

// Windows lists the application windows front to back.
func (x *X) Windows(ctx context.Context) ([]backend.Window, error) {
	tops := x.topLevels()
	focus := x.focusWindow()
	focusTop := x.topOf(focus, tops)
	out := make([]backend.Window, 0, len(tops))
	for i := len(tops) - 1; i >= 0; i-- {
		win := tops[i]
		r, ok := x.geometry(win)
		if !ok || r.W <= 1 || r.H <= 1 {
			continue
		}
		out = append(out, backend.Window{ID: fmt.Sprintf("0x%x", uint32(win)), Title: x.title(win), Bounds: r,
			App: x.app(win), Focused: win == focusTop && focusTop != 0, Minimized: x.hidden(win)})
	}
	return out, nil
}

// topOf is the top-level window that holds win: win itself, or the ancestor of it that is one.
func (x *X) topOf(win xproto.Window, tops []xproto.Window) xproto.Window {
	in := map[xproto.Window]bool{}
	for _, t := range tops {
		in[t] = true
	}
	for i := 0; i < 32 && win != 0 && win != x.root; i++ {
		if in[win] {
			return win
		}
		tree, err := xproto.QueryTree(x.conn, win).Reply()
		if err != nil {
			return 0
		}
		win = tree.Parent
	}
	return 0
}

func (x *X) WindowAt(ctx context.Context, px, py float64) (backend.Window, bool, error) {
	ws, err := x.Windows(ctx)
	if err != nil {
		return backend.Window{}, false, err
	}
	for _, w := range ws {
		if !w.Minimized && w.Bounds.Contains(px, py) {
			return w, true, nil
		}
	}
	return backend.Window{}, false, nil
}

func (x *X) Foreground(ctx context.Context) (backend.Window, bool, error) {
	ws, err := x.Windows(ctx)
	if err != nil {
		return backend.Window{}, false, err
	}
	for _, w := range ws {
		if w.Focused {
			return w, true, nil
		}
	}
	return backend.Window{}, false, nil
}

func parseWindowID(id string) (xproto.Window, error) {
	v, err := strconv.ParseUint(strings.TrimPrefix(id, "0x"), 16, 32)
	if err != nil {
		return 0, fmt.Errorf("window id %q is not an X window", id)
	}
	return xproto.Window(v), nil
}

// Activate asks the window manager to activate the window, as a pager does; with no window manager
// it raises the window and gives it the focus itself.
func (x *X) Activate(ctx context.Context, id string) error {
	win, err := parseWindowID(id)
	if err != nil {
		return err
	}
	if len(x.windowList("_NET_ACTIVE_WINDOW")) > 0 || x.supported("_NET_ACTIVE_WINDOW") {
		ev := xproto.ClientMessageEvent{Format: 32, Window: win, Type: x.atom("_NET_ACTIVE_WINDOW"),
			Data: xproto.ClientMessageDataUnionData32New([]uint32{2, 0, 0, 0, 0})}
		return xproto.SendEventChecked(x.conn, false, x.root,
			xproto.EventMaskSubstructureRedirect|xproto.EventMaskSubstructureNotify, string(ev.Bytes())).Check()
	}
	if err := xproto.ConfigureWindowChecked(x.conn, win, xproto.ConfigWindowStackMode, []uint32{xproto.StackModeAbove}).Check(); err != nil {
		return err
	}
	return xproto.SetInputFocusChecked(x.conn, xproto.InputFocusParent, win, xproto.TimeCurrentTime).Check()
}

func (x *X) supported(name string) bool {
	v, _, err := x.property(x.root, "_NET_SUPPORTED", xproto.AtomAtom, 1<<12)
	if err != nil {
		return false
	}
	a := x.atom(name)
	for i := 0; i+4 <= len(v); i += 4 {
		if xproto.Atom(xgb.Get32(v[i:])) == a {
			return true
		}
	}
	return false
}

func (x *X) Cursor(ctx context.Context) (float64, float64, error) {
	r, err := xproto.QueryPointer(x.conn, x.root).Reply()
	if err != nil {
		return 0, 0, err
	}
	return float64(r.RootX), float64(r.RootY), nil
}

// fake sends one XTest event and waits for the server to have taken it, so events never pile up
// unsent and an error comes back with the call that caused it.
func (x *X) fake(typ, detail byte, px, py int16) error {
	return xtest.FakeInputChecked(x.conn, typ, detail, 0, x.root, px, py, 0).Check()
}

func (x *X) Move(ctx context.Context, px, py float64) error {
	return x.fake(xproto.MotionNotify, 0, int16(geom.Snap(px)), int16(geom.Snap(py)))
}

func (x *X) Button(ctx context.Context, b backend.Button, down bool) error {
	var detail byte
	switch b {
	case backend.Left:
		detail = 1
	case backend.Middle:
		detail = 2
	case backend.Right:
		detail = 3
	default:
		return fmt.Errorf("no button %v", b)
	}
	typ := byte(xproto.ButtonRelease)
	if down {
		typ = xproto.ButtonPress
	}
	return x.fake(typ, detail, 0, 0)
}

// Scroll turns the wheel by notches, as X11's buttons 4 to 7.
func (x *X) Scroll(ctx context.Context, dx, dy int) error {
	click := func(b byte, n int) error {
		for i := 0; i < n; i++ {
			if err := x.fake(xproto.ButtonPress, b, 0, 0); err != nil {
				return err
			}
			if err := x.fake(xproto.ButtonRelease, b, 0, 0); err != nil {
				return err
			}
		}
		return nil
	}
	switch {
	case dy > 0:
		if err := click(5, dy); err != nil {
			return err
		}
	case dy < 0:
		if err := click(4, -dy); err != nil {
			return err
		}
	}
	switch {
	case dx > 0:
		return click(7, dx)
	case dx < 0:
		return click(6, -dx)
	}
	return nil
}

func (x *X) keymap() (keymap, error) {
	setup := xproto.Setup(x.conn)
	lo, hi := setup.MinKeycode, setup.MaxKeycode
	r, err := xproto.GetKeyboardMapping(x.conn, lo, byte(int(hi)-int(lo)+1)).Reply()
	if err != nil {
		return keymap{}, err
	}
	m := keymap{min: uint8(lo), max: uint8(hi), per: int(r.KeysymsPerKeycode)}
	m.syms = make([]uint32, len(r.Keysyms))
	for i, k := range r.Keysyms {
		m.syms[i] = uint32(k)
	}
	return m, nil
}

// bind puts keysym ks on a spare keycode in every column.
func (x *X) bind(code uint8, per int, ks uint32) error {
	syms := make([]xproto.Keysym, per)
	for i := range syms {
		syms[i] = xproto.Keysym(ks)
	}
	return xproto.ChangeKeyboardMappingChecked(x.conn, 1, xproto.Keycode(code), byte(per), syms).Check()
}

func (x *X) unbind(code uint8, per int) {
	_ = xproto.ChangeKeyboardMappingChecked(x.conn, 1, xproto.Keycode(code), byte(per), make([]xproto.Keysym, per)).Check()
}

// Key presses or releases one key. A key the layout lacks is bound to a spare keycode while it is
// down.
func (x *X) Key(ctx context.Context, k keys.Key, down bool) error {
	ks, ok := keysymOf(k)
	if !ok {
		return fmt.Errorf("no keysym for key %q", k)
	}
	x.typing.Lock()
	defer x.typing.Unlock()
	m, err := x.keymap()
	if err != nil {
		return err
	}
	code, _, found := m.find(ks)
	if !found {
		if c, held := x.bound[ks]; held {
			code, found = c, true
		}
	}
	if !found {
		if !down {
			return nil // nothing was pressed for it
		}
		spares := m.spare()
		if len(spares) == 0 {
			return fmt.Errorf("key %q is not on the keyboard layout and no keycode is free to bind it", k)
		}
		code = spares[0]
		if err := x.bind(code, m.per, ks); err != nil {
			return err
		}
		x.bound[ks] = code
	}
	typ := byte(xproto.KeyRelease)
	if down {
		typ = xproto.KeyPress
	}
	err = x.fake(typ, code, 0, 0)
	if !down {
		if c, held := x.bound[ks]; held {
			delete(x.bound, ks)
			time.Sleep(scratchSettle)
			x.unbind(c, m.per)
		}
	}
	return err
}

// Type types text whatever the layout: a character on the layout by its keycode (with Shift for the
// second column), every other one bound for the moment to a spare keycode, all of a call's missing
// characters at once, so the keymap changes twice a call rather than twice a character.
func (x *X) Type(ctx context.Context, text string) error {
	x.typing.Lock()
	defer x.typing.Unlock()
	m, err := x.keymap()
	if err != nil {
		return err
	}
	shiftCode, _, hasShift := m.find(shiftKeysym)
	runes := []rune(text)
	for len(runes) > 0 {
		// Bind as many missing characters as there are spare keycodes, then type up to the first one
		// that does not fit.
		spares := m.spare()
		binding := map[uint32]uint8{}
		n := 0
		for n < len(runes) {
			ks := runeKeysym(runes[n])
			if _, _, ok := m.find(ks); !ok {
				if _, ok := binding[ks]; !ok {
					if len(binding) == len(spares) {
						break
					}
					binding[ks] = spares[len(binding)]
				}
			}
			n++
		}
		if n == 0 {
			return errors.New("no keycode is free to type a character the layout lacks")
		}
		for ks, code := range binding {
			if err := x.bind(code, m.per, ks); err != nil {
				return err
			}
		}
		var typeErr error
		for _, r := range runes[:n] {
			if err := ctx.Err(); err != nil {
				typeErr = err
				break
			}
			ks := runeKeysym(r)
			code, col := binding[ks], 0
			if code == 0 {
				code, col, _ = m.find(ks)
			}
			if typeErr = x.stroke(code, col == 1 && hasShift, shiftCode); typeErr != nil {
				break
			}
		}
		if len(binding) > 0 {
			time.Sleep(scratchSettle)
			for _, code := range binding {
				x.unbind(code, m.per)
			}
		}
		if typeErr != nil {
			return typeErr
		}
		runes = runes[n:]
	}
	return nil
}

func (x *X) stroke(code uint8, shift bool, shiftCode uint8) error {
	if shift {
		if err := x.fake(xproto.KeyPress, shiftCode, 0, 0); err != nil {
			return err
		}
	}
	err := x.fake(xproto.KeyPress, code, 0, 0)
	if err == nil {
		err = x.fake(xproto.KeyRelease, code, 0, 0)
	}
	if shift {
		if e := x.fake(xproto.KeyRelease, shiftCode, 0, 0); err == nil {
			err = e
		}
	}
	return err
}

// Secure reports an active screen saver, which is what a screen locker on X11 raises.
func (x *X) Secure(ctx context.Context) (backend.Secure, error) {
	if !x.hasSaver {
		return backend.Secure{}, nil
	}
	r, err := screensaver.QueryInfo(x.conn, xproto.Drawable(x.root)).Reply()
	if err != nil {
		return backend.Secure{}, err
	}
	if r.State == screensaver.StateOn {
		return backend.Secure{Locked: true, Reason: "screen_saver"}, nil
	}
	return backend.Secure{}, nil
}

// LastInput is the screen saver extension's idle time: it counts XTest's events too, which the
// daemon allows for by comparing it with its own last action.
func (x *X) LastInput(ctx context.Context) (time.Time, bool) {
	if !x.hasSaver {
		return time.Time{}, false
	}
	r, err := screensaver.QueryInfo(x.conn, xproto.Drawable(x.root)).Reply()
	if err != nil {
		return time.Time{}, false
	}
	return time.Now().Add(-time.Duration(r.MsSinceUserInput) * time.Millisecond), true
}
