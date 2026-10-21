package quartz

import (
	"fmt"
	"unicode"

	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// The modifier bits of CGEventFlags (kCGEventFlagMask*). They are set on every key and mouse event
// we post, because an application reads the chord from the event's flags rather than from the keys
// it saw go down: a Command key press followed by a "c" whose flags lack the Command bit is a plain
// "c" to most of them.
const (
	flagShift   uint64 = 0x00020000
	flagControl uint64 = 0x00040000
	flagOption  uint64 = 0x00080000
	flagCommand uint64 = 0x00100000
)

var modFlags = map[keys.Mod]uint64{
	keys.Ctrl:  flagControl,
	keys.Alt:   flagOption,
	keys.Shift: flagShift,
	keys.Super: flagCommand,
}

// The virtual key codes of the left-hand modifiers (kVK_Control, kVK_Option, kVK_Shift, kVK_Command).
var modCodes = map[keys.Mod]uint16{
	keys.Ctrl:  0x3B,
	keys.Alt:   0x3A,
	keys.Shift: 0x38,
	keys.Super: 0x37,
}

// runeCodes is the US ANSI layout's character keys (kVK_ANSI_* in HIToolbox's Events.h). Virtual key
// codes name positions, not characters, so on another layout a code types that layout's character;
// that is why text goes through Type, which does not depend on the layout, and keys through here.
var runeCodes = map[rune]uint16{
	'a': 0x00, 's': 0x01, 'd': 0x02, 'f': 0x03, 'h': 0x04, 'g': 0x05, 'z': 0x06, 'x': 0x07,
	'c': 0x08, 'v': 0x09, 'b': 0x0B, 'q': 0x0C, 'w': 0x0D, 'e': 0x0E, 'r': 0x0F, 'y': 0x10,
	't': 0x11, '1': 0x12, '2': 0x13, '3': 0x14, '4': 0x15, '6': 0x16, '5': 0x17, '=': 0x18,
	'9': 0x19, '7': 0x1A, '-': 0x1B, '8': 0x1C, '0': 0x1D, ']': 0x1E, 'o': 0x1F, 'u': 0x20,
	'[': 0x21, 'i': 0x22, 'p': 0x23, 'l': 0x25, 'j': 0x26, '\'': 0x27, 'k': 0x28, ';': 0x29,
	'\\': 0x2A, ',': 0x2B, '/': 0x2C, 'n': 0x2D, 'm': 0x2E, '.': 0x2F, '`': 0x32,
}

// shiftedRunes is the character each US key gives with Shift. A key event for "!" is the "1" key
// with the Shift bit in its flags, which the layout turns into "!" as it would for a person.
var shiftedRunes = map[rune]rune{
	'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
	'_': '-', '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';', '"': '\'', '<': ',', '>': '.', '?': '/',
	'~': '`',
}

var namedCodes = map[string]uint16{
	keys.Enter:     0x24, // kVK_Return
	keys.Tab:       0x30,
	keys.Space:     0x31,
	keys.Backspace: 0x33, // kVK_Delete: the key a Mac labels "delete" erases backwards
	keys.Escape:    0x35,
	keys.CapsLock:  0x39,
	keys.Delete:    0x75, // kVK_ForwardDelete
	// A Mac keyboard has no Insert; the key in its place on full-size keyboards is Help, and a PC
	// keyboard's Insert arrives as Help too.
	keys.Insert:   0x72,
	keys.Home:     0x73,
	keys.End:      0x77,
	keys.PageUp:   0x74,
	keys.PageDown: 0x79,
	keys.Left:     0x7B,
	keys.Right:    0x7C,
	keys.Down:     0x7D,
	keys.Up:       0x7E,
	keys.Menu:     0x6E, // kVK_ContextualMenu
}

// fCodes is F1 to F20; macOS has no F21 to F24.
var fCodes = [20]uint16{
	0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D,
	0x67, 0x6F, 0x69, 0x6B, 0x71, 0x6A, 0x40, 0x4F, 0x50, 0x5A,
}

// keyStroke is how one keys.Key is pressed on macOS.
type keyStroke struct {
	code uint16
	// implied is flags the key needs on top of the held ones: Shift for "!" or "A".
	implied uint64
	// mod is the modifier the key is, if it is one; its flag then follows the key's state.
	mod keys.Mod
}

// lookupKey is the virtual key that presses k on the US layout.
func lookupKey(k keys.Key) (keyStroke, error) {
	if k.Name != "" {
		if c, ok := modCodes[keys.Mod(k.Name)]; ok {
			return keyStroke{code: c, mod: keys.Mod(k.Name)}, nil
		}
		if c, ok := namedCodes[k.Name]; ok {
			return keyStroke{code: c}, nil
		}
		if n, ok := k.IsF(); ok {
			if n <= len(fCodes) {
				return keyStroke{code: fCodes[n-1]}, nil
			}
			return keyStroke{}, fmt.Errorf("macOS has no key %s", k.Name)
		}
		// Print Screen is the one named key with no Mac counterpart; screenshots there are
		// Command-Shift-3, which the caller can press as that chord.
		return keyStroke{}, fmt.Errorf("macOS has no key %q", k.Name)
	}
	r := k.Rune
	if c, ok := runeCodes[r]; ok {
		return keyStroke{code: c}, nil
	}
	if base, ok := shiftedRunes[r]; ok {
		return keyStroke{code: runeCodes[base], implied: flagShift}, nil
	}
	if r < unicode.MaxASCII && unicode.IsUpper(r) {
		if c, ok := runeCodes[unicode.ToLower(r)]; ok {
			return keyStroke{code: c, implied: flagShift}, nil
		}
	}
	return keyStroke{}, fmt.Errorf("the character %q has no key on the US layout; type it as text instead", r)
}
