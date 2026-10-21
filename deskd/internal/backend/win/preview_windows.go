//go:build windows

package win

import (
	"context"
	"errors"
	"image"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

var (
	procPrintWindow                 = user32.NewProc("PrintWindow")
	procGetDisplayConfigBufferSizes = user32.NewProc("GetDisplayConfigBufferSizes")
	procQueryDisplayConfig          = user32.NewProc("QueryDisplayConfig")
	procDisplayConfigGetDeviceInfo  = user32.NewProc("DisplayConfigGetDeviceInfo")
)

const (
	// pwRenderFullContent asks PrintWindow for what DWM composes, which is the only way a window
	// drawn with DirectX or by a browser engine comes out as anything but black.
	pwRenderFullContent = 2

	qdcOnlyActivePaths        = 2
	displayConfigGetSource    = 1
	displayConfigGetTarget    = 2
	errorInsufficientBuffer   = 122
	maxDisplayConfigAttempts  = 4
	displayConfigPathInfoSize = 72
	displayConfigModeInfoSize = 64
)

// CaptureWindow draws the window into a bitmap of its own with PrintWindow, so a window partly
// covered by another still shows whole in the operator's preview. PrintWindow draws GetWindowRect,
// which includes the invisible resize border; the picture is cut to the window's visible bounds
// (DWM's extended frame), the same rectangle the screen capture of a window uses.
func (d *desktop) CaptureWindow(ctx context.Context, w backend.Window) (*image.RGBA, error) {
	hwnd, err := parseWindowID(w.ID)
	if err != nil {
		return nil, err
	}
	if isIconic(hwnd) {
		return nil, errors.New("a minimised window has nothing to draw")
	}
	var full windows.Rect
	if r, _, err := procGetWindowRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&full))); r == 0 {
		return nil, callErr("GetWindowRect", err)
	}
	fw, fh := int(full.Right-full.Left), int(full.Bottom-full.Top)
	if fw <= 0 || fh <= 0 {
		return nil, errors.New("the window has no area")
	}
	img, err := withDIB(fw, fh, func(screen, mem uintptr) error {
		if r, _, err := procPrintWindow.Call(uintptr(hwnd), mem, pwRenderFullContent); r == 0 {
			return callErr("PrintWindow", err)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	// A program that ignores WM_PRINT and is not composed by DWM leaves the bitmap untouched, all
	// zeros; the screen region is then the better picture, so this reports a failure to fall back on.
	if blank(img) {
		return nil, errors.New("the window drew nothing")
	}
	visible := windowBounds(hwnd)
	crop := image.Rect(int(visible.X)-int(full.Left), int(visible.Y)-int(full.Top),
		int(visible.X+visible.W)-int(full.Left), int(visible.Y+visible.H)-int(full.Top)).Intersect(img.Bounds())
	if crop.Empty() {
		return img, nil
	}
	out := image.NewRGBA(image.Rect(0, 0, crop.Dx(), crop.Dy()))
	for y := 0; y < crop.Dy(); y++ {
		src := img.PixOffset(crop.Min.X, crop.Min.Y+y)
		copy(out.Pix[y*out.Stride:y*out.Stride+crop.Dx()*4], img.Pix[src:src+crop.Dx()*4])
	}
	return out, nil
}

// blank reports a picture whose every colour byte is zero. The alpha bytes are ignored: bgraToRGBA
// sets them opaque.
func blank(img *image.RGBA) bool {
	for i := 0; i+3 < len(img.Pix); i += 4 {
		if img.Pix[i] != 0 || img.Pix[i+1] != 0 || img.Pix[i+2] != 0 {
			return false
		}
	}
	return true
}

type luid struct {
	Low  uint32
	High int32
}

type displayConfigPathSourceInfo struct {
	Adapter     luid
	ID          uint32
	ModeIndex   uint32
	StatusFlags uint32
}

type displayConfigPathTargetInfo struct {
	Adapter          luid
	ID               uint32
	ModeIndex        uint32
	OutputTechnology uint32
	Rotation         uint32
	Scaling          uint32
	RefreshNum       uint32
	RefreshDen       uint32
	ScanLineOrdering uint32
	TargetAvailable  int32
	StatusFlags      uint32
}

type displayConfigPathInfo struct {
	Source displayConfigPathSourceInfo
	Target displayConfigPathTargetInfo
	Flags  uint32
}

// displayConfigModeInfo is only ever handed to QueryDisplayConfig to fill; nothing reads it, so its
// union is left as bytes. The eight-byte alignment comes from the union's 64-bit pixel rate.
type displayConfigModeInfo struct {
	_ [displayConfigModeInfoSize / 8]uint64
}

type displayConfigDeviceInfoHeader struct {
	Type    uint32
	Size    uint32
	Adapter luid
	ID      uint32
}

type displayConfigSourceDeviceName struct {
	Header        displayConfigDeviceInfoHeader
	GDIDeviceName [32]uint16
}

type displayConfigTargetDeviceName struct {
	Header            displayConfigDeviceInfoHeader
	Flags             uint32
	OutputTechnology  uint32
	EDIDManufactureID uint16
	EDIDProductCodeID uint16
	ConnectorInstance uint32
	FriendlyName      [64]uint16
	DevicePath        [128]uint16
}

// The structures must have the sizes user32 expects, or every call fails with invalid parameter;
// either subtraction underflows, a compile error, unless the size is right.
var (
	_ [unsafe.Sizeof(displayConfigPathInfo{}) - displayConfigPathInfoSize]byte
	_ [displayConfigPathInfoSize - unsafe.Sizeof(displayConfigPathInfo{})]byte
	_ [unsafe.Sizeof(displayConfigTargetDeviceName{}) - 420]byte
	_ [420 - unsafe.Sizeof(displayConfigTargetDeviceName{})]byte
	_ [unsafe.Sizeof(displayConfigSourceDeviceName{}) - 84]byte
	_ [84 - unsafe.Sizeof(displayConfigSourceDeviceName{})]byte
)

// monitorNames maps each active output's GDI device name ("\\.\DISPLAY1") to the monitor's own name
// from its EDID ("DELL U2720Q"), which is what Windows' display settings show. Any failure gives an
// empty map: a name is a courtesy, and the display then goes by its number.
func monitorNames() map[string]string {
	names := map[string]string{}
	if procQueryDisplayConfig.Find() != nil || procDisplayConfigGetDeviceInfo.Find() != nil {
		return names
	}
	var paths []displayConfigPathInfo
	for attempt := 0; attempt < maxDisplayConfigAttempts; attempt++ {
		var np, nm uint32
		if r, _, _ := procGetDisplayConfigBufferSizes.Call(qdcOnlyActivePaths, uintptr(unsafe.Pointer(&np)),
			uintptr(unsafe.Pointer(&nm))); r != 0 || np == 0 {
			return names
		}
		p := make([]displayConfigPathInfo, np)
		m := make([]displayConfigModeInfo, max(nm, 1))
		r, _, _ := procQueryDisplayConfig.Call(qdcOnlyActivePaths, uintptr(unsafe.Pointer(&np)), uintptr(unsafe.Pointer(&p[0])),
			uintptr(unsafe.Pointer(&nm)), uintptr(unsafe.Pointer(&m[0])), 0)
		if r == errorInsufficientBuffer {
			// A display came or went between the two calls.
			continue
		}
		if r != 0 {
			return names
		}
		paths = p[:np]
		break
	}
	for _, path := range paths {
		src := displayConfigSourceDeviceName{Header: displayConfigDeviceInfoHeader{Type: displayConfigGetSource,
			Size: uint32(unsafe.Sizeof(displayConfigSourceDeviceName{})), Adapter: path.Source.Adapter, ID: path.Source.ID}}
		if r, _, _ := procDisplayConfigGetDeviceInfo.Call(uintptr(unsafe.Pointer(&src))); r != 0 {
			continue
		}
		dst := displayConfigTargetDeviceName{Header: displayConfigDeviceInfoHeader{Type: displayConfigGetTarget,
			Size: uint32(unsafe.Sizeof(displayConfigTargetDeviceName{})), Adapter: path.Target.Adapter, ID: path.Target.ID}}
		if r, _, _ := procDisplayConfigGetDeviceInfo.Call(uintptr(unsafe.Pointer(&dst))); r != 0 {
			continue
		}
		gdi := windows.UTF16ToString(src.GDIDeviceName[:])
		// A mirrored pair shares one source; the first monitor named keeps it.
		if _, seen := names[gdi]; !seen {
			names[gdi] = windows.UTF16ToString(dst.FriendlyName[:])
		}
	}
	return names
}
