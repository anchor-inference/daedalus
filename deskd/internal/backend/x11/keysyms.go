package x11

import (
	"unicode"

	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// The keysyms of the named keys and modifiers (X11's keysymdef.h).
var namedKeysyms = map[string]uint32{
	keys.Enter: 0xff0d, keys.Tab: 0xff09, keys.Escape: 0xff1b, keys.Backspace: 0xff08, keys.Delete: 0xffff,
	keys.Insert: 0xff63, keys.Home: 0xff50, keys.End: 0xff57, keys.PageUp: 0xff55, keys.PageDown: 0xff56,
	keys.Left: 0xff51, keys.Up: 0xff52, keys.Right: 0xff53, keys.Down: 0xff54, keys.Space: 0x20,
	keys.CapsLock: 0xffe5, keys.PrintScr: 0xff61, keys.Menu: 0xff67,
	string(keys.Ctrl): 0xffe3, string(keys.Shift): 0xffe1, string(keys.Alt): 0xffe9, string(keys.Super): 0xffeb,
}

const shiftKeysym = 0xffe1

// keysymOf is the keysym for a key: a named key's own, or a character's. Latin-1 characters are
// their own code; every other character is its code point plus 0x01000000, which X11 defines for
// all of Unicode, so no character needs a table.
func keysymOf(k keys.Key) (uint32, bool) {
	if k.Name != "" {
		if n, ok := k.IsF(); ok {
			return 0xffbe + uint32(n-1), true
		}
		ks, ok := namedKeysyms[k.Name]
		return ks, ok
	}
	return runeKeysym(k.Rune), k.Rune != 0
}

func runeKeysym(r rune) uint32 {
	if r >= 0x20 && r <= 0x7e || r >= 0xa0 && r <= 0xff {
		return uint32(r)
	}
	return 0x01000000 | uint32(r)
}

// keysymRune is the character a keysym types, for the keysyms the daemon itself produces and the
// common ones of a default keymap; 0 for anything else.
func keysymRune(ks uint32) rune {
	switch {
	case ks >= 0x20 && ks <= 0x7e, ks >= 0xa0 && ks <= 0xff:
		return rune(ks)
	case ks&0xff000000 == 0x01000000:
		return rune(ks & 0x00ffffff)
	}
	return 0
}

// keymap is the server's keyboard mapping: for each keycode, its keysyms by column (column 0
// unshifted, column 1 with Shift).
type keymap struct {
	min, max uint8
	per      int
	syms     []uint32
}

func (m keymap) at(code uint8, col int) uint32 {
	if code < m.min || code > m.max || col >= m.per {
		return 0
	}
	return m.syms[int(code-m.min)*m.per+col]
}

// find is the keycode and column (0 or 1) that type keysym ks. A letter whose keycode lists only its
// lower case is found in column 1 for its capital, as X11's own case rule reads such a keymap.
func (m keymap) find(ks uint32) (uint8, int, bool) {
	for code := int(m.min); code <= int(m.max); code++ {
		for col := 0; col < min(2, m.per); col++ {
			if m.at(uint8(code), col) == ks {
				return uint8(code), col, true
			}
		}
	}
	if r := keysymRune(ks); r != 0 && unicode.IsUpper(r) {
		lower := runeKeysym(unicode.ToLower(r))
		for code := int(m.min); code <= int(m.max); code++ {
			if m.at(uint8(code), 0) == lower && (m.per < 2 || m.at(uint8(code), 1) == 0) {
				return uint8(code), 1, true
			}
		}
	}
	return 0, 0, false
}

// spare lists the keycodes with no keysym at all: the daemon binds a character missing from the
// layout to one of them for a moment, types it, and unbinds it.
func (m keymap) spare() []uint8 {
	var out []uint8
	for code := int(m.max); code >= int(m.min); code-- {
		empty := true
		for col := 0; col < m.per; col++ {
			if m.at(uint8(code), col) != 0 {
				empty = false
				break
			}
		}
		if empty {
			out = append(out, uint8(code))
		}
	}
	return out
}

// KeysymRune is the character a keysym types, or 0: the selftest's own window decodes its key
// presses with it.
func KeysymRune(ks uint32) rune { return keysymRune(ks) }
