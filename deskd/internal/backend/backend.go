// Package backend is what one operating system's desktop must offer the daemon: its displays, a
// capture of a region, its windows and the apps behind them, synthetic input, and the few states that
// stop the agent (a locked screen, a missing permission, the operator's own hand on the mouse). The
// daemon's rules (the lease, its scope, the screenshot ids, the pause, the limits) live above it in
// package desk and are the same on every system; a backend only reports and obeys.
package backend

import (
	"context"
	"errors"
	"fmt"
	"image"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// The session types a backend reports.
const (
	SessionX11     = "x11"
	SessionWayland = "wayland"
	SessionQuartz  = "quartz"
	SessionWin32   = "win32"
	SessionFake    = "fake"
)

// The units desktop coordinates are in (see package geom).
const (
	UnitsPixels = "pixels"
	UnitsPoints = "points"
)

// Platform names the desktop a backend drives.
type Platform struct {
	OS          string `json:"os"`
	SessionType string `json:"session_type"`
	Units       string `json:"units"`
}

// Permission is the state of one grant the operating system keys to the program.
type Permission string

const (
	Granted       Permission = "granted"
	Denied        Permission = "denied"
	Unknown       Permission = "unknown"
	NotApplicable Permission = "not_applicable"
)

// The permissions a backend may report, by the names RequestPermission takes.
const (
	PermScreen          = "screen"
	PermAccessibility   = "accessibility"
	PermInputMonitoring = "input_monitoring"
)

// Permissions is the state of every grant computer use can need. Only macOS asks for any; elsewhere
// they are not_applicable.
type Permissions struct {
	Screen          Permission `json:"screen"`
	Accessibility   Permission `json:"accessibility"`
	InputMonitoring Permission `json:"input_monitoring"`
}

// Get is the state of the permission called which.
func (p Permissions) Get(which string) Permission {
	switch which {
	case PermScreen:
		return p.Screen
	case PermAccessibility:
		return p.Accessibility
	case PermInputMonitoring:
		return p.InputMonitoring
	}
	return Unknown
}

// App is the program behind a window, by every identity the system offers. Which fields are set
// depends on the system: BundleID on macOS, ExePath on Windows and Linux, WMClass on X11.
type App struct {
	// ID is stable for the daemon's life and unique among running apps; the host passes it back to
	// apps.focus.
	ID       string `json:"id"`
	Name     string `json:"name"`
	PID      int    `json:"pid,omitempty"`
	BundleID string `json:"bundle_id,omitempty"`
	ExePath  string `json:"exe_path,omitempty"`
	WMClass  string `json:"wm_class,omitempty"`
	// Elevated is a process at a higher integrity level than the daemon (a Windows program run as
	// administrator): synthetic input to it is silently dropped by the system, so the daemon refuses
	// it instead of reporting a click that never happened.
	Elevated bool `json:"elevated,omitempty"`
}

// Window is one top-level window.
type Window struct {
	ID string `json:"id"`
	// Title may be empty: some systems withhold titles without a permission.
	Title     string    `json:"title"`
	Bounds    geom.Rect `json:"bounds"`
	App       App       `json:"app"`
	Focused   bool      `json:"focused"`
	Minimized bool      `json:"minimized,omitempty"`
}

// Button is a mouse button.
type Button int

const (
	Left Button = iota + 1
	Middle
	Right
)

func (b Button) String() string {
	switch b {
	case Left:
		return "left"
	case Middle:
		return "middle"
	case Right:
		return "right"
	}
	return fmt.Sprintf("button%d", int(b))
}

// Secure is whether the desktop is one the agent must never see or drive.
type Secure struct {
	Locked bool `json:"locked"`
	// Reason is lock_screen, secure_desktop (Windows' UAC and Ctrl+Alt+Del) or screen_saver.
	Reason string `json:"reason,omitempty"`
}

// Backend is one desktop. Every method may be called from several goroutines; the daemon serialises
// input itself, so a backend need not.
type Backend interface {
	Platform() Platform
	// Displays lists the screens, the primary one first.
	Displays(ctx context.Context) ([]geom.Display, error)
	// Permissions reads the grants without asking for any: a probe must never raise a system prompt
	// by itself, since the operator's own flow decides when one appears.
	Permissions(ctx context.Context) Permissions
	// RequestPermission asks the system for a grant, which may show its prompt or open its settings.
	// Only the operator's role reaches it.
	RequestPermission(ctx context.Context, which string) error
	// Capture returns region (desktop units, inside display) at the display's own resolution: about
	// region.W×PixelsPerUnit pixels across.
	Capture(ctx context.Context, display geom.Display, region geom.Rect) (*image.RGBA, error)
	// Windows lists the visible top-level windows, front to back.
	Windows(ctx context.Context) ([]Window, error)
	// WindowAt is the top-level window under a point, if any.
	WindowAt(ctx context.Context, x, y float64) (Window, bool, error)
	// Foreground is the window that has the keyboard, if any.
	Foreground(ctx context.Context) (Window, bool, error)
	// Activate raises a window and gives it the keyboard.
	Activate(ctx context.Context, windowID string) error
	// Cursor is the pointer's position.
	Cursor(ctx context.Context) (float64, float64, error)
	Move(ctx context.Context, x, y float64) error
	Button(ctx context.Context, b Button, down bool) error
	// Scroll turns the wheel by notches: dy > 0 is down, dx > 0 is right.
	Scroll(ctx context.Context, dx, dy int) error
	// Key presses or releases one key; a modifier is a Key named after it ("ctrl").
	Key(ctx context.Context, k keys.Key, down bool) error
	// Type types characters, whatever the keyboard layout. The text holds no line breaks or tabs:
	// the daemon presses those as keys (keys.Split).
	Type(ctx context.Context, text string) error
	// Secure reports a locked screen or a secure desktop.
	Secure(ctx context.Context) (Secure, error)
	// LastInput is when the system last saw input from any source, ours included; ok is false where
	// it cannot tell. The daemon compares it with its own last action to notice the operator.
	LastInput(ctx context.Context) (at time.Time, ok bool)
	Close() error
}

// ErrUnsupported is an operation this backend cannot do here.
var ErrUnsupported = errors.New("not supported by this desktop")

// PermissionError is an operation the system refused for want of a grant. Which names it in the
// words of Permissions.
type PermissionError struct {
	Which  string
	Detail string
}

func (e *PermissionError) Error() string {
	msg := "the system has not granted the " + e.Which + " permission"
	if e.Detail != "" {
		msg += ": " + e.Detail
	}
	return msg
}

// UnreachableError is a window the system will not let synthetic input reach (Windows' UIPI in front
// of an elevated program).
type UnreachableError struct{ Reason string }

func (e *UnreachableError) Error() string { return "the window cannot be reached: " + e.Reason }

// ErrNoBackend is a system with no backend in this build.
var ErrNoBackend = errors.New("this build has no desktop backend for this system")
