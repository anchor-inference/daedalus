package win

// This file holds the arithmetic of the Windows backend that needs no system call: the virtual-key
// table, the wheel encoding, the pixel conversion and the integrity comparison. It carries no build
// tag so that its test runs on any machine; nothing in it imports a Windows package.

import (
	"strings"

	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// The virtual-key codes the table uses, as winuser.h names them.
const (
	vkBack     = 0x08
	vkTab      = 0x09
	vkReturn   = 0x0D
	vkShift    = 0x10
	vkControl  = 0x11
	vkMenu     = 0x12
	vkCapital  = 0x14
	vkEscape   = 0x1B
	vkSpace    = 0x20
	vkPrior    = 0x21
	vkNext     = 0x22
	vkEnd      = 0x23
	vkHome     = 0x24
	vkLeft     = 0x25
	vkUp       = 0x26
	vkRight    = 0x27
	vkDown     = 0x28
	vkSnapshot = 0x2C
	vkInsert   = 0x2D
	vkDelete   = 0x2E
	vkLWin     = 0x5B
	vkApps     = 0x5D
	vkF1       = 0x70
)

// vkCode is one key as SendInput takes it: the virtual key and whether its scan code carries the E0
// prefix. The flag is not decoration: without KEYEVENTF_EXTENDEDKEY the arrow and navigation keys
// arrive as their numeric-keypad twins, so with Num Lock on a "left" types a 4, and the Windows key
// and the menu key are not recognised at all.
type vkCode struct {
	vk       uint16
	extended bool
}

var namedVK = map[string]vkCode{
	keys.Enter:     {vkReturn, false},
	keys.Tab:       {vkTab, false},
	keys.Escape:    {vkEscape, false},
	keys.Backspace: {vkBack, false},
	keys.Delete:    {vkDelete, true},
	keys.Insert:    {vkInsert, true},
	keys.Home:      {vkHome, true},
	keys.End:       {vkEnd, true},
	keys.PageUp:    {vkPrior, true},
	keys.PageDown:  {vkNext, true},
	keys.Up:        {vkUp, true},
	keys.Down:      {vkDown, true},
	keys.Left:      {vkLeft, true},
	keys.Right:     {vkRight, true},
	keys.Space:     {vkSpace, false},
	keys.CapsLock:  {vkCapital, false},
	keys.PrintScr:  {vkSnapshot, true},
	keys.Menu:      {vkApps, true},
	// The modifiers are the left-hand keys, which carry no prefix; the Windows key is the exception,
	// its scan code is E0 5B on every keyboard.
	string(keys.Ctrl):  {vkControl, false},
	string(keys.Alt):   {vkMenu, false},
	string(keys.Shift): {vkShift, false},
	string(keys.Super): {vkLWin, true},
}

// namedKey is the virtual key of a named key, and whether there is one.
func namedKey(k keys.Key) (vkCode, bool) {
	if n, ok := k.IsF(); ok {
		return vkCode{vk: uint16(vkF1 + n - 1)}, true
	}
	c, ok := namedVK[k.Name]
	return c, ok
}

// wheelDelta is one notch of the wheel as WM_MOUSEWHEEL counts it.
const wheelDelta = 120

// wheelData is the mouseData of the wheel events for a scroll of n notches. Windows' vertical wheel is
// positive away from the user, that is up, while the daemon's dy is positive down, so the vertical
// sign flips; the horizontal wheel is positive right, as the daemon's dx is. Each notch is its own
// event: a single event of five notches is honoured by most programs but some (older Office, a few
// games) take any delta as one notch.
func wheelData(n int, vertical bool) []int32 {
	step := int32(wheelDelta)
	if n < 0 {
		step, n = -step, -n
	}
	if vertical {
		step = -step
	}
	out := make([]int32, n)
	for i := range out {
		out[i] = step
	}
	return out
}

// bgraToRGBA converts a top-down 32-bit device-independent bitmap, whose bytes are blue, green, red
// and an unused fourth, into the bytes of an image.RGBA. The fourth byte is set opaque: GDI leaves it
// undefined (mostly zero), and a picture encoded with it would come out fully transparent.
func bgraToRGBA(dst, src []byte) {
	n := min(len(dst), len(src)) / 4 * 4
	for i := 0; i < n; i += 4 {
		dst[i+0] = src[i+2]
		dst[i+1] = src[i+1]
		dst[i+2] = src[i+0]
		dst[i+3] = 0xFF
	}
}

// moreTrusted reports whether a process at integrity level target outranks one at ours, which is the
// case where Windows' user interface privilege isolation drops our synthetic input to it. Equal
// levels are fine, and so is a lower one (we may drive a sandboxed browser tab's window).
func moreTrusted(target, ours uint32) bool { return target > ours }

// appName is the name a person knows a program by: the executable's base name without ".exe".
func appName(exePath string) string {
	base := exePath[strings.LastIndexAny(exePath, `\/`)+1:]
	if len(base) > 4 && strings.EqualFold(base[len(base)-4:], ".exe") {
		base = base[:len(base)-4]
	}
	return base
}
