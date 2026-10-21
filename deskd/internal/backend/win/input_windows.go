//go:build windows

package win

import (
	"context"
	"fmt"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

var procGetAsyncKeyState = user32.NewProc("GetAsyncKeyState")

// sendBatch is how many events one SendInput call carries. SendInput inserts a call's events as one
// uninterrupted run, which is what keeps the operator's own keystrokes from landing inside a word;
// an unbounded run, though, can overflow the target's message queue (ten thousand messages by
// default) before it has drained, and the overflow is dropped without a word.
const sendBatch = 256

// unassignedVK is a virtual-key code winuser.h leaves unassigned, tapped where a key press must
// happen and must do nothing (see Activate).
const unassignedVK = 0xE8

// sendInput inserts events of one shape and checks they were all taken. SendInput reports how many it
// inserted; when user interface privilege isolation stands in front of the target, recent Windows
// inserts none and sets access denied, older builds inserted them and dropped them on the way, which
// is why the elevated check before each action stays as well.
func sendInput[T mouseINPUT | keyINPUT](events []T) error {
	if len(events) == 0 {
		return nil
	}
	var zero T
	r, _, err := procSendInput.Call(uintptr(len(events)), uintptr(unsafe.Pointer(&events[0])), unsafe.Sizeof(zero))
	if int(r) == len(events) {
		return nil
	}
	if isAccessDenied(err) {
		return &backend.UnreachableError{Reason: "the system blocked synthetic input (a window of higher integrity, or a secure desktop)"}
	}
	return fmt.Errorf("SendInput inserted %d of %d events: %w", r, len(events), callErr("SendInput", err))
}

func (d *desktop) mouse(events ...mouseInput) error {
	batch := make([]mouseINPUT, len(events))
	for i, e := range events {
		e.ExtraInfo = d.nonce
		batch[i] = mouseINPUT{Type: inputMouse, Mi: e}
	}
	return sendInput(batch)
}

func (d *desktop) keyboard(events ...keybdInput) error {
	batch := make([]keyINPUT, len(events))
	for i, e := range events {
		e.ExtraInfo = d.nonce
		batch[i] = keyINPUT{Type: inputKeyboard, Ki: e}
	}
	return sendInput(batch)
}

func (d *desktop) Cursor(ctx context.Context) (float64, float64, error) {
	p, err := getCursorPos()
	if err != nil {
		return 0, 0, err
	}
	return float64(p.X), float64(p.Y), nil
}

// Move sends an absolute move over the whole virtual screen, then checks where the pointer went.
// SendInput is the first choice because its move travels the input stream like a real mouse's: it
// carries the nonce, raw-input readers see it, and hover effects follow. Its 0–65535 coordinates are
// scaled by the system, though, and some pointer drivers (virtual machines' and remote desktops'
// absolute pointing devices) rescale them again, landing pixels off; SetCursorPos places the pointer
// on the exact pixel and is the fallback when the check finds it more than a pixel away.
func (d *desktop) Move(ctx context.Context, x, y float64) error {
	px, py := geom.Snap(x), geom.Snap(y)
	nx, ny := geom.NormalizeVirtual(px, py, virtualScreen())
	if err := d.mouse(mouseInput{Dx: nx, Dy: ny, Flags: mouseeventfMove | mouseeventfAbsolute | mouseeventfVirtualDesk}); err != nil {
		return err
	}
	// The raw input thread applies the move after SendInput has returned, so the first read may still
	// show the old position; a short wait keeps that from being taken for a miss.
	deadline := time.Now().Add(50 * time.Millisecond)
	for {
		p, err := getCursorPos()
		if err != nil {
			return err
		}
		if abs(int(p.X)-px) <= 1 && abs(int(p.Y)-py) <= 1 {
			return nil
		}
		if time.Now().After(deadline) {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Millisecond):
		}
	}
	if r, _, err := procSetCursorPos.Call(uintptr(int32(px)), uintptr(int32(py))); r == 0 {
		return callErr("SetCursorPos", err)
	}
	return nil
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

func (d *desktop) Button(ctx context.Context, b backend.Button, down bool) error {
	var flags uint32
	switch b {
	case backend.Left:
		flags = pick(down, mouseeventfLeftDown, mouseeventfLeftUp)
	case backend.Middle:
		flags = pick(down, mouseeventfMiddleDown, mouseeventfMiddleUp)
	case backend.Right:
		flags = pick(down, mouseeventfRightDown, mouseeventfRightUp)
	default:
		return fmt.Errorf("no mouse button %v", b)
	}
	// A press goes to the window under the pointer. A release is always sent, whatever is under it,
	// so a button is never left held down.
	if down {
		if err := d.refuseElevatedAtCursor(); err != nil {
			return err
		}
	}
	return d.mouse(mouseInput{Flags: flags})
}

func pick(cond bool, yes, no uint32) uint32 {
	if cond {
		return yes
	}
	return no
}

func (d *desktop) Scroll(ctx context.Context, dx, dy int) error {
	if dx == 0 && dy == 0 {
		return nil
	}
	if err := d.refuseElevatedAtCursor(); err != nil {
		return err
	}
	var events []mouseInput
	for _, v := range wheelData(dy, true) {
		events = append(events, mouseInput{MouseData: uint32(v), Flags: mouseeventfWheel})
	}
	for _, v := range wheelData(dx, false) {
		events = append(events, mouseInput{MouseData: uint32(v), Flags: mouseeventfHWheel})
	}
	return d.mouse(events...)
}

func (d *desktop) Key(ctx context.Context, k keys.Key, down bool) error {
	code, err := d.virtualKey(k)
	if err != nil {
		return err
	}
	// A press is refused in front of an elevated window; a release always goes out, or a modifier
	// pressed before the window changed would stay held for the operator.
	if down {
		if err := d.refuseElevatedForeground(); err != nil {
			return err
		}
	}
	scan, _, _ := procMapVirtualKeyW.Call(uintptr(code.vk), mapvkVKToVSC)
	var flags uint32
	if code.extended {
		flags |= keyeventfExtendedKey
	}
	if !down {
		flags |= keyeventfKeyUp
	}
	// The scan code goes along with the virtual key: programs that read the scan code from lParam
	// (games, terminal emulators, remote-desktop clients) see a zero without it.
	return d.keyboard(keybdInput{Vk: code.vk, Scan: uint16(scan), Flags: flags})
}

// virtualKey is the key that types k. A character key is looked up on the layout of the window that
// has the keyboard, not the daemon's own thread: the operator may switch layouts per window, and the
// daemon's thread keeps the one it started with. Only the key is pressed, never the Shift or AltGr
// the layout puts the character on; the chord names its modifiers, and a character that needs others
// is for Type.
func (d *desktop) virtualKey(k keys.Key) (vkCode, error) {
	if k.Name != "" {
		code, ok := namedKey(k)
		if !ok {
			return vkCode{}, fmt.Errorf("no key is called %q", k.Name)
		}
		return code, nil
	}
	if k.Rune <= 0 || k.Rune > 0xFFFF {
		return vkCode{}, fmt.Errorf("no key on the keyboard types %q; type it instead", string(k.Rune))
	}
	var hkl windows.Handle
	if fg := windows.GetForegroundWindow(); fg != 0 {
		tid, _ := windows.GetWindowThreadProcessId(fg, nil)
		hkl = windows.GetKeyboardLayout(tid)
	}
	r, _, _ := procVkKeyScanExW.Call(uintptr(k.Rune), uintptr(hkl))
	if int16(r) == -1 {
		return vkCode{}, fmt.Errorf("no key on the current keyboard layout types %q; type it instead", string(k.Rune))
	}
	return vkCode{vk: uint16(r & 0xFF)}, nil
}

// Type sends the text as Unicode key events, which reach the window as WM_CHAR whatever the layout.
func (d *desktop) Type(ctx context.Context, text string) error {
	if err := d.refuseElevatedForeground(); err != nil {
		return err
	}
	strokes := keys.UnicodeStrokes(text)
	var batch []keybdInput
	flush := func() error {
		err := d.keyboard(batch...)
		batch = batch[:0]
		return err
	}
	for _, s := range strokes {
		// A batch ends only where a character begins, never between the halves of a surrogate pair:
		// the window joins the pair only when both arrive in one run.
		if len(batch) >= sendBatch && !s.Up && !isLowSurrogate(s.Unit) {
			if err := flush(); err != nil {
				return err
			}
			// Let the target drain its queue before the next run (see sendBatch).
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(5 * time.Millisecond):
			}
		}
		flags := uint32(keyeventfUnicode)
		if s.Up {
			flags |= keyeventfKeyUp
		}
		batch = append(batch, keybdInput{Scan: s.Unit, Flags: flags})
	}
	return flush()
}

func isLowSurrogate(u uint16) bool { return u >= 0xDC00 && u <= 0xDFFF }

func (d *desktop) refuseElevatedForeground() error {
	fg := foregroundRoot()
	if fg == 0 {
		return nil
	}
	return d.refuseElevated(fg)
}

func (d *desktop) refuseElevatedAtCursor() error {
	p, err := getCursorPos()
	if err != nil {
		return err
	}
	hwnd := windowFromPoint(p.X, p.Y)
	if hwnd == 0 {
		return nil
	}
	if root := getAncestor(hwnd, gaRoot); root != 0 {
		hwnd = root
	}
	return d.refuseElevated(hwnd)
}

// refuseElevated stops input before it is sent to a window above our integrity level: the system
// would drop it, on older builds without even a failed call, and the daemon would report a click
// that never happened.
func (d *desktop) refuseElevated(hwnd windows.HWND) error {
	var pid uint32
	windows.GetWindowThreadProcessId(hwnd, &pid)
	if app := d.appOf(pid); app.Elevated {
		return &backend.UnreachableError{Reason: fmt.Sprintf("%s runs with higher privileges than the daemon (as administrator)", app.Name)}
	}
	return nil
}

// withAltHeld runs fn with Alt held down, for the foreground lock (see Activate), and taps an
// unassigned key before releasing Alt so that no window is left in menu mode.
func (d *desktop) withAltHeld(fn func()) error {
	altScan, _, _ := procMapVirtualKeyW.Call(vkMenu, mapvkVKToVSC)
	if err := d.keyboard(keybdInput{Vk: vkMenu, Scan: uint16(altScan)}); err != nil {
		return err
	}
	// The lock is lifted when the system has processed the press, which happens on its own thread
	// after SendInput returns; calling fn before that would meet the lock still in place.
	deadline := time.Now().Add(50 * time.Millisecond)
	for time.Now().Before(deadline) {
		if s, _, _ := procGetAsyncKeyState.Call(vkMenu); int16(s) < 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	fn()
	// One run, so that nothing the operator types can fall between the tap and the release.
	return d.keyboard(
		keybdInput{Vk: unassignedVK},
		keybdInput{Vk: unassignedVK, Flags: keyeventfKeyUp},
		keybdInput{Vk: vkMenu, Scan: uint16(altScan), Flags: keyeventfKeyUp},
	)
}
