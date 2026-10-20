//go:build windows

// Package win is the Windows desktop: GDI capture, SendInput, and the window and process calls of
// user32, all through golang.org/x/sys with no cgo. The process is per-monitor DPI aware, so every
// coordinate it reads or writes is a physical pixel of the one virtual screen, whatever each
// display's scale.
package win

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"image"
	"math"
	"sort"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

type desktop struct {
	// nonce marks every event this process injects (dwExtraInfo). Nothing reads it yet; it is there
	// so that a low-level hook added later can tell the agent's input from the operator's, which
	// LastInput cannot: the system counts injected input as input like any other. It is random per
	// process so that another program cannot pass its input off as the agent's by guessing it.
	nonce uintptr

	mu   sync.Mutex
	apps map[uint32]cachedApp
}

// Open makes the process per-monitor DPI aware and returns the desktop. The awareness has to be set
// before the first window or coordinate call: an unaware process is shown a virtualised desktop
// scaled to 96 DPI, where a capture is blurred and a click on a 150 % display lands a third off.
func Open(ctx context.Context) (backend.Backend, error) {
	setDPIAwareness()
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return nil, fmt.Errorf("drawing the input nonce: %w", err)
	}
	d := &desktop{nonce: uintptr(binary.LittleEndian.Uint64(b[:])), apps: map[uint32]cachedApp{}}
	if d.nonce == 0 {
		d.nonce = 1
	}
	return d, nil
}

// setDPIAwareness asks for the newest awareness the system offers. Each call fails with access
// denied when the awareness was already set (by a manifest, or by an earlier Open in the same
// process), which is success for our purpose; only "the procedure does not exist" moves on to the
// older call.
func setDPIAwareness() {
	if procSetProcessDpiAwarenessContext.Find() == nil {
		if r, _, _ := procSetProcessDpiAwarenessContext.Call(dpiAwarenessContextPerMonitorV2); r != 0 {
			return
		}
		// Windows 10 before 1703 knows the call but not the V2 context; it still takes the older
		// per-monitor awareness below.
	}
	if procSetProcessDpiAwareness.Find() == nil {
		hr, _, _ := procSetProcessDpiAwareness.Call(processPerMonitorDpiAware)
		if hr == 0 || uint32(hr) == uint32(windows.E_ACCESSDENIED) {
			return
		}
	}
	if procSetProcessDPIAware.Find() == nil {
		procSetProcessDPIAware.Call()
	}
}

func (d *desktop) Platform() backend.Platform {
	return backend.Platform{OS: "windows", SessionType: backend.SessionWin32, Units: backend.UnitsPixels}
}

// Windows asks for no grant computer use needs: capture and input are open to any program of the
// session, and the one wall, an elevated window, is not a permission the operator can give.
func (d *desktop) Permissions(ctx context.Context) backend.Permissions {
	return backend.Permissions{
		Screen:          backend.NotApplicable,
		Accessibility:   backend.NotApplicable,
		InputMonitoring: backend.NotApplicable,
	}
}

// RequestPermission succeeds for every known permission, there being nothing to ask for. Returning
// ErrUnsupported instead would make the operator's grant flow report a failure for a permission that
// is already, in effect, granted.
func (d *desktop) RequestPermission(ctx context.Context, which string) error {
	switch which {
	case backend.PermScreen, backend.PermAccessibility, backend.PermInputMonitoring:
		return nil
	}
	return fmt.Errorf("no permission is called %q", which)
}

// The enumeration callbacks are made once for the process: windows.NewCallback hands out a slot
// from a fixed table that is never returned, so a callback per call would exhaust it after a few
// thousand screenshots. The callbacks write into the collector of the call in progress, and the
// mutexes keep two calls from sharing one.
var (
	monitorOnce     sync.Once
	monitorCallback uintptr
	monitorMu       sync.Mutex
	monitorFound    []uintptr
)

func enumMonitors() ([]uintptr, error) {
	monitorOnce.Do(func() {
		monitorCallback = windows.NewCallback(func(hmon, hdc uintptr, rect *windows.Rect, data uintptr) uintptr {
			monitorFound = append(monitorFound, hmon)
			return 1
		})
	})
	monitorMu.Lock()
	defer monitorMu.Unlock()
	monitorFound = nil
	if r, _, err := procEnumDisplayMonitors.Call(0, 0, monitorCallback, 0); r == 0 {
		return nil, callErr("EnumDisplayMonitors", err)
	}
	found := monitorFound
	monitorFound = nil
	return found, nil
}

func (d *desktop) Displays(ctx context.Context) ([]geom.Display, error) {
	monitors, err := enumMonitors()
	if err != nil {
		return nil, err
	}
	var out []geom.Display
	for _, hmon := range monitors {
		mi := monitorInfoEx{Size: uint32(unsafe.Sizeof(monitorInfoEx{}))}
		if r, _, err := procGetMonitorInfoW.Call(hmon, uintptr(unsafe.Pointer(&mi))); r == 0 {
			return nil, callErr("GetMonitorInfoW", err)
		}
		scale := 1.0
		if procGetDpiForMonitor.Find() == nil {
			var dpiX, dpiY uint32
			hr, _, _ := procGetDpiForMonitor.Call(hmon, mdtEffectiveDpi,
				uintptr(unsafe.Pointer(&dpiX)), uintptr(unsafe.Pointer(&dpiY)))
			if hr == 0 && dpiX > 0 {
				scale = float64(dpiX) / 96
			}
		}
		// The device name ("\\.\DISPLAY1") is the id: an HMONITOR is a handle that changes when the
		// display configuration does, while the name stays with the output.
		name := windows.UTF16ToString(mi.Device[:])
		r := mi.Monitor
		out = append(out, geom.Display{
			ID:            name,
			Name:          name,
			Bounds:        geom.Rect{X: float64(r.Left), Y: float64(r.Top), W: float64(r.Right - r.Left), H: float64(r.Bottom - r.Top)},
			Primary:       mi.Flags&monitorInfoFPrimary != 0,
			PixelsPerUnit: 1,
			Scale:         scale,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("the system reports no display")
	}
	// The primary first, then left to right and top to bottom, so the order does not follow the
	// enumeration's, which is undocumented.
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Primary != b.Primary {
			return a.Primary
		}
		if a.Bounds.X != b.Bounds.X {
			return a.Bounds.X < b.Bounds.X
		}
		return a.Bounds.Y < b.Bounds.Y
	})
	return out, nil
}

// virtualScreen is the rectangle of all displays, the space SendInput's absolute coordinates span.
func virtualScreen() geom.Rect {
	return geom.Rect{
		X: float64(getSystemMetrics(smXVirtualScreen)),
		Y: float64(getSystemMetrics(smYVirtualScreen)),
		W: float64(getSystemMetrics(smCXVirtualScreen)),
		H: float64(getSystemMetrics(smCYVirtualScreen)),
	}
}

// Capture copies the region from the screen with GDI. Desktop Duplication would be faster but hands
// out whole displays at a time, needs Direct3D, and fails for a while after every mode change; a
// screenshot per action is not a video stream, and BitBlt is enough.
func (d *desktop) Capture(ctx context.Context, display geom.Display, region geom.Rect) (*image.RGBA, error) {
	x, y := geom.Snap(region.X), geom.Snap(region.Y)
	w, h := int(math.Round(region.W)), int(math.Round(region.H))
	if w <= 0 || h <= 0 {
		return nil, errors.New("the region to capture is empty")
	}
	screen, _, err := procGetDC.Call(0)
	if screen == 0 {
		return nil, callErr("GetDC", err)
	}
	defer procReleaseDC.Call(0, screen)
	mem, _, err := procCreateCompatibleDC.Call(screen)
	if mem == 0 {
		return nil, callErr("CreateCompatibleDC", err)
	}
	defer procDeleteDC.Call(mem)

	bi := bitmapInfo{Header: bitmapInfoHeader{
		Size:     uint32(unsafe.Sizeof(bitmapInfoHeader{})),
		Width:    int32(w),
		Height:   -int32(h), // negative: rows top to bottom, as image.RGBA stores them
		Planes:   1,
		BitCount: 32,
		// BI_RGB with 32 bits is blue, green, red, unused in each pixel.
		Compression: biRGB,
	}}
	var bits unsafe.Pointer
	bmp, _, err := procCreateDIBSection.Call(mem, uintptr(unsafe.Pointer(&bi)), dibRGBColor,
		uintptr(unsafe.Pointer(&bits)), 0, 0)
	if bmp == 0 || bits == nil {
		return nil, callErr("CreateDIBSection", err)
	}
	defer procDeleteObject.Call(bmp)
	old, _, _ := procSelectObject.Call(mem, bmp)
	// The bitmap must be selected out before it is deleted, or DeleteObject fails and the bitmap
	// leaks; defers run last in first out, so this runs before the DeleteObject above.
	defer procSelectObject.Call(mem, old)

	if r, _, err := procBitBlt.Call(mem, 0, 0, uintptr(w), uintptr(h), screen,
		uintptr(int32(x)), uintptr(int32(y)), srcCopy|captureBlt); r == 0 {
		// A secure desktop (UAC, the lock screen) refuses the copy with access denied.
		return nil, callErr("BitBlt", err)
	}
	// GDI may batch the copy; the bits are only final once the batch is flushed.
	procGdiFlush.Call()

	img := image.NewRGBA(image.Rect(0, 0, w, h))
	bgraToRGBA(img.Pix, unsafe.Slice((*byte)(bits), w*h*4))
	return img, nil
}

// Secure reads which desktop has the input and whether the session is locked. The lock comes from
// the session's own flag, not from the desktop's name: the lock screen, the UAC prompt and the
// Ctrl+Alt+Del screen all run on the same "Winlogon" desktop, so the name cannot tell them apart.
func (d *desktop) Secure(ctx context.Context) (backend.Secure, error) {
	if sessionLocked() {
		return backend.Secure{Locked: true, Reason: "lock_screen"}, nil
	}
	name, err := inputDesktopName()
	if err != nil {
		if isAccessDenied(err) {
			// Only a desktop of the secure kind refuses a program of the session even a read.
			return backend.Secure{Locked: true, Reason: "secure_desktop"}, nil
		}
		return backend.Secure{}, err
	}
	switch name {
	case "Default":
	case "Screen-saver":
		// A screen saver with a password runs on a desktop of its own.
		return backend.Secure{Locked: true, Reason: "screen_saver"}, nil
	default:
		return backend.Secure{Locked: true, Reason: "secure_desktop"}, nil
	}
	var running uint32
	if r, _, _ := procSystemParametersInfoW.Call(spiGetScreenSaverRunning, 0, uintptr(unsafe.Pointer(&running)), 0); r != 0 && running != 0 {
		return backend.Secure{Locked: true, Reason: "screen_saver"}, nil
	}
	return backend.Secure{}, nil
}

// sessionLocked reads WTS_SESSIONSTATE_LOCK from the session's WTSINFOEX. The flag's meaning is
// reversed on Windows 7, which Go no longer runs on.
func sessionLocked() bool {
	if procWTSQuerySessionInfoW.Find() != nil {
		return false
	}
	var buf *byte
	var size uint32
	r, _, _ := procWTSQuerySessionInfoW.Call(0, wtsCurrentSession, wtsSessionInfoEx,
		uintptr(unsafe.Pointer(&buf)), uintptr(unsafe.Pointer(&size)))
	if r == 0 || buf == nil {
		return false
	}
	defer windows.WTSFreeMemory(uintptr(unsafe.Pointer(buf)))
	// WTSINFOEXW is a level number and then a union; the union holds a LARGE_INTEGER and so starts
	// at offset 8, where WTSINFOEX_LEVEL1_W has SessionId, SessionState and then SessionFlags.
	const flagsOffset = 16
	if size < flagsOffset+4 {
		return false
	}
	info := unsafe.Slice(buf, size)
	level := binary.LittleEndian.Uint32(info[0:])
	flags := binary.LittleEndian.Uint32(info[flagsOffset:])
	return level == 1 && flags == wtsSessionStateLock
}

func inputDesktopName() (string, error) {
	h, _, err := procOpenInputDesktop.Call(0, 0, desktopReadObjects)
	if h == 0 {
		if errno, ok := err.(syscall.Errno); ok && errno != 0 {
			return "", errno
		}
		return "", errors.New("OpenInputDesktop failed")
	}
	defer procCloseDesktop.Call(h)
	var name [64]uint16
	var needed uint32
	if r, _, err := procGetUserObjectInformationW.Call(h, uoiName, uintptr(unsafe.Pointer(&name[0])),
		uintptr(len(name)*2), uintptr(unsafe.Pointer(&needed))); r == 0 {
		return "", callErr("GetUserObjectInformationW", err)
	}
	return windows.UTF16ToString(name[:]), nil
}

// LastInput is the system's last input of any kind, ours included. Both stamps are the 32-bit tick
// count, which wraps every 49.7 days; the unsigned subtraction stays right across the wrap.
func (d *desktop) LastInput(ctx context.Context) (time.Time, bool) {
	li := lastInputInfo{Size: uint32(unsafe.Sizeof(lastInputInfo{}))}
	if r, _, _ := procGetLastInputInfo.Call(uintptr(unsafe.Pointer(&li))); r == 0 {
		return time.Time{}, false
	}
	idle := tickCount() - li.Time
	return time.Now().Add(-time.Duration(idle) * time.Millisecond), true
}

func (d *desktop) Close() error { return nil }
