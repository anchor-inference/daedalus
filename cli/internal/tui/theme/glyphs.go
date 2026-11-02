package theme

// Glyphs are the markers that open each kind of line, in two sets that occupy the same cells.
//
// The drawn set copies Claude Code's markers. The one deliberate difference is the tool bullet:
// Claude Code draws U+23FA on macOS, but no widely installed monospace font on Linux carries it, so
// it renders as a notdef box there; the filled circle is in every font and is what Claude Code
// itself falls back to off macOS. The set below is the portable one; New swaps in U+23FA for a
// macOS platform. The platform is an option rather than read here, so a test pins it: reading the
// running system made every golden frame differ on the macOS runner.
type Glyphs struct {
	// Bullet opens a tool call and an answer.
	Bullet string
	// Result hangs a tool's result under its call.
	Result string
	// Prompt opens the input line and a past prompt.
	Prompt string
	// Pointer marks the selected row of a list.
	Pointer string
	// Spinner is the animation of a working turn, played forward then back.
	Spinner []string
	// Star is the welcome mark and the thinking marker.
	Star       string
	Ellipsis   string
	Separator  string
	Check      string
	Cross      string
	Box        string
	BoxChecked string
	BoxActive  string
	Pause      string
	Play       string
	Warning    string
	Bang       string
	ArrowUp    string
	ArrowDown  string
	Rule       string
	Corner     string
}

// UnicodeGlyphs is the drawn set.
var UnicodeGlyphs = Glyphs{
	Bullet:     "●",
	Result:     "⎿",
	Prompt:     ">",
	Pointer:    "❯",
	Spinner:    []string{"·", "✢", "✳", "✶", "✻", "✽"},
	Star:       "✻",
	Ellipsis:   "…",
	Separator:  "·",
	Check:      "✔",
	Cross:      "✘",
	Box:        "☐",
	BoxChecked: "☒",
	BoxActive:  "◼",
	Pause:      "⏸",
	Play:       "⏵⏵",
	Warning:    "⚠",
	Bang:       "!",
	ArrowUp:    "↑",
	ArrowDown:  "↓",
	Rule:       "─",
	Corner:     "╭",
}

// ASCIIGlyphs stands in where multi-byte characters cannot be drawn, cell for cell where it can.
var ASCIIGlyphs = Glyphs{
	Bullet:     "*",
	Result:     "L",
	Prompt:     ">",
	Pointer:    ">",
	Spinner:    []string{".", "+", "*", "x", "*", "+"},
	Star:       "*",
	Ellipsis:   "...",
	Separator:  "|",
	Check:      "v",
	Cross:      "x",
	Box:        "[ ]",
	BoxChecked: "[x]",
	BoxActive:  "[>]",
	Pause:      "||",
	Play:       ">>",
	Warning:    "!",
	Bang:       "!",
	ArrowUp:    "^",
	ArrowDown:  "v",
	Rule:       "-",
	Corner:     "+",
}

// macBullet is the record mark Claude Code draws on macOS, where the system fonts carry it.
const macBullet = "⏺"

// SpinnerFrame is the n-th frame of the spinner, played forward and then back so the animation
// breathes rather than jumping from the last glyph to the first.
func (g Glyphs) SpinnerFrame(n int) string {
	frames := g.Spinner
	if len(frames) < 2 {
		return frames[0]
	}
	cycle := 2*len(frames) - 2
	n %= cycle
	if n < 0 {
		n += cycle
	}
	if n >= len(frames) {
		n = cycle - n
	}
	return frames[n]
}
