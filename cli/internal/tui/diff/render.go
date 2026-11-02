package diff

import (
	"strconv"
	"strings"
	"unicode"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// Render draws lines at width, at most max rows (max <= 0 draws them all).
//
// Each row is the line number right-aligned in a gutter, the sign, and the text. Changed rows are
// tinted across the whole width, so a change reads as a band even when its text is short; the
// changed words of a line replaced by a similar one are tinted more strongly. Long lines are cut with
// an ellipsis rather than wrapped: a wrapped row has no line number and reads as a line of its own.
func Render(t *theme.Theme, lines []Line, width int, max int) []string {
	if len(lines) == 0 || width <= 0 {
		return nil
	}
	gutter := 1
	for _, line := range lines {
		gutter = maxInt(gutter, len(strconv.Itoa(maxInt(line.Old, line.New))))
	}
	words := pairWords(lines)

	shown := lines
	hidden := 0
	if max > 0 && len(lines) > max {
		shown = lines[:max]
		for _, line := range lines[max:] {
			if line.Op != Elision {
				hidden++
			}
		}
	}

	out := make([]string, 0, len(shown)+1)
	for i, line := range shown {
		out = append(out, renderRow(t, line, words[i], gutter, width))
	}
	if hidden > 0 {
		out = append(out, t.Dim().Render(ansi.Truncate(strings.Repeat(" ", gutter+1)+t.Glyphs.Ellipsis+" +"+strconv.Itoa(hidden)+" lines", width, "")))
	}
	return out
}

func renderRow(t *theme.Theme, line Line, changed []bool, gutter, width int) string {
	if line.Op == Elision {
		return t.Dim().Render(ansi.Truncate(strings.Repeat(" ", gutter+1)+t.Glyphs.Ellipsis, width, ""))
	}
	number, sign := line.New, " "
	var rowBg, gutterBg, wordBg lipgloss.Style
	text := t.Fg(t.Palette.Text)
	switch line.Op {
	case Add:
		sign = "+"
		rowBg, gutterBg, wordBg = t.Bg(t.Palette.DiffAdd), t.Bg(t.Palette.DiffAddDim), t.Bg(t.Palette.DiffAddWord)
	case Remove:
		number, sign = line.Old, "-"
		rowBg, gutterBg, wordBg = t.Bg(t.Palette.DiffRemove), t.Bg(t.Palette.DiffRemoveDim), t.Bg(t.Palette.DiffRemoveWord)
	default:
		rowBg, gutterBg = t.Style(), t.Style()
		text = t.Fg(t.Palette.Text)
	}
	numberText := strings.Repeat(" ", gutter-len(strconv.Itoa(number))) + strconv.Itoa(number)
	head := numberText + " " + sign + " "
	var b strings.Builder
	if line.Op == Context {
		b.WriteString(t.Dim().Render(head))
	} else {
		b.WriteString(gutterBg.Inherit(text).Render(head))
	}
	room := width - ansi.StringWidth(head)
	if room <= 0 {
		return ansi.Truncate(b.String(), width, "")
	}

	segments := split(expandTabs(line.Text), changed)
	used := 0
	cut := false
	for _, segment := range segments {
		if used >= room {
			cut = true
			break
		}
		piece := segment.text
		w := ansi.StringWidth(piece)
		if used+w > room {
			piece = ansi.Truncate(piece, maxInt(0, room-used-ansi.StringWidth(t.Glyphs.Ellipsis)), "")
			w = ansi.StringWidth(piece)
			cut = true
		}
		style := rowBg.Inherit(text)
		if segment.changed && line.Op != Context {
			style = wordBg.Inherit(text)
		}
		b.WriteString(style.Render(piece))
		used += w
		if cut {
			break
		}
	}
	if cut {
		b.WriteString(rowBg.Inherit(t.Dim()).Render(t.Glyphs.Ellipsis))
		used += ansi.StringWidth(t.Glyphs.Ellipsis)
	}
	if pad := room - used; pad > 0 && line.Op != Context {
		b.WriteString(rowBg.Render(strings.Repeat(" ", pad)))
	}
	return b.String()
}

type segment struct {
	text    string
	changed bool
}

// split cuts a line's text into runs of changed and unchanged tokens; nil marks means one plain run.
func split(text string, marks []bool) []segment {
	if marks == nil {
		return []segment{{text: text}}
	}
	tokens := tokenize(text)
	var out []segment
	for i, token := range tokens {
		changed := i < len(marks) && marks[i]
		if n := len(out); n > 0 && out[n-1].changed == changed {
			out[n-1].text += token
			continue
		}
		out = append(out, segment{text: token, changed: changed})
	}
	return out
}

// pairWords marks, for each removed line followed by an added line in the same changed stretch, the
// tokens that differ. Lines are paired in order; a pair whose lines share too little is left unmarked,
// because highlighting every word of an unrelated replacement says nothing the row tint does not.
func pairWords(lines []Line) [][]bool {
	marks := make([][]bool, len(lines))
	for i := 0; i < len(lines); {
		if lines[i].Op != Remove {
			i++
			continue
		}
		start := i
		for i < len(lines) && lines[i].Op == Remove {
			i++
		}
		addStart := i
		for i < len(lines) && lines[i].Op == Add {
			i++
		}
		removed, added := addStart-start, i-addStart
		for k := 0; k < removed && k < added; k++ {
			a := tokenize(expandTabs(lines[start+k].Text))
			b := tokenize(expandTabs(lines[addStart+k].Text))
			oldMarks, newMarks, similar := tokenDiff(a, b)
			if similar {
				marks[start+k] = oldMarks
				marks[addStart+k] = newMarks
			}
		}
	}
	return marks
}

// tokenDiff marks the tokens of a and b outside their longest common subsequence, and says whether
// the two lines are similar enough for word marks to help: at least half of the longer line's
// non-space characters must be shared.
func tokenDiff(a, b []string) (aMarks, bMarks []bool, similar bool) {
	if len(a)*len(b) > 40_000 {
		return nil, nil, false
	}
	cols := len(b) + 1
	table := make([]int, (len(a)+1)*cols)
	for i := len(a) - 1; i >= 0; i-- {
		for j := len(b) - 1; j >= 0; j-- {
			if a[i] == b[j] {
				table[i*cols+j] = table[(i+1)*cols+j+1] + 1
			} else {
				table[i*cols+j] = maxInt(table[(i+1)*cols+j], table[i*cols+j+1])
			}
		}
	}
	aMarks = make([]bool, len(a))
	bMarks = make([]bool, len(b))
	for k := range aMarks {
		aMarks[k] = true
	}
	for k := range bMarks {
		bMarks[k] = true
	}
	shared := 0
	i, j := 0, 0
	for i < len(a) && j < len(b) {
		switch {
		case a[i] == b[j]:
			aMarks[i], bMarks[j] = false, false
			shared += solid(a[i])
			i, j = i+1, j+1
		case table[(i+1)*cols+j] >= table[i*cols+j+1]:
			i++
		default:
			j++
		}
	}
	total := maxInt(solidAll(a), solidAll(b))
	return aMarks, bMarks, total > 0 && shared*2 >= total
}

func solid(token string) int {
	n := 0
	for _, r := range token {
		if !unicode.IsSpace(r) {
			n++
		}
	}
	return n
}

func solidAll(tokens []string) int {
	n := 0
	for _, token := range tokens {
		n += solid(token)
	}
	return n
}

// tokenize cuts text into words, runs of space, and single punctuation characters: the units a
// person sees change when a line is edited.
func tokenize(text string) []string {
	var out []string
	runes := []rune(text)
	for i := 0; i < len(runes); {
		j := i + 1
		switch {
		case isWord(runes[i]):
			for j < len(runes) && isWord(runes[j]) {
				j++
			}
		case unicode.IsSpace(runes[i]):
			for j < len(runes) && unicode.IsSpace(runes[j]) {
				j++
			}
		}
		out = append(out, string(runes[i:j]))
		i = j
	}
	return out
}

func isWord(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) }

func expandTabs(s string) string { return strings.ReplaceAll(s, "\t", "    ") }

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
