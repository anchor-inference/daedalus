//go:build windows

package selftest

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

var (
	user32 = windows.NewLazySystemDLL("user32.dll")
	gdi32  = windows.NewLazySystemDLL("gdi32.dll")

	procRegisterClassExW             = user32.NewProc("RegisterClassExW")
	procUnregisterClassW             = user32.NewProc("UnregisterClassW")
	procCreateWindowExW              = user32.NewProc("CreateWindowExW")
	procDestroyWindow                = user32.NewProc("DestroyWindow")
	procDefWindowProcW               = user32.NewProc("DefWindowProcW")
	procGetMessageW                  = user32.NewProc("GetMessageW")
	procTranslateMessage             = user32.NewProc("TranslateMessage")
	procDispatchMessageW             = user32.NewProc("DispatchMessageW")
	procPostMessageW                 = user32.NewProc("PostMessageW")
	procPostQuitMessage              = user32.NewProc("PostQuitMessage")
	procBeginPaint                   = user32.NewProc("BeginPaint")
	procEndPaint                     = user32.NewProc("EndPaint")
	procFillRect                     = user32.NewProc("FillRect")
	procGetClientRect                = user32.NewProc("GetClientRect")
	procGetWindowRect                = user32.NewProc("GetWindowRect")
	procClientToScreen               = user32.NewProc("ClientToScreen")
	procSetWindowPos                 = user32.NewProc("SetWindowPos")
	procSetForegroundWindow          = user32.NewProc("SetForegroundWindow")
	procGetKeyState                  = user32.NewProc("GetKeyState")
	procLoadCursorW                  = user32.NewProc("LoadCursorW")
	procSetThreadDpiAwarenessContext = user32.NewProc("SetThreadDpiAwarenessContext")
	procCreateSolidBrush             = gdi32.NewProc("CreateSolidBrush")
	procDeleteObject                 = gdi32.NewProc("DeleteObject")
)

const (
	wmDestroy       = 0x0002
	wmPaint         = 0x000F
	wmClose         = 0x0010
	wmEraseBkgnd    = 0x0014
	wmKeyDown       = 0x0100
	wmChar          = 0x0102
	wmSysKeyDown    = 0x0104
	wmSysChar       = 0x0106
	wmSysCommand    = 0x0112
	wmMouseMove     = 0x0200
	wmLButtonDown   = 0x0201
	wmLButtonUp     = 0x0202
	wmRButtonDown   = 0x0204
	wmRButtonUp     = 0x0205
	wmMButtonDown   = 0x0207
	wmMButtonUp     = 0x0208
	wmMouseWheel    = 0x020A
	wmDpiChanged    = 0x02E0
	scKeyMenu       = 0xF100
	wsOverlappedWin = 0x00CF0000
	wsVisible       = 0x10000000
	wsExTopmost     = 0x00000008
	swpNoSize       = 0x0001
	swpNoMove       = 0x0002
	swpNoZOrder     = 0x0004
	swpNoActivate   = 0x0010
	idcArrow        = 32512

	vkTab     = 0x09
	vkReturn  = 0x0D
	vkShift   = 0x10
	vkControl = 0x11
	vkMenu    = 0x12
	vkLWin    = 0x5B
	vkRWin    = 0x5C

	// dpiAwarenessContextPerMonitorV2 is DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, the handle -4.
	dpiAwarenessContextPerMonitorV2 = ^uintptr(3)

	// testWindowX and testWindowY place the window on the primary display, whose top-left corner is
	// the origin of the virtual screen on every Windows setup, clear of the taskbar wherever it sits.
	testWindowX = 160
	testWindowY = 160
)

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   windows.Handle
	Icon       windows.Handle
	Cursor     windows.Handle
	Background windows.Handle
	MenuName   *uint16
	ClassName  *uint16
	IconSm     windows.Handle
}

type msg struct {
	Hwnd    windows.HWND
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      struct{ X, Y int32 }
	Private uint32
}

type paintStruct struct {
	Hdc       windows.Handle
	Erase     int32
	Paint     windows.Rect
	Restore   int32
	IncUpdate int32
	Reserved  [32]byte
}

func init() { opener = openWin32 }

// winWindow is the test window. Its message loop runs on one locked thread, as Win32 requires: a
// window's messages are delivered only to the thread that created it.
type winWindow struct {
	hwnd      windows.HWND
	className *uint16
	done      chan struct{}

	mu     sync.Mutex
	events []Event
	// highSurrogate holds the first half of a character beyond the basic plane until its second half
	// arrives in the next WM_CHAR.
	highSurrogate uint16
}

// The window procedure is one callback for the process, dispatching by handle: windows.NewCallback
// slots are never freed, and a selftest run opens a window per check.
var (
	procOnce    sync.Once
	wndProcPtr  uintptr
	liveMu      sync.Mutex
	liveWindows = map[windows.HWND]*winWindow{}
	// creating is the window whose CreateWindowExW is in progress: its first messages (WM_NCCREATE
	// and the like) arrive before the handle is known and registered.
	creating *winWindow
	// createMu lets one window at a time be the one in creation.
	createMu   sync.Mutex
	classCount atomic.Uint64
)

func openWin32(ctx context.Context, title string, w, h int) (Window, error) {
	procOnce.Do(func() { wndProcPtr = windows.NewCallback(wndProc) })
	win := &winWindow{done: make(chan struct{})}
	ready := make(chan error, 1)
	go win.loop(title, w, h, ready)
	select {
	case err := <-ready:
		if err != nil {
			return nil, err
		}
	case <-ctx.Done():
		// The loop goes on to create the window; close it as soon as it exists.
		go func() {
			if err := <-ready; err == nil {
				win.Close()
			}
		}()
		return nil, ctx.Err()
	}
	return win, nil
}

func (win *winWindow) loop(title string, w, h int, ready chan<- error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	defer close(win.done)
	// The window's sizes must be physical pixels whether or not the backend has already made the
	// process DPI aware, so the thread asks for itself; without it the client area of a 150 % display
	// comes out half again as large as asked.
	if procSetThreadDpiAwarenessContext.Find() == nil {
		procSetThreadDpiAwarenessContext.Call(dpiAwarenessContextPerMonitorV2)
	}

	className, _ := windows.UTF16PtrFromString(fmt.Sprintf("deskd-selftest-%d-%d", windows.GetCurrentProcessId(), classCount.Add(1)))
	titlePtr, err := windows.UTF16PtrFromString(title)
	if err != nil {
		ready <- err
		return
	}
	win.className = className
	cursor, _, _ := procLoadCursorW.Call(0, idcArrow)
	// No CS_DBLCLKS: a double click then arrives as two presses, which is what the selftest counts.
	wc := wndClassEx{
		Size:      uint32(unsafe.Sizeof(wndClassEx{})),
		WndProc:   wndProcPtr,
		Cursor:    windows.Handle(cursor),
		ClassName: className,
	}
	if r, _, err := procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc))); r == 0 {
		ready <- fmt.Errorf("RegisterClassExW: %w", err)
		return
	}
	defer procUnregisterClassW.Call(uintptr(unsafe.Pointer(className)), 0)

	createMu.Lock()
	liveMu.Lock()
	creating = win
	liveMu.Unlock()
	hwnd, _, err := procCreateWindowExW.Call(wsExTopmost, uintptr(unsafe.Pointer(className)),
		uintptr(unsafe.Pointer(titlePtr)), wsOverlappedWin|wsVisible,
		testWindowX, testWindowY, uintptr(w), uintptr(h), 0, 0, 0, 0)
	liveMu.Lock()
	creating = nil
	if hwnd != 0 {
		liveWindows[windows.HWND(hwnd)] = win
	}
	liveMu.Unlock()
	createMu.Unlock()
	if hwnd == 0 {
		ready <- fmt.Errorf("CreateWindowExW: %w", err)
		return
	}
	win.hwnd = windows.HWND(hwnd)
	defer func() {
		liveMu.Lock()
		delete(liveWindows, win.hwnd)
		liveMu.Unlock()
	}()

	// CreateWindowExW takes the outer size; grow it by the frame so the client area is exactly w×h,
	// measured rather than computed so that whatever frame this display's scale draws is counted.
	var client, outer windows.Rect
	procGetClientRect.Call(hwnd, uintptr(unsafe.Pointer(&client)))
	procGetWindowRect.Call(hwnd, uintptr(unsafe.Pointer(&outer)))
	ow := int32(w) + (outer.Right - outer.Left) - (client.Right - client.Left)
	oh := int32(h) + (outer.Bottom - outer.Top) - (client.Bottom - client.Top)
	procSetWindowPos.Call(hwnd, 0, 0, 0, uintptr(ow), uintptr(oh), swpNoMove|swpNoZOrder)
	// Typing reaches only the foreground window. The selftest also activates it through the backend,
	// which knows how to get past the foreground lock; this call covers the common case where the
	// selftest was started from the foreground terminal and may take the foreground.
	procSetForegroundWindow.Call(hwnd)
	ready <- nil

	var m msg
	for {
		r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			// 0 is WM_QUIT; -1 an error, after which the loop could only spin.
			return
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

func wndProc(hwnd windows.HWND, message uint32, wParam, lParam uintptr) uintptr {
	liveMu.Lock()
	win := liveWindows[hwnd]
	if win == nil {
		win = creating
	}
	liveMu.Unlock()
	if win != nil {
		if handled, result := win.handle(hwnd, message, wParam, lParam); handled {
			return result
		}
	}
	r, _, _ := procDefWindowProcW.Call(uintptr(hwnd), uintptr(message), wParam, lParam)
	return r
}

func (win *winWindow) handle(hwnd windows.HWND, message uint32, wParam, lParam uintptr) (bool, uintptr) {
	switch message {
	case wmPaint:
		win.paint(hwnd)
		return true, 0
	case wmEraseBkgnd:
		// The paint covers every pixel; erasing first would only flash white under a capture.
		return true, 1
	case wmDpiChanged:
		// Moved onto a display of another scale, the window keeps its size in physical pixels, which
		// is what the checks measured it in, instead of taking the size the system suggests.
		return true, 0
	case wmSysCommand:
		// Alt pressed alone would put the window into menu mode and swallow the next key typed.
		if wParam&0xFFF0 == scKeyMenu {
			return true, 0
		}
	case wmLButtonDown, wmLButtonUp, wmMButtonDown, wmMButtonUp, wmRButtonDown, wmRButtonUp:
		kind, button := "press", 1
		switch message {
		case wmLButtonUp, wmMButtonUp, wmRButtonUp:
			kind = "release"
		}
		switch message {
		case wmMButtonDown, wmMButtonUp:
			button = 2
		case wmRButtonDown, wmRButtonUp:
			button = 3
		}
		x, y := pointFromLParam(lParam)
		win.record(Event{Kind: kind, Button: button, X: x, Y: y})
		return true, 0
	case wmMouseMove:
		x, y := pointFromLParam(lParam)
		win.record(Event{Kind: "motion", X: x, Y: y})
		return true, 0
	case wmMouseWheel:
		delta := int16(uint16(wParam >> 16))
		win.record(Event{Kind: "wheel", Wheel: -int(delta) / 120})
		return true, 0
	case wmChar:
		win.char(uint16(wParam))
		return true, 0
	case wmSysChar:
		// Recorded as a key chord already; passed on, DefWindowProc would beep for the missing menu.
		return true, 0
	case wmKeyDown, wmSysKeyDown:
		if chord := chordFor(wParam); chord != "" {
			win.record(Event{Kind: "key", Key: chord})
		}
		if message == wmSysKeyDown {
			// DefWindowProc still handles Alt+F4 and the system menu.
			return false, 0
		}
		return true, 0
	case wmClose:
		procDestroyWindow.Call(uintptr(hwnd))
		return true, 0
	case wmDestroy:
		procPostQuitMessage.Call(0)
		return true, 0
	}
	return false, 0
}

// pointFromLParam is GET_X_LPARAM and GET_Y_LPARAM: signed, since a captured mouse reports points
// left of and above the client area as negative.
func pointFromLParam(lParam uintptr) (float64, float64) {
	return float64(int16(uint16(lParam))), float64(int16(uint16(lParam >> 16)))
}

func (win *winWindow) paint(hwnd windows.HWND) {
	var ps paintStruct
	hdc, _, _ := procBeginPaint.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&ps)))
	if hdc == 0 {
		return
	}
	defer procEndPaint.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&ps)))
	var rc windows.Rect
	procGetClientRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&rc)))
	midX, midY := rc.Right/2, rc.Bottom/2
	quads := [4]windows.Rect{
		{Left: 0, Top: 0, Right: midX, Bottom: midY},
		{Left: midX, Top: 0, Right: rc.Right, Bottom: midY},
		{Left: 0, Top: midY, Right: midX, Bottom: rc.Bottom},
		{Left: midX, Top: midY, Right: rc.Right, Bottom: rc.Bottom},
	}
	for i, q := range quads {
		c := quadrantColors[i]
		// COLORREF is 0x00BBGGRR.
		brush, _, _ := procCreateSolidBrush.Call(uintptr(c[0]) | uintptr(c[1])<<8 | uintptr(c[2])<<16)
		if brush == 0 {
			continue
		}
		procFillRect.Call(hdc, uintptr(unsafe.Pointer(&q)), brush)
		procDeleteObject.Call(brush)
	}
}

func (win *winWindow) record(e Event) {
	win.mu.Lock()
	win.events = append(win.events, e)
	win.mu.Unlock()
}

// char records one WM_CHAR unit. Control characters are left out: Enter, Tab and the Ctrl chords
// that produce them are recorded as key events, and recording both would count each twice.
func (win *winWindow) char(unit uint16) {
	win.mu.Lock()
	defer win.mu.Unlock()
	switch {
	case unit >= 0xD800 && unit <= 0xDBFF:
		win.highSurrogate = unit
		return
	case unit >= 0xDC00 && unit <= 0xDFFF:
		hi := win.highSurrogate
		win.highSurrogate = 0
		if hi == 0 {
			// A low half with no high one before it is the broken order the backend must not send;
			// record it as the replacement character so the check sees the damage.
			win.events = append(win.events, Event{Kind: "char", Text: "�"})
			return
		}
		r := (rune(hi)-0xD800)<<10 + (rune(unit) - 0xDC00) + 0x10000
		win.events = append(win.events, Event{Kind: "char", Text: string(r)})
		return
	}
	win.highSurrogate = 0
	if unit < 0x20 || unit == 0x7F {
		return
	}
	win.events = append(win.events, Event{Kind: "char", Text: string(rune(unit))})
}

func keyHeld(vk uintptr) bool {
	r, _, _ := procGetKeyState.Call(vk)
	return int16(r) < 0
}

// chordFor names the key of a WM_KEYDOWN in the canonical form of package keys: Enter and Tab always,
// a letter or digit only while Ctrl, Alt or the Windows key is held, since without one it arrives
// as a character instead.
func chordFor(vk uintptr) string {
	ctrl, alt, shift := keyHeld(vkControl), keyHeld(vkMenu), keyHeld(vkShift)
	super := keyHeld(vkLWin) || keyHeld(vkRWin)
	var key string
	switch {
	case vk == vkReturn:
		key = "enter"
	case vk == vkTab:
		key = "tab"
	case (vk >= 'A' && vk <= 'Z') || (vk >= '0' && vk <= '9'):
		if !ctrl && !alt && !super {
			return ""
		}
		key = strings.ToLower(string(rune(vk)))
	default:
		return ""
	}
	var b strings.Builder
	for _, m := range []struct {
		held bool
		name string
	}{{ctrl, "ctrl"}, {alt, "alt"}, {shift, "shift"}, {super, "super"}} {
		if m.held {
			b.WriteString(m.name)
			b.WriteByte('+')
		}
	}
	b.WriteString(key)
	return b.String()
}

func (win *winWindow) ID() string { return fmt.Sprintf("0x%x", uintptr(win.hwnd)) }

func (win *winWindow) Content() (geom.Rect, error) {
	var origin struct{ X, Y int32 }
	if r, _, err := procClientToScreen.Call(uintptr(win.hwnd), uintptr(unsafe.Pointer(&origin))); r == 0 {
		return geom.Rect{}, fmt.Errorf("ClientToScreen: %w", err)
	}
	var rc windows.Rect
	if r, _, err := procGetClientRect.Call(uintptr(win.hwnd), uintptr(unsafe.Pointer(&rc))); r == 0 {
		return geom.Rect{}, fmt.Errorf("GetClientRect: %w", err)
	}
	return geom.Rect{X: float64(origin.X), Y: float64(origin.Y), W: float64(rc.Right - rc.Left), H: float64(rc.Bottom - rc.Top)}, nil
}

func (win *winWindow) Events() []Event {
	win.mu.Lock()
	defer win.mu.Unlock()
	out := win.events
	win.events = nil
	return out
}

// MoveBy moves the window from another thread, which SetWindowPos allows: it sends the window's
// thread the messages it needs and waits for them, and that thread is always pumping.
func (win *winWindow) MoveBy(dx, dy float64) error {
	var rc windows.Rect
	if r, _, err := procGetWindowRect.Call(uintptr(win.hwnd), uintptr(unsafe.Pointer(&rc))); r == 0 {
		return fmt.Errorf("GetWindowRect: %w", err)
	}
	x, y := rc.Left+int32(geom.Snap(dx)), rc.Top+int32(geom.Snap(dy))
	if r, _, err := procSetWindowPos.Call(uintptr(win.hwnd), 0, uintptr(x), uintptr(y), 0, 0,
		swpNoSize|swpNoZOrder|swpNoActivate); r == 0 {
		return fmt.Errorf("SetWindowPos: %w", err)
	}
	return nil
}

// Close asks the window to close; its own thread destroys it, ends the loop and unregisters the
// class, since DestroyWindow called from any other thread fails.
func (win *winWindow) Close() error {
	select {
	case <-win.done:
		return nil
	default:
	}
	if r, _, err := procPostMessageW.Call(uintptr(win.hwnd), wmClose, 0, 0); r == 0 {
		return fmt.Errorf("PostMessageW: %w", err)
	}
	select {
	case <-win.done:
		return nil
	case <-time.After(5 * time.Second):
		return errors.New("the test window did not close")
	}
}
