//go:build windows

package win

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

var (
	windowOnce     sync.Once
	windowCallback uintptr
	windowMu       sync.Mutex
	windowFound    []windows.HWND
)

// enumWindows lists the top-level windows in z-order, front first, which is the order EnumWindows
// documents. See enumMonitors for why the callback is made once.
func enumWindows() ([]windows.HWND, error) {
	windowOnce.Do(func() {
		windowCallback = windows.NewCallback(func(hwnd windows.HWND, data uintptr) uintptr {
			windowFound = append(windowFound, hwnd)
			return 1
		})
	})
	windowMu.Lock()
	defer windowMu.Unlock()
	windowFound = nil
	if r, _, err := procEnumWindows.Call(windowCallback, 0); r == 0 {
		return nil, callErr("EnumWindows", err)
	}
	found := windowFound
	windowFound = nil
	return found, nil
}

func (d *desktop) Windows(ctx context.Context) ([]backend.Window, error) {
	hwnds, err := enumWindows()
	if err != nil {
		return nil, err
	}
	fg := foregroundRoot()
	var out []backend.Window
	for _, hwnd := range hwnds {
		if !windows.IsWindowVisible(hwnd) || cloaked(hwnd) {
			continue
		}
		// A tool window (a floating palette, a tray program's hidden helper) is not one a person
		// switches to; Alt+Tab leaves it out for the same reason.
		index := int32(gwlExStyle) // negative, so it goes through a variable to become a uintptr
		if ex, _, _ := procGetWindowLongW.Call(uintptr(hwnd), uintptr(index)); uint32(ex)&wsExToolWindow != 0 {
			continue
		}
		w := d.describe(hwnd, fg)
		if w.Bounds.Empty() && !w.Minimized {
			continue
		}
		out = append(out, w)
	}
	return out, nil
}

// cloaked reports a window DWM keeps off the screen although it counts as visible: a suspended
// Store app, a window on another virtual desktop. Listing those offers the agent windows it can
// neither see nor click.
func cloaked(hwnd windows.HWND) bool {
	if procDwmGetWindowAttribute.Find() != nil {
		return false
	}
	var v uint32
	hr, _, _ := procDwmGetWindowAttribute.Call(uintptr(hwnd), dwmwaCloaked, uintptr(unsafe.Pointer(&v)), unsafe.Sizeof(v))
	return hr == 0 && v != 0
}

// windowBounds prefers DWM's extended frame bounds: GetWindowRect includes the invisible resize
// border Windows 10 and 11 put around every window, seven pixels or so at 100 %, so a capture of the
// window would carry a strip of whatever is behind it and a click at its edge would miss.
func windowBounds(hwnd windows.HWND) geom.Rect {
	var r windows.Rect
	ok := false
	if procDwmGetWindowAttribute.Find() == nil {
		hr, _, _ := procDwmGetWindowAttribute.Call(uintptr(hwnd), dwmwaExtendedFrm, uintptr(unsafe.Pointer(&r)), unsafe.Sizeof(r))
		ok = hr == 0
	}
	if !ok {
		if ret, _, _ := procGetWindowRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&r))); ret == 0 {
			return geom.Rect{}
		}
	}
	return geom.Rect{X: float64(r.Left), Y: float64(r.Top), W: float64(r.Right - r.Left), H: float64(r.Bottom - r.Top)}
}

// windowTitle reads the caption. For a window of another process GetWindowTextW reads the copy the
// system keeps rather than sending WM_GETTEXT, so a hung program cannot stall the listing.
func windowTitle(hwnd windows.HWND) string {
	n, _, _ := procGetWindowTextLengthW.Call(uintptr(hwnd))
	if n == 0 {
		return ""
	}
	buf := make([]uint16, n+1)
	got, _, _ := procGetWindowTextW.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	return windows.UTF16ToString(buf[:got])
}

func (d *desktop) describe(hwnd, fg windows.HWND) backend.Window {
	var pid uint32
	windows.GetWindowThreadProcessId(hwnd, &pid)
	return backend.Window{
		ID:        windowID(hwnd),
		Title:     windowTitle(hwnd),
		Bounds:    windowBounds(hwnd),
		App:       d.appOf(pid),
		Focused:   hwnd == fg,
		Minimized: isIconic(hwnd),
	}
}

func windowID(hwnd windows.HWND) string { return fmt.Sprintf("0x%x", uintptr(hwnd)) }

func parseWindowID(id string) (windows.HWND, error) {
	v, err := strconv.ParseUint(strings.TrimPrefix(id, "0x"), 16, 64)
	if err != nil || !strings.HasPrefix(id, "0x") {
		return 0, fmt.Errorf("%q is not a window id", id)
	}
	hwnd := windows.HWND(uintptr(v))
	if !windows.IsWindow(hwnd) {
		return 0, fmt.Errorf("window %s no longer exists", id)
	}
	return hwnd, nil
}

// foregroundRoot is the top-level window with the keyboard, or 0 while the system is between two
// (an activation in progress, a secure desktop up).
func foregroundRoot() windows.HWND {
	fg := windows.GetForegroundWindow()
	if fg == 0 {
		return 0
	}
	if root := getAncestor(fg, gaRoot); root != 0 {
		return root
	}
	return fg
}

func (d *desktop) WindowAt(ctx context.Context, x, y float64) (backend.Window, bool, error) {
	hwnd := windowFromPoint(int32(geom.Snap(x)), int32(geom.Snap(y)))
	if hwnd == 0 {
		return backend.Window{}, false, nil
	}
	// WindowFromPoint answers with the innermost child (a button, an edit box); the agent deals in
	// top-level windows.
	if root := getAncestor(hwnd, gaRoot); root != 0 {
		hwnd = root
	}
	return d.describe(hwnd, foregroundRoot()), true, nil
}

func (d *desktop) Foreground(ctx context.Context) (backend.Window, bool, error) {
	fg := foregroundRoot()
	if fg == 0 {
		return backend.Window{}, false, nil
	}
	return d.describe(fg, fg), true, nil
}

// Activate restores and raises a window. Windows' foreground lock refuses SetForegroundWindow to a
// process that did not receive the last input, which a background daemon never did, and the refusal
// only flashes the taskbar button. The system lifts the lock while Alt is held (so that Alt+Tab
// works), so the daemon holds Alt across the call. Alt pressed and released alone would put the
// window that gets the release into menu mode, where the next key the agent types selects a menu
// instead of reaching the document, so an unassigned virtual key is tapped in between: any key
// pressed inside an Alt chord cancels menu mode, and an unassigned one does nothing else.
func (d *desktop) Activate(ctx context.Context, windowID string) error {
	hwnd, err := parseWindowID(windowID)
	if err != nil {
		return err
	}
	if isIconic(hwnd) {
		procShowWindow.Call(uintptr(hwnd), swRestore)
	}
	if r, _, _ := procSetForegroundWindow.Call(uintptr(hwnd)); r != 0 && foregroundRoot() == hwnd {
		return nil
	}
	if err := d.withAltHeld(func() { procSetForegroundWindow.Call(uintptr(hwnd)) }); err != nil {
		return fmt.Errorf("activating window %s: %w", windowID, err)
	}
	// The switch is processed by the target's thread; give it a moment before calling it refused.
	deadline := time.Now().Add(200 * time.Millisecond)
	for foregroundRoot() != hwnd {
		if time.Now().After(deadline) {
			return errors.New("the system refused to bring window " + windowID + " to the front (foreground lock)")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
	return nil
}

// cachedApp is a process's identity as last read. Reading it opens the process and its token, a few
// system calls per window, and a listing of thirty windows from five programs would repeat each
// five-fold; a pid is not reused within the few seconds the cache keeps it.
type cachedApp struct {
	app backend.App
	at  time.Time
}

const appCacheFor = 5 * time.Second

func (d *desktop) appOf(pid uint32) backend.App {
	d.mu.Lock()
	c, ok := d.apps[pid]
	d.mu.Unlock()
	if ok && time.Since(c.at) < appCacheFor {
		return c.app
	}
	app := readApp(pid)
	d.mu.Lock()
	now := time.Now()
	for p, old := range d.apps {
		if now.Sub(old.at) >= appCacheFor {
			delete(d.apps, p)
		}
	}
	d.apps[pid] = cachedApp{app: app, at: now}
	d.mu.Unlock()
	return app
}

func readApp(pid uint32) backend.App {
	app := backend.App{ID: fmt.Sprintf("pid:%d", pid), PID: int(pid), Name: fmt.Sprintf("pid %d", pid)}
	if pid == windows.GetCurrentProcessId() {
		app.ExePath, _ = ownExe()
		if app.ExePath != "" {
			app.Name = appName(app.ExePath)
		}
		return app
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		// Limited query rights are granted for nearly every process of the session, elevated ones
		// included; a refusal means one guarded above us (a protected or system process), and
		// input to it is as dropped as to an elevated one.
		app.Elevated = isAccessDenied(err)
		return app
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, windows.MAX_LONG_PATH)
	size := uint32(len(buf))
	if windows.QueryFullProcessImageName(h, 0, &buf[0], &size) == nil {
		app.ExePath = windows.UTF16ToString(buf[:size])
		app.Name = appName(app.ExePath)
	}
	app.Elevated = elevated(h)
	return app
}

func ownExe() (string, error) {
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n, err := windows.GetModuleFileName(0, &buf[0], uint32(len(buf)))
	if err != nil {
		return "", err
	}
	return windows.UTF16ToString(buf[:n]), nil
}

// elevated compares the process's integrity level with ours. The token of an elevated process
// refuses a query from a medium-level one, and that refusal is exactly the boundary that drops our
// input (user interface privilege isolation), so it counts as elevated rather than as unknown.
func elevated(process windows.Handle) bool {
	var token windows.Token
	if err := windows.OpenProcessToken(process, windows.TOKEN_QUERY, &token); err != nil {
		return isAccessDenied(err)
	}
	defer token.Close()
	theirs, err := integrityLevel(token)
	if err != nil {
		return false
	}
	ours, err := ownIntegrity()
	if err != nil {
		return false
	}
	return moreTrusted(theirs, ours)
}

var ownIntegrity = sync.OnceValues(func() (uint32, error) {
	return integrityLevel(windows.GetCurrentProcessToken())
})

// integrityLevel is the relative id of the token's mandatory label: 0x2000 medium, 0x3000 high.
func integrityLevel(token windows.Token) (uint32, error) {
	var n uint32
	err := windows.GetTokenInformation(token, windows.TokenIntegrityLevel, nil, 0, &n)
	if n == 0 {
		return 0, fmt.Errorf("reading the integrity level: %w", err)
	}
	buf := make([]byte, n)
	if err := windows.GetTokenInformation(token, windows.TokenIntegrityLevel, &buf[0], n, &n); err != nil {
		return 0, fmt.Errorf("reading the integrity level: %w", err)
	}
	label := (*windows.Tokenmandatorylabel)(unsafe.Pointer(&buf[0]))
	sid := label.Label.Sid
	count := sid.SubAuthorityCount()
	if count == 0 {
		return 0, errors.New("the integrity label has no level")
	}
	return sid.SubAuthority(uint32(count) - 1), nil
}
