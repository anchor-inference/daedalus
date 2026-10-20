// Package keys is the keyboard vocabulary every backend shares: the names a model or the host may use
// for a key, parsed into one canonical chord, and the way text is cut up before it is typed. Models
// come with three dialects (xdotool's "Return" and "Page_Down", OpenAI's "ARROWLEFT", Playwright's
// "Meta"), and one table here means no backend learns them on its own.
package keys

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf16"
	"unicode/utf8"
)

// Mod is a modifier key.
type Mod string

// The modifiers, in the order a chord presses them. Super is Command on macOS and the Windows key
// elsewhere: a model asking for "cmd+c" on Linux means the same key position as "super+c".
const (
	Ctrl  Mod = "ctrl"
	Alt   Mod = "alt"
	Shift Mod = "shift"
	Super Mod = "super"
)

var modOrder = []Mod{Ctrl, Alt, Shift, Super}

var modNames = map[string]Mod{
	"ctrl": Ctrl, "control": Ctrl, "ctl": Ctrl, "lctrl": Ctrl, "rctrl": Ctrl, "controlleft": Ctrl, "controlright": Ctrl,
	"controll": Ctrl, "controlr": Ctrl, // xdotool's Control_L and Control_R
	"alt": Alt, "option": Alt, "opt": Alt, "lalt": Alt, "ralt": Alt, "altleft": Alt, "altright": Alt, "altgr": Alt, "altl": Alt, "altr": Alt,
	"shift": Shift, "lshift": Shift, "rshift": Shift, "shiftleft": Shift, "shiftright": Shift, "shiftl": Shift, "shiftr": Shift,
	"super": Super, "cmd": Super, "command": Super, "meta": Super, "win": Super, "windows": Super,
	"metaleft": Super, "metaright": Super, "superl": Super, "superr": Super, "metal": Super, "metar": Super, "os": Super,
}

// The named keys a backend must know. Every other key is a single character.
const (
	Enter     = "enter"
	Tab       = "tab"
	Escape    = "escape"
	Backspace = "backspace"
	Delete    = "delete"
	Insert    = "insert"
	Home      = "home"
	End       = "end"
	PageUp    = "pageup"
	PageDown  = "pagedown"
	Up        = "up"
	Down      = "down"
	Left      = "left"
	Right     = "right"
	Space     = "space"
	CapsLock  = "capslock"
	PrintScr  = "printscreen"
	Menu      = "menu"
)

var named = map[string]string{
	"enter": Enter, "return": Enter, "kpenter": Enter, "numpadenter": Enter, "ret": Enter,
	"tab": Tab, "escape": Escape, "esc": Escape,
	"backspace": Backspace, "bksp": Backspace, "back": Backspace,
	"delete": Delete, "del": Delete,
	"insert": Insert, "ins": Insert,
	"home": Home, "end": End,
	"pageup": PageUp, "pgup": PageUp, "prior": PageUp,
	"pagedown": PageDown, "pgdn": PageDown, "next": PageDown,
	"up": Up, "arrowup": Up, "down": Down, "arrowdown": Down,
	"left": Left, "arrowleft": Left, "right": Right, "arrowright": Right,
	"space": Space, "spacebar": Space,
	"capslock": CapsLock, "caps": CapsLock,
	"printscreen": PrintScr, "print": PrintScr, "prtsc": PrintScr,
	"menu": Menu, "apps": Menu, "contextmenu": Menu,
	// Characters whose own glyph cannot stand in a chord written with "+" and spaces.
	"plus": "+", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
	"semicolon": ";", "apostrophe": "'", "quote": "'", "grave": "`", "backquote": "`",
	"bracketleft": "[", "bracketright": "]",
}

func init() {
	for i := 1; i <= 24; i++ {
		f := fmt.Sprintf("f%d", i)
		named[f] = f
	}
}

// Key is one key: a named one (Name) or the key that types a character (Rune).
type Key struct {
	Name string `json:"name,omitempty"`
	Rune rune   `json:"rune,omitempty"`
}

// IsF reports whether the key is a function key, and which: F1 is 1.
func (k Key) IsF() (int, bool) {
	var n int
	if _, err := fmt.Sscanf(k.Name, "f%d", &n); err == nil && n >= 1 && n <= 24 {
		return n, true
	}
	return 0, false
}

func (k Key) String() string {
	if k.Name != "" {
		return k.Name
	}
	return string(k.Rune)
}

// Chord is modifiers held around one key.
type Chord struct {
	Mods []Mod `json:"mods,omitempty"`
	Key  Key   `json:"key"`
}

// String is the chord in canonical form, "ctrl+shift+t".
func (c Chord) String() string {
	var b strings.Builder
	for _, m := range c.Mods {
		b.WriteString(string(m))
		b.WriteByte('+')
	}
	b.WriteString(c.Key.String())
	return b.String()
}

// Printable reports whether the chord types a character a person would see: a character key with
// no modifier but Shift. The audit records such keys as their count, never their value, since a
// model can type a password one key at a time.
func (c Chord) Printable() bool {
	if c.Key.Rune == 0 && c.Key.Name != Space {
		return false
	}
	for _, m := range c.Mods {
		if m != Shift {
			return false
		}
	}
	return true
}

func canon(s string) string {
	s = strings.ToLower(s)
	return strings.NewReplacer("_", "", "-", "", " ", "").Replace(s)
}

// ParseChord reads one chord: modifiers and a key joined by "+", in any of the dialects ("Return",
// "ctrl+shift+Page_Down", "CTRL+ARROWLEFT", "Meta+a", "cmd+space"). A "+" key is written "plus" or
// as the last of two plus signs ("ctrl++"). A capital letter alone means Shift and the letter, as
// a person pressing it would.
func ParseChord(s string) (Chord, error) {
	if strings.TrimSpace(s) == "" {
		return Chord{}, errors.New("an empty key")
	}
	var parts []string
	if strings.HasSuffix(s, "++") {
		parts = append(strings.Split(strings.TrimSuffix(s, "++"), "+"), "+")
	} else if s == "+" {
		parts = []string{"+"}
	} else {
		parts = strings.Split(s, "+")
	}
	held := map[Mod]bool{}
	var key *Key
	for i, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			return Chord{}, fmt.Errorf("key %q has an empty part", s)
		}
		last := i == len(parts)-1
		if m, ok := modNames[canon(p)]; ok {
			if last {
				// A modifier alone ("shift") is a key in its own right: pressed and released.
				key = &Key{Name: string(m)}
				break
			}
			held[m] = true
			continue
		}
		if !last {
			return Chord{}, fmt.Errorf("key %q: %q is not a modifier", s, p)
		}
		k, err := parseKey(p)
		if err != nil {
			return Chord{}, fmt.Errorf("key %q: %w", s, err)
		}
		key = &k
	}
	if key == nil {
		return Chord{}, fmt.Errorf("key %q names no key", s)
	}
	if key.Rune != 0 && unicode.IsUpper(key.Rune) && key.Rune < utf8.RuneSelf {
		key.Rune = unicode.ToLower(key.Rune)
		if len(held) == 0 {
			held[Shift] = true
		}
	}
	c := Chord{Key: *key}
	for _, m := range modOrder {
		if held[m] && Mod(key.Name) != m {
			c.Mods = append(c.Mods, m)
		}
	}
	return c, nil
}

func parseKey(p string) (Key, error) {
	if utf8.RuneCountInString(p) == 1 {
		r, _ := utf8.DecodeRuneInString(p)
		if r == ' ' {
			return Key{Name: Space}, nil
		}
		if !unicode.IsPrint(r) {
			return Key{}, fmt.Errorf("%q is not a printable character", p)
		}
		return Key{Rune: r}, nil
	}
	n, ok := named[canon(p)]
	if !ok {
		// Keypad digits and the like: "KP_1", "Numpad1".
		c := canon(p)
		for _, prefix := range []string{"kp", "numpad", "digit", "key"} {
			if rest, found := strings.CutPrefix(c, prefix); found && utf8.RuneCountInString(rest) == 1 {
				r, _ := utf8.DecodeRuneInString(rest)
				return Key{Rune: r}, nil
			}
		}
		return Key{}, fmt.Errorf("%q is not a key this daemon knows", p)
	}
	if utf8.RuneCountInString(n) == 1 {
		r, _ := utf8.DecodeRuneInString(n)
		return Key{Rune: r}, nil
	}
	return Key{Name: n}, nil
}

// ParseSequence reads chords separated by spaces, as xdotool's "ctrl+a Delete" does.
func ParseSequence(s string) ([]Chord, error) {
	var out []Chord
	for _, f := range strings.Fields(s) {
		c, err := ParseChord(f)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	if len(out) == 0 {
		return nil, errors.New("no keys")
	}
	return out, nil
}

// Segment is a piece of text to type: either characters (Text) or a key that stands for one (Key:
// a line break is Enter, a tab is Tab). Applications treat a typed "\n" character inconsistently
// (Windows programs mostly ignore it; on macOS a Unicode event that starts with one is silently
// dropped), so every backend presses the key instead.
type Segment struct {
	Text string
	Key  string
}

// Split cuts text into segments: runs of characters, and Enter for every line break ("\r\n", "\n"
// or a lone "\r"), Tab for every tab.
func Split(text string) []Segment {
	var out []Segment
	var run strings.Builder
	flush := func() {
		if run.Len() > 0 {
			out = append(out, Segment{Text: run.String()})
			run.Reset()
		}
	}
	for i := 0; i < len(text); {
		r, size := utf8.DecodeRuneInString(text[i:])
		switch r {
		case '\r':
			flush()
			out = append(out, Segment{Key: Enter})
			if strings.HasPrefix(text[i+size:], "\n") {
				size++
			}
		case '\n':
			flush()
			out = append(out, Segment{Key: Enter})
		case '\t':
			flush()
			out = append(out, Segment{Key: Tab})
		default:
			run.WriteRune(r)
		}
		i += size
	}
	flush()
	return out
}

// ChunkUTF16 cuts text into pieces of at most limit UTF-16 code units, never between the two halves
// of a surrogate pair. macOS's CGEventKeyboardSetUnicodeString types at most 20 units of one event
// and drops the rest without a word, and an emoji split across two events arrives as two broken
// characters.
func ChunkUTF16(text string, limit int) []string {
	if limit < 2 {
		limit = 2
	}
	var out []string
	var cur strings.Builder
	units := 0
	for _, r := range text {
		n := utf16.RuneLen(r)
		if n < 0 {
			n = 1
		}
		if units+n > limit {
			out = append(out, cur.String())
			cur.Reset()
			units = 0
		}
		cur.WriteRune(r)
		units += n
	}
	if cur.Len() > 0 {
		out = append(out, cur.String())
	}
	return out
}

// Stroke is one key event of Unicode typing on Windows: a UTF-16 unit, pressed or released.
type Stroke struct {
	Unit uint16
	Up   bool
}

// UnicodeStrokes is the SendInput sequence (KEYEVENTF_UNICODE) that types text. A character of the
// basic plane is its unit down and up. A character beyond it (an emoji) is both surrogates down, high
// first, then both up in the same order: the window gets WM_CHAR for the high and then the low half
// while both are down, which is what lets it join them. Sending the high half down and up before the
// low half, as two libraries did, gives programs that pair WM_KEYUP with WM_CHAR a lone surrogate.
// [unverified on a real machine: the order is the one Windows' own touch keyboard is reported to use]
func UnicodeStrokes(text string) []Stroke {
	var out []Stroke
	for _, r := range text {
		if hi, lo := utf16.EncodeRune(r); hi != unicode.ReplacementChar || lo != unicode.ReplacementChar {
			out = append(out, Stroke{Unit: uint16(hi)}, Stroke{Unit: uint16(lo)},
				Stroke{Unit: uint16(hi), Up: true}, Stroke{Unit: uint16(lo), Up: true})
			continue
		}
		u := uint16(r)
		if r > 0xFFFF || (r >= 0xD800 && r <= 0xDFFF) {
			u = 0xFFFD
		}
		out = append(out, Stroke{Unit: u}, Stroke{Unit: u, Up: true})
	}
	return out
}
