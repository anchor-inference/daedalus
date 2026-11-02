// Package theme is the one table every colour and marker of the terminal interface comes from.
//
// Nothing else in the interface names a colour. A component asks for a role ("the accent", "the
// border of the prompt in shell mode", "a removed line's background") and the theme answers, so a
// new palette is one table and a component can never drift from the rest by hard-coding a shade.
// The roles and the default shades follow Claude Code's own palette, which is the look this client
// copies: the orange accent, the blue-violet of permission prompts, teal for plan, violet for
// auto-accept, pink for shell mode, and tinted red and green rows for diffs.
package theme

import (
	"image/color"
	"os"
	"strings"

	"charm.land/lipgloss/v2"
)

// Palette is the set of colour roles. Every field is a role, never a hue: the light palette gives
// "Text" a near-black and the dark palette a near-white, and both are right.
type Palette struct {
	// Accent is the orange of the spinner, the welcome mark and the working status.
	Accent color.Color
	// AccentShimmer is the lighter orange the spinner's verb shimmers through.
	AccentShimmer color.Color
	Text          color.Color
	// Secondary is dimmed text: results, hints, the status line.
	Secondary color.Color
	// Subtle is the faintest text: placeholders, rules, queued messages.
	Subtle color.Color
	// Border is the prompt box's border in the ordinary state.
	Border color.Color
	// ShellBorder is the prompt box's border in `!` mode.
	ShellBorder color.Color
	// Permission is the accent of permission prompts and selections.
	Permission color.Color
	// Plan and Auto are the local guards' accents.
	Plan color.Color
	Auto color.Color
	// Suggestion is the highlighted row of a palette or picker.
	Suggestion color.Color
	Success    color.Color
	Error      color.Color
	Warning    color.Color
	// DiffAdd and DiffRemove tint whole changed rows; the Word variants mark the changed words in
	// them; the Dim variants tint the line-number gutter beside them.
	DiffAdd        color.Color
	DiffRemove     color.Color
	DiffAddWord    color.Color
	DiffRemoveWord color.Color
	DiffAddDim     color.Color
	DiffRemoveDim  color.Color
	// UserBackground is the faint band a past prompt sits on in the transcript.
	UserBackground color.Color
	// Selection is the background of text selected with the mouse.
	Selection color.Color
	// Agents are the colours subagents are told apart by, in order of appearance.
	Agents [8]color.Color
}

// Theme is a palette plus the facts about the terminal that decide how it is drawn.
type Theme struct {
	Name    string
	Dark    bool
	Palette Palette
	Glyphs  Glyphs
	// Plain is a terminal that asked for no colour (NO_COLOR, TERM=dumb): every style renders bare.
	Plain bool
}

func hex(s string) color.Color { return lipgloss.Color(s) }

// The two Claude palettes. The shades are the ones Claude Code draws, read off its screens; they are
// facts about a look, not code.
var (
	darkPalette = Palette{
		Accent:         hex("#D77757"),
		AccentShimmer:  hex("#EB9F7F"),
		Text:           hex("#FFFFFF"),
		Secondary:      hex("#999999"),
		Subtle:         hex("#505050"),
		Border:         hex("#888888"),
		ShellBorder:    hex("#FD5DB1"),
		Permission:     hex("#B1B9F9"),
		Plan:           hex("#48968C"),
		Auto:           hex("#AF87FF"),
		Suggestion:     hex("#B1B9F9"),
		Success:        hex("#4EBA65"),
		Error:          hex("#FF6B80"),
		Warning:        hex("#FFC107"),
		DiffAdd:        hex("#225C2B"),
		DiffRemove:     hex("#7A2936"),
		DiffAddWord:    hex("#38A660"),
		DiffRemoveWord: hex("#B3596B"),
		DiffAddDim:     hex("#47584A"),
		DiffRemoveDim:  hex("#69484D"),
		UserBackground: hex("#373737"),
		Selection:      hex("#264F78"),
		Agents: [8]color.Color{
			hex("#DC5050"), hex("#5A8CF0"), hex("#4EBA65"), hex("#E6B43C"),
			hex("#AF87FF"), hex("#F08C3C"), hex("#F07DB4"), hex("#46BEC8"),
		},
	}
	lightPalette = Palette{
		Accent:         hex("#D77757"),
		AccentShimmer:  hex("#F5A07F"),
		Text:           hex("#000000"),
		Secondary:      hex("#666666"),
		Subtle:         hex("#AFAFAF"),
		Border:         hex("#999999"),
		ShellBorder:    hex("#FF0087"),
		Permission:     hex("#5769F7"),
		Plan:           hex("#006666"),
		Auto:           hex("#8700FF"),
		Suggestion:     hex("#5769F7"),
		Success:        hex("#2C7A39"),
		Error:          hex("#AB2B3F"),
		Warning:        hex("#966C1E"),
		DiffAdd:        hex("#69DB7C"),
		DiffRemove:     hex("#FFA8B4"),
		DiffAddWord:    hex("#2F9D44"),
		DiffRemoveWord: hex("#D1454B"),
		DiffAddDim:     hex("#C7E1CB"),
		DiffRemoveDim:  hex("#FDD2D8"),
		UserBackground: hex("#F0F0F0"),
		Selection:      hex("#B4D5FE"),
		Agents: [8]color.Color{
			hex("#C83232"), hex("#2864C8"), hex("#2C7A39"), hex("#A07800"),
			hex("#8700FF"), hex("#C86400"), hex("#C83C8C"), hex("#1E8C96"),
		},
	}
	// ansiPalette is for terminals with sixteen colours, or for people who want the terminal's own
	// scheme: every role is one of the basic ANSI colours, so it follows whatever the terminal maps
	// them to, light or dark.
	ansiPalette = Palette{
		Accent:         lipgloss.Red,
		AccentShimmer:  lipgloss.BrightRed,
		Text:           lipgloss.BrightWhite,
		Secondary:      lipgloss.BrightBlack,
		Subtle:         lipgloss.BrightBlack,
		Border:         lipgloss.White,
		ShellBorder:    lipgloss.Magenta,
		Permission:     lipgloss.Blue,
		Plan:           lipgloss.Cyan,
		Auto:           lipgloss.Magenta,
		Suggestion:     lipgloss.BrightBlue,
		Success:        lipgloss.Green,
		Error:          lipgloss.Red,
		Warning:        lipgloss.Yellow,
		DiffAdd:        lipgloss.Green,
		DiffRemove:     lipgloss.Red,
		DiffAddWord:    lipgloss.BrightGreen,
		DiffRemoveWord: lipgloss.BrightRed,
		DiffAddDim:     lipgloss.Green,
		DiffRemoveDim:  lipgloss.Red,
		UserBackground: nil,
		Selection:      lipgloss.Blue,
		Agents: [8]color.Color{
			lipgloss.Red, lipgloss.Blue, lipgloss.Green, lipgloss.Yellow,
			lipgloss.Magenta, lipgloss.BrightRed, lipgloss.BrightMagenta, lipgloss.Cyan,
		},
	}
)

// Names lists the themes /config offers, in the order it cycles them.
var Names = []string{"dark", "light", "ansi"}

// Options are the facts about the terminal a theme is built for.
type Options struct {
	// Name is one of Names; empty or "auto" picks dark or light from Dark.
	Name string
	// Dark is the terminal's background as detected; it matters only for "auto" and "ansi".
	Dark bool
	// Plain draws no colour at all.
	Plain bool
	// ASCII draws the fallback markers.
	ASCII bool
}

// New builds the theme an Options names. An unknown name falls back to auto rather than failing:
// a settings file from a newer version must not stop the program from starting.
func New(o Options) *Theme {
	t := &Theme{Plain: o.Plain, Glyphs: UnicodeGlyphs}
	if o.ASCII {
		t.Glyphs = ASCIIGlyphs
	}
	switch strings.ToLower(o.Name) {
	case "dark":
		t.Name, t.Dark, t.Palette = "dark", true, darkPalette
	case "light":
		t.Name, t.Dark, t.Palette = "light", false, lightPalette
	case "ansi":
		t.Name, t.Dark, t.Palette = "ansi", o.Dark, ansiPalette
	default:
		if o.Dark {
			t.Name, t.Dark, t.Palette = "dark", true, darkPalette
		} else {
			t.Name, t.Dark, t.Palette = "light", false, lightPalette
		}
	}
	return t
}

// Environment reads the variables that ask for no colour or no drawn characters.
//
// NO_COLOR counts on presence, not on value: an empty string is how a shell most often exports it.
// A locale that is not UTF-8 gets the ASCII markers, since every drawn marker is multi-byte and a
// terminal decoding bytes one at a time turns each into mojibake that also breaks the layout.
func Environment() (plain, ascii bool) {
	_, noColor := os.LookupEnv("NO_COLOR")
	plain = noColor || strings.EqualFold(os.Getenv("TERM"), "dumb")
	locale := os.Getenv("LC_ALL")
	if locale == "" {
		locale = os.Getenv("LC_CTYPE")
	}
	if locale == "" {
		locale = os.Getenv("LANG")
	}
	lowered := strings.ToLower(locale)
	ascii = locale != "" && locale != "C.UTF-8" && !strings.Contains(lowered, "utf-8") && !strings.Contains(lowered, "utf8")
	if os.Getenv("DAEDALUS_ASCII") == "1" {
		ascii = true
	}
	return plain, ascii
}

// Style starts a style that is bare on a plain terminal. Every component builds its styles through
// this, so NO_COLOR is honoured in one place and cannot be forgotten by one component.
func (t *Theme) Style() lipgloss.Style { return lipgloss.NewStyle() }

// Fg is a style with a foreground role, or a bare style on a plain terminal.
func (t *Theme) Fg(c color.Color) lipgloss.Style {
	if t.Plain || c == nil {
		return lipgloss.NewStyle()
	}
	return lipgloss.NewStyle().Foreground(c)
}

// Bg is a style with a background role, or a bare style on a plain terminal.
func (t *Theme) Bg(c color.Color) lipgloss.Style {
	if t.Plain || c == nil {
		return lipgloss.NewStyle()
	}
	return lipgloss.NewStyle().Background(c)
}

// Dim is secondary text.
func (t *Theme) Dim() lipgloss.Style { return t.Fg(t.Palette.Secondary) }

// Faint is the faintest text.
func (t *Theme) Faint() lipgloss.Style { return t.Fg(t.Palette.Subtle) }

// Bold is the text colour, bold. Bold survives a plain terminal: it is emphasis, not colour.
func (t *Theme) Bold() lipgloss.Style { return t.Fg(t.Palette.Text).Bold(true) }

// Agent is the colour of the n-th subagent, cycling through the eight.
func (t *Theme) Agent(n int) color.Color {
	if n < 0 {
		n = -n
	}
	return t.Palette.Agents[n%len(t.Palette.Agents)]
}
