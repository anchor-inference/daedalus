// Package markdown draws the agent's answers: Markdown rendered with Glamour in the theme's colours,
// the app's rich-answer syntax flattened to text first, and a streaming form that renders each
// finished block once and re-renders only the block still being written.
package markdown

import (
	"fmt"
	"image/color"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"charm.land/glamour/v2"
	gansi "charm.land/glamour/v2/ansi"
	"charm.land/lipgloss/v2"
	"github.com/alecthomas/chroma/v2"
	"github.com/alecthomas/chroma/v2/styles"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// Renderer renders Markdown for one theme. It is safe for concurrent use.
type Renderer struct {
	theme  *theme.Theme
	config gansi.StyleConfig

	mu    sync.Mutex
	terms map[int]*glamour.TermRenderer

	// renders counts calls into Glamour, so a test can prove that a stream does not re-render what
	// it has already committed.
	renders atomic.Int64
}

// New builds a renderer for a theme.
func New(t *theme.Theme) *Renderer {
	return &Renderer{theme: t, config: styleConfig(t), terms: map[int]*glamour.TermRenderer{}}
}

// Render draws text at width: no margin, no blank lines before or after, every line at most width
// cells. Rich-answer syntax is flattened first. A text Glamour cannot render comes back wrapped as
// written, because an answer must never vanish from the screen over a rendering fault.
func (r *Renderer) Render(text string, width int) string {
	width = max(width, 8)
	// Tabs are expanded before rendering: Glamour measures a tab as one cell and the terminal draws it
	// as up to eight, which breaks every width the layout computed around a code block.
	text = strings.ReplaceAll(Flatten(text), "\t", "    ")
	if strings.TrimSpace(text) == "" {
		return ""
	}
	r.renders.Add(1)
	r.mu.Lock()
	term, err := r.term(width)
	var out string
	if err == nil {
		out, err = term.Render(text)
	}
	r.mu.Unlock()
	if err != nil {
		return strings.TrimRight(lipgloss.Wrap(text, width, ""), "\n")
	}
	return tidy(out, width)
}

func (r *Renderer) term(width int) (*glamour.TermRenderer, error) {
	if term, ok := r.terms[width]; ok {
		return term, nil
	}
	formatter := "terminal16m"
	if r.theme.Name == "ansi" {
		formatter = "terminal16"
	}
	term, err := glamour.NewTermRenderer(
		glamour.WithStyles(r.config),
		glamour.WithWordWrap(width),
		glamour.WithChromaFormatter(formatter),
	)
	if err != nil {
		return nil, err
	}
	// A handful of widths is all a session sees (one per window size); the bound only stops a resize
	// storm from keeping a renderer for every width it passed through.
	if len(r.terms) > 8 {
		r.terms = map[int]*glamour.TermRenderer{}
	}
	r.terms[width] = term
	return term, nil
}

// tidy drops the padding Glamour leaves: trailing spaces on every line, which would paint the
// background colour to the window's edge and make copied text ragged, and the blank lines around the
// document. A line still wider than width after that (a long URL Glamour would not break) is cut.
func tidy(out string, width int) string {
	lines := strings.Split(out, "\n")
	for i, line := range lines {
		line = trimRight(line)
		if ansi.StringWidth(line) > width {
			line = ansi.Truncate(line, width, "…")
		}
		lines[i] = line
	}
	// Blank lines are collapsed to one between blocks and dropped around the document: Glamour leaves
	// two after a nested list, and a gap of two reads as a section break that is not there.
	var kept []string
	for _, line := range lines {
		blank := strings.TrimSpace(ansi.Strip(line)) == ""
		if blank && (len(kept) == 0 || kept[len(kept)-1] == "") {
			continue
		}
		if blank {
			line = ""
		}
		kept = append(kept, line)
	}
	for len(kept) > 0 && kept[len(kept)-1] == "" {
		kept = kept[:len(kept)-1]
	}
	return strings.Join(kept, "\n")
}

// trimRight removes trailing spaces from a styled line, keeping a reset when any styling is left
// open, so the colour of the last word does not bleed into whatever is drawn after the line.
func trimRight(line string) string {
	if !strings.Contains(line, "\x1b") {
		return strings.TrimRight(line, " ")
	}
	visibleEnd := 0
	state := byte(0)
	for i := 0; i < len(line); {
		seq, width, n, newState := ansi.DecodeSequence(line[i:], state, nil)
		state = newState
		if width > 0 && strings.TrimSpace(seq) != "" {
			visibleEnd = i + n
		}
		i += n
	}
	kept := line[:visibleEnd]
	if strings.Contains(kept, "\x1b[") && !strings.HasSuffix(kept, "\x1b[m") && !strings.HasSuffix(kept, "\x1b[0m") {
		kept += "\x1b[m"
	}
	if at := strings.LastIndex(kept, "\x1b]8;"); at >= 0 && !strings.HasPrefix(kept[at:], "\x1b]8;;") {
		// A hyperlink opened on this line and closed in the spaces that were cut: close it here, or
		// every cell drawn after the line would be part of the link.
		kept += "\x1b]8;;\x1b\\"
	}
	return kept
}

// colorString writes a colour as Glamour's style table spells one: an ANSI index for the basic and
// indexed colours, so the sixteen-colour theme keeps following the terminal's scheme, and hex for the
// rest.
func colorString(c color.Color) *string {
	if c == nil {
		return nil
	}
	var s string
	switch v := c.(type) {
	case ansi.BasicColor:
		s = strconv.Itoa(int(v))
	case ansi.IndexedColor:
		s = strconv.Itoa(int(v))
	default:
		r, g, b, _ := c.RGBA()
		s = fmt.Sprintf("#%02X%02X%02X", r>>8, g>>8, b>>8)
	}
	return &s
}

func yes() *bool            { v := true; return &v }
func uintp(n uint) *uint    { return &n }
func strp(s string) *string { return &s }

// styleConfig builds Glamour's style table from the theme, in Claude Code's manner: text at the
// left edge, headings bold with no "#" shown, inline code in the permission colour, code blocks
// highlighted with no box around them, quotes behind a dim bar. Glamour's stock styles are not used
// because they indent the whole document and frame headings, which in a terminal transcript reads as
// a second layout inside the first.
func styleConfig(t *theme.Theme) gansi.StyleConfig {
	p := t.Palette
	color := func(c color.Color) *string {
		if t.Plain {
			return nil
		}
		return colorString(c)
	}
	bullet := "- "
	cfg := gansi.StyleConfig{
		Document:  gansi.StyleBlock{Margin: uintp(0)},
		Paragraph: gansi.StyleBlock{},
		BlockQuote: gansi.StyleBlock{
			StylePrimitive: gansi.StylePrimitive{Color: color(p.Secondary), Italic: yes()},
			Indent:         uintp(1),
			IndentToken:    strp("▎ "),
		},
		List: gansi.StyleList{LevelIndent: 2},
		Heading: gansi.StyleBlock{
			StylePrimitive: gansi.StylePrimitive{Bold: yes(), BlockSuffix: "\n"},
		},
		H1:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes(), Underline: yes()}},
		H2:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes()}},
		H3:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes()}},
		H4:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes()}},
		H5:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes()}},
		H6:            gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Bold: yes(), Color: color(p.Secondary)}},
		Strikethrough: gansi.StylePrimitive{CrossedOut: yes()},
		Emph:          gansi.StylePrimitive{Italic: yes()},
		Strong:        gansi.StylePrimitive{Bold: yes()},
		HorizontalRule: gansi.StylePrimitive{
			Color:  color(p.Subtle),
			Format: "\n" + strings.Repeat(t.Glyphs.Rule, 3) + "\n",
		},
		Item:        gansi.StylePrimitive{BlockPrefix: bullet},
		Enumeration: gansi.StylePrimitive{BlockPrefix: ". "},
		Task:        gansi.StyleTask{Ticked: t.Glyphs.BoxChecked + " ", Unticked: t.Glyphs.Box + " "},
		Link:        gansi.StylePrimitive{Color: color(p.Secondary)},
		LinkText:    gansi.StylePrimitive{Underline: yes()},
		Image:       gansi.StylePrimitive{Color: color(p.Secondary)},
		ImageText:   gansi.StylePrimitive{Color: color(p.Secondary), Format: "[image: {{.text}}]"},
		Code:        gansi.StyleBlock{StylePrimitive: gansi.StylePrimitive{Color: color(p.Permission)}},
		CodeBlock:   gansi.StyleCodeBlock{StyleBlock: gansi.StyleBlock{Margin: uintp(0)}},
		Table: gansi.StyleTable{
			CenterSeparator: strp("┼"),
			ColumnSeparator: strp("│"),
			RowSeparator:    strp("─"),
		},
		DefinitionDescription: gansi.StylePrimitive{BlockPrefix: "\n  "},
	}
	if t.Glyphs.Rule != "─" {
		cfg.BlockQuote.IndentToken = strp("| ")
		cfg.Table.CenterSeparator = strp("+")
		cfg.Table.ColumnSeparator = strp("|")
		cfg.Table.RowSeparator = strp("-")
	}
	if !t.Plain {
		cfg.CodeBlock.Theme = chromaTheme(t)
	}
	return cfg
}

var chromaOnce sync.Mutex

// chromaTheme registers, once, the code highlighting style for a theme and returns its name. The
// stock Chroma styles paint a background behind every token, which in a terminal is a box that ends
// raggedly at each line's last character; these carry foregrounds only.
func chromaTheme(t *theme.Theme) string {
	name := "daedalus-light"
	entries := chroma.StyleEntries{
		chroma.Text:              "#24292E",
		chroma.Comment:           "italic #6A737D",
		chroma.CommentPreproc:    "#D73A49",
		chroma.Keyword:           "#D73A49",
		chroma.KeywordType:       "#6F42C1",
		chroma.KeywordConstant:   "#005CC5",
		chroma.Operator:          "#D73A49",
		chroma.Punctuation:       "#24292E",
		chroma.NameBuiltin:       "#005CC5",
		chroma.NameFunction:      "#6F42C1",
		chroma.NameClass:         "#6F42C1",
		chroma.NameTag:           "#22863A",
		chroma.NameAttribute:     "#6F42C1",
		chroma.NameDecorator:     "#6F42C1",
		chroma.LiteralString:     "#032F62",
		chroma.LiteralNumber:     "#005CC5",
		chroma.GenericDeleted:    "#B31D28",
		chroma.GenericInserted:   "#22863A",
		chroma.GenericHeading:    "bold #005CC5",
		chroma.GenericSubheading: "#6F42C1",
	}
	if t.Dark {
		name = "daedalus-dark"
		entries = chroma.StyleEntries{
			chroma.Text:              "#E1E4E8",
			chroma.Comment:           "italic #8B949E",
			chroma.CommentPreproc:    "#FF7B72",
			chroma.Keyword:           "#FF7B72",
			chroma.KeywordType:       "#FFA657",
			chroma.KeywordConstant:   "#79C0FF",
			chroma.Operator:          "#FF7B72",
			chroma.Punctuation:       "#C9D1D9",
			chroma.NameBuiltin:       "#79C0FF",
			chroma.NameFunction:      "#D2A8FF",
			chroma.NameClass:         "#FFA657",
			chroma.NameTag:           "#7EE787",
			chroma.NameAttribute:     "#79C0FF",
			chroma.NameDecorator:     "#D2A8FF",
			chroma.LiteralString:     "#A5D6FF",
			chroma.LiteralNumber:     "#79C0FF",
			chroma.GenericDeleted:    "#FFA198",
			chroma.GenericInserted:   "#7EE787",
			chroma.GenericHeading:    "bold #79C0FF",
			chroma.GenericSubheading: "#D2A8FF",
		}
	}
	if t.Name == "ansi" {
		name = "daedalus-ansi"
		entries = chroma.StyleEntries{
			chroma.Comment:         "italic #808080",
			chroma.Keyword:         "#800080",
			chroma.KeywordType:     "#008080",
			chroma.NameFunction:    "#000080",
			chroma.LiteralString:   "#008000",
			chroma.LiteralNumber:   "#808000",
			chroma.GenericDeleted:  "#800000",
			chroma.GenericInserted: "#008000",
		}
	}
	chromaOnce.Lock()
	defer chromaOnce.Unlock()
	if _, ok := styles.Registry[name]; !ok {
		styles.Register(chroma.MustNewStyle(name, entries))
	}
	return name
}
