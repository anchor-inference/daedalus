package ptytest

import "strings"

// keySequences are the bytes a terminal sends for named keys, in the xterm encoding Bubble Tea
// reads without any keyboard enhancement negotiated.
var keySequences = map[string]string{
	"enter":       "\r",
	"esc":         "\x1b",
	"escape":      "\x1b",
	"tab":         "\t",
	"shift+tab":   "\x1b[Z",
	"backspace":   "\x7f",
	"space":       " ",
	"up":          "\x1b[A",
	"down":        "\x1b[B",
	"right":       "\x1b[C",
	"left":        "\x1b[D",
	"home":        "\x1b[H",
	"end":         "\x1b[F",
	"pgup":        "\x1b[5~",
	"pgdown":      "\x1b[6~",
	"delete":      "\x1b[3~",
	"shift+enter": "\x1b[13;2u",
	"alt+enter":   "\x1b\r",
	"ctrl+enter":  "\x1b[13;5u",
}

// KeySequence is the byte sequence for a named key: the entries above, "ctrl+<letter>", or
// "alt+<char>". Anything else is returned as it is, so a name that is not a key is typed as text.
func KeySequence(name string) string {
	lowered := strings.ToLower(name)
	if seq, ok := keySequences[lowered]; ok {
		return seq
	}
	if rest, ok := strings.CutPrefix(lowered, "ctrl+"); ok && len(rest) == 1 {
		c := rest[0]
		switch {
		case c >= 'a' && c <= 'z':
			return string(rune(c - 'a' + 1))
		case c == '_':
			return "\x1f"
		case c == '@' || c == ' ':
			return "\x00"
		}
	}
	if rest, ok := strings.CutPrefix(lowered, "alt+"); ok && len(rest) == 1 {
		return "\x1b" + rest
	}
	return name
}
