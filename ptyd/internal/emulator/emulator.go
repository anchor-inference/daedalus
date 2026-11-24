// Package emulator is the headless terminal the daemon keeps for every terminal, behind one
// interface so that the implementation can change without touching the rest of the daemon.
//
// An Emulator is not safe for concurrent use. The terminal owns it from one goroutine, which feeds
// it, resizes it and asks it questions in order; that is also what makes a snapshot consistent with
// the output offset it is taken at.
package emulator

// Emulator is a headless terminal: bytes in, a screen out.
type Emulator interface {
	// Feed consumes output from the PTY. It never blocks and never fails: whatever the bytes are,
	// the screen afterwards is some screen.
	Feed(p []byte)
	Resize(cols, rows int)
	Size() (cols, rows int)
	Cursor() Cursor
	Modes() Modes
	// History reports the scrollback lines held and the absolute row number of the oldest one.
	// Absolute rows count every line since the terminal started, so a row keeps its number as older
	// ones fall off the scrollback.
	History() (lines int, firstAbsRow int64)
	// Snapshot returns VT bytes that reproduce the scrollback (o.Scrollback lines of it), both
	// screens, the cursor, the pen, the modes, the scroll region and the character sets when written
	// into a freshly reset terminal of the same size.
	Snapshot(o SnapshotOptions) ([]byte, SnapshotInfo)
	// Text returns the rows from..to (absolute, half-open) as text, soft-wrapped rows joined.
	Text(from, to int64) []string
	// Runs returns the rows from..to (absolute, half-open) as styled runs, for previews.
	Runs(from, to int64) [][]Run
	Close()
}

// Options are what a new emulator is created with.
type Options struct {
	Cols, Rows int
	// ScrollbackLines is the history to keep, in lines of the terminal's width. An emulator may
	// enforce it as the memory those lines take, so it is about this many lines.
	ScrollbackLines int
	// ScrollbackBytes is the most memory the history may take whatever the lines hold: the
	// terminal's memory budget.
	ScrollbackBytes int
	// GraphemeClusters starts the emulator with grapheme clustering on (mode 2027), which is how the
	// browser's width provider measures text. With it off, the two would disagree about the width of
	// every emoji sequence and every combining cluster, and a snapshot would misplace the rest of the
	// row. The browser cannot follow an application that turns the mode off again; none of the
	// programs this is for does.
	GraphemeClusters bool
}

// Factory creates an emulator. The daemon's production factory is chosen at build time.
type Factory func(o Options) Emulator

// Cursor is the cursor's position on the visible screen, and its absolute row.
type Cursor struct {
	X, Y    int
	Visible bool
	AbsRow  int64 // history lines + Y
}

// Mouse is the mouse reporting the application asked for.
type Mouse struct {
	Mode     int // 0 off, else 9, 1000, 1002 or 1003
	Encoding int // 0 default, else 1005, 1006, 1015 or 1016
}

// Modes are the terminal modes that change what the daemon sends or reports.
type Modes struct {
	AltScreen      bool
	BracketedPaste bool
	AppCursor      bool // DECCKM: arrows send ESC O x
	AppKeypad      bool // DECKPAM
	Mouse          Mouse
	FocusEvents    bool
	KittyFlags     int
	CursorStyle    int // DECSCUSR
	Origin         bool
	Wrap           bool
	Insert         bool
	SyncOutput     bool // mode 2026
}

// SnapshotOptions select what a snapshot carries.
type SnapshotOptions struct {
	Scrollback int // lines of history to include
}

// SnapshotInfo describes a snapshot's content.
type SnapshotInfo struct {
	Cols, Rows  int
	FirstAbsRow int64 // absolute row of the snapshot's first line
}

// Run is a stretch of a row with one style. A colour of 0 is the default colour, so that the common
// case costs nothing in JSON; 1..256 are palette entries 0..255 plus one; 0x1000000 + RGB is true
// colour.
type Run struct {
	T   string `json:"t"`
	FG  int    `json:"fg,omitempty"`
	BG  int    `json:"bg,omitempty"`
	B   bool   `json:"b,omitempty"`
	I   bool   `json:"i,omitempty"`
	U   bool   `json:"u,omitempty"`
	D   bool   `json:"d,omitempty"`
	Inv bool   `json:"inv,omitempty"`
}

// Querier is what an emulator with a screen also knows: the state terminal queries ask about beyond
// Cursor and Modes. The query answerer uses it when the emulator has it and falls back to Cursor and
// Modes when not.
type Querier interface {
	// ModeReport is the state of a mode as DECRQM reports it: known is false for a mode the emulator
	// does not have.
	ModeReport(mode int, ansi bool) (set, known bool)
	// Pen is the current graphic rendition as SGR parameters ("0;1;38;5;3"), what DECRQSS m reports.
	Pen() string
	// ScrollRegion is the top and bottom margins, 1-based and inclusive.
	ScrollRegion() (top, bottom int)
	// CursorShape is the DECSCUSR value in effect (1 blinking block … 6 steady bar).
	CursorShape() int
	// Color is the colour in effect for a slot, including what the program set with OSC 4/10/11/12.
	Color(slot ColorSlot, index int) (RGB, bool)
	// SetTheme sets the colours a program gets when it did not set its own: the ones the person
	// looking at the terminal sees.
	SetTheme(Theme)
}

// ColorSlot names a colour a program can ask about.
type ColorSlot int

const (
	ColorPalette    ColorSlot = iota // OSC 4, index 0..255
	ColorForeground                  // OSC 10
	ColorBackground                  // OSC 11
	ColorCursor                      // OSC 12
)

// RGB is a colour.
type RGB struct{ R, G, B uint8 }

// Theme is the colours of the person looking at a terminal. Palette may be shorter than 16 or empty;
// the entries it lacks keep their defaults.
type Theme struct {
	Foreground, Background, Cursor RGB
	Palette                        []RGB
}

// DefaultTheme is the app's dark palette (FALLBACK in miniapp/src/terminal/theme.ts). A terminal
// answers colour queries with it until it learns the colours of the person looking at it; a host
// test keeps the two copies equal.
var DefaultTheme = Theme{
	Foreground: RGB{0xf5, 0xf5, 0xf5},
	Background: RGB{0x06, 0x06, 0x06},
	Cursor:     RGB{0x5b, 0xa5, 0xf6},
	Palette: []RGB{
		{0x1c, 0x1c, 0x1c}, {0xf8, 0x6a, 0x6b}, {0x5b, 0xcc, 0x80}, {0xf3, 0xaf, 0x49},
		{0x5b, 0xa5, 0xf6}, {0xbf, 0x93, 0xf1}, {0x5c, 0xc9, 0xc2}, {0xa4, 0xa4, 0xa4},
		{0x63, 0x63, 0x63}, {0xff, 0x8f, 0x8e}, {0x86, 0xe3, 0xa4}, {0xf8, 0xc8, 0x77},
		{0x8c, 0xc0, 0xfa}, {0xd4, 0xb5, 0xf6}, {0x8a, 0xdc, 0xd6}, {0xf5, 0xf5, 0xf5},
	},
}
