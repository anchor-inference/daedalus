//go:build windows

package win

import (
	"errors"
	"unsafe"

	"golang.org/x/sys/windows"
)

// The procedures are resolved lazily, as ptyd does, so that a missing one (shcore on an old build, a
// newer user32 export) fails the call that needs it rather than the whole program at start.
var (
	user32   = windows.NewLazySystemDLL("user32.dll")
	gdi32    = windows.NewLazySystemDLL("gdi32.dll")
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")
	shcore   = windows.NewLazySystemDLL("shcore.dll")
	dwmapi   = windows.NewLazySystemDLL("dwmapi.dll")
	wtsapi32 = windows.NewLazySystemDLL("wtsapi32.dll")

	procSetProcessDpiAwarenessContext = user32.NewProc("SetProcessDpiAwarenessContext")
	procSetProcessDPIAware            = user32.NewProc("SetProcessDPIAware")
	procSetProcessDpiAwareness        = shcore.NewProc("SetProcessDpiAwareness")
	procGetDpiForMonitor              = shcore.NewProc("GetDpiForMonitor")

	procEnumDisplayMonitors = user32.NewProc("EnumDisplayMonitors")
	procGetMonitorInfoW     = user32.NewProc("GetMonitorInfoW")
	procGetSystemMetrics    = user32.NewProc("GetSystemMetrics")

	procGetDC              = user32.NewProc("GetDC")
	procReleaseDC          = user32.NewProc("ReleaseDC")
	procCreateCompatibleDC = gdi32.NewProc("CreateCompatibleDC")
	procDeleteDC           = gdi32.NewProc("DeleteDC")
	procCreateDIBSection   = gdi32.NewProc("CreateDIBSection")
	procSelectObject       = gdi32.NewProc("SelectObject")
	procDeleteObject       = gdi32.NewProc("DeleteObject")
	procBitBlt             = gdi32.NewProc("BitBlt")
	procGdiFlush           = gdi32.NewProc("GdiFlush")

	procEnumWindows           = user32.NewProc("EnumWindows")
	procGetWindowTextW        = user32.NewProc("GetWindowTextW")
	procGetWindowTextLengthW  = user32.NewProc("GetWindowTextLengthW")
	procGetWindowRect         = user32.NewProc("GetWindowRect")
	procGetWindowLongW        = user32.NewProc("GetWindowLongW")
	procIsIconic              = user32.NewProc("IsIconic")
	procWindowFromPoint       = user32.NewProc("WindowFromPoint")
	procGetAncestor           = user32.NewProc("GetAncestor")
	procShowWindow            = user32.NewProc("ShowWindow")
	procSetForegroundWindow   = user32.NewProc("SetForegroundWindow")
	procDwmGetWindowAttribute = dwmapi.NewProc("DwmGetWindowAttribute")

	procGetCursorPos     = user32.NewProc("GetCursorPos")
	procSetCursorPos     = user32.NewProc("SetCursorPos")
	procSendInput        = user32.NewProc("SendInput")
	procVkKeyScanExW     = user32.NewProc("VkKeyScanExW")
	procMapVirtualKeyW   = user32.NewProc("MapVirtualKeyW")
	procGetLastInputInfo = user32.NewProc("GetLastInputInfo")
	procGetTickCount     = kernel32.NewProc("GetTickCount")

	procOpenInputDesktop          = user32.NewProc("OpenInputDesktop")
	procCloseDesktop              = user32.NewProc("CloseDesktop")
	procGetUserObjectInformationW = user32.NewProc("GetUserObjectInformationW")
	procSystemParametersInfoW     = user32.NewProc("SystemParametersInfoW")
	procWTSQuerySessionInfoW      = wtsapi32.NewProc("WTSQuerySessionInformationW")
)

const (
	// dpiAwarenessContextPerMonitorV2 is DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, the handle -4.
	dpiAwarenessContextPerMonitorV2 = ^uintptr(3)
	processPerMonitorDpiAware       = 2
	mdtEffectiveDpi                 = 0
	monitorInfoFPrimary             = 1

	smXVirtualScreen  = 76
	smYVirtualScreen  = 77
	smCXVirtualScreen = 78
	smCYVirtualScreen = 79

	biRGB       = 0
	dibRGBColor = 0
	srcCopy     = 0x00CC0020
	// captureBlt includes layered windows (tooltips, menus, most modern programs' pop-ups) in the
	// copy; without it they are missing from the picture although the operator sees them.
	captureBlt = 0x40000000

	gwlExStyle       = -20
	wsExToolWindow   = 0x00000080
	gaRoot           = 2
	swRestore        = 9
	dwmwaExtendedFrm = 9
	dwmwaCloaked     = 14

	inputMouse    = 0
	inputKeyboard = 1

	mouseeventfMove        = 0x0001
	mouseeventfLeftDown    = 0x0002
	mouseeventfLeftUp      = 0x0004
	mouseeventfRightDown   = 0x0008
	mouseeventfRightUp     = 0x0010
	mouseeventfMiddleDown  = 0x0020
	mouseeventfMiddleUp    = 0x0040
	mouseeventfWheel       = 0x0800
	mouseeventfHWheel      = 0x1000
	mouseeventfVirtualDesk = 0x4000
	mouseeventfAbsolute    = 0x8000

	keyeventfExtendedKey = 0x0001
	keyeventfKeyUp       = 0x0002
	keyeventfUnicode     = 0x0004

	mapvkVKToVSC = 0

	desktopReadObjects       = 0x0001
	uoiName                  = 2
	spiGetScreenSaverRunning = 0x0072

	wtsCurrentSession   = 0xFFFFFFFF
	wtsSessionInfoEx    = 25
	wtsSessionStateLock = 0
)

type point struct{ X, Y int32 }

type monitorInfoEx struct {
	Size    uint32
	Monitor windows.Rect
	Work    windows.Rect
	Flags   uint32
	Device  [32]uint16
}

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

// bitmapInfo is BITMAPINFO with its colour table, which a 32-bit BI_RGB bitmap does not read but the
// structure still has room for.
type bitmapInfo struct {
	Header bitmapInfoHeader
	Colors [1]uint32
}

type mouseInput struct {
	Dx, Dy    int32
	MouseData uint32
	Flags     uint32
	Time      uint32
	ExtraInfo uintptr
}

type keybdInput struct {
	Vk        uint16
	Scan      uint16
	Flags     uint32
	Time      uint32
	ExtraInfo uintptr
}

// mouseINPUT and keyINPUT are the two shapes of INPUT. The C union is as large as its largest member,
// MOUSEINPUT, which is eight bytes longer than KEYBDINPUT on both 32- and 64-bit Windows; SendInput
// refuses the whole array when cbSize is not the size it expects, so the keyboard shape is padded and
// the two sizes are pinned equal at compile time below.
type mouseINPUT struct {
	Type uint32
	Mi   mouseInput
}

type keyINPUT struct {
	Type uint32
	Ki   keybdInput
	_    [8]byte
}

// Either subtraction underflows, a compile error, unless the two sizes are equal.
var (
	_ [unsafe.Sizeof(mouseINPUT{}) - unsafe.Sizeof(keyINPUT{})]byte
	_ [unsafe.Sizeof(keyINPUT{}) - unsafe.Sizeof(mouseINPUT{})]byte
)

type lastInputInfo struct {
	Size uint32
	Time uint32
}

// isAccessDenied reports the refusal that means the target sits behind a privilege boundary.
func isAccessDenied(err error) bool {
	return errors.Is(err, windows.ERROR_ACCESS_DENIED)
}

// callErr turns the error a failed lazy call returned into one worth reporting: a call that failed
// without setting the last error comes back as Errno 0, "The operation completed successfully".
func callErr(name string, err error) error {
	var errno windows.Errno
	if errors.As(err, &errno) && errno == 0 {
		return errors.New(name + " failed")
	}
	return errors.New(name + ": " + err.Error())
}

func getSystemMetrics(index int) int32 {
	r, _, _ := procGetSystemMetrics.Call(uintptr(index))
	return int32(r)
}

// windowFromPoint passes POINT by value, which the calling conventions spell differently: one
// register holding both halves on amd64 and arm64, two stack words on 386.
func windowFromPoint(x, y int32) windows.HWND {
	var r uintptr
	if unsafe.Sizeof(uintptr(0)) == 8 {
		r, _, _ = procWindowFromPoint.Call(uintptr(uint32(x)) | uintptr(uint32(y))<<16<<16)
	} else {
		r, _, _ = procWindowFromPoint.Call(uintptr(uint32(x)), uintptr(uint32(y)))
	}
	return windows.HWND(r)
}

func getAncestor(hwnd windows.HWND, flags uint32) windows.HWND {
	r, _, _ := procGetAncestor.Call(uintptr(hwnd), uintptr(flags))
	return windows.HWND(r)
}

func isIconic(hwnd windows.HWND) bool {
	r, _, _ := procIsIconic.Call(uintptr(hwnd))
	return r != 0
}

func getCursorPos() (point, error) {
	var p point
	if r, _, err := procGetCursorPos.Call(uintptr(unsafe.Pointer(&p))); r == 0 {
		return p, callErr("GetCursorPos", err)
	}
	return p, nil
}

// tickCount is the 32-bit millisecond counter, the same clock GetLastInputInfo stamps with. The
// 64-bit counter would be returned in two registers on 386, and the comparison is only ever with a
// 32-bit stamp anyway.
func tickCount() uint32 {
	r, _, _ := procGetTickCount.Call()
	return uint32(r)
}
