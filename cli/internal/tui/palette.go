package tui

import (
	"context"
	"fmt"
	"sort"
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/files"
)

// completion is the list under the prompt: slash commands while the prompt starts with `/`, file
// paths while the word under the cursor starts with `@`.
type completion struct {
	kind   string // "slash" or "mention"
	items  []compItem
	cursor int
	// start and end are the rune range of the prompt the chosen item replaces.
	start, end int
}

type compItem struct {
	insert string
	label  string
	detail string
	tag    string
	dir    bool
}

// maxCompletionRows is how many rows the list shows; Claude Code shows about this many before it
// scrolls, which keeps the transcript in view above.
const maxCompletionRows = 8

// refreshCompletion recomputes the list after the prompt changed.
func (m *Model) refreshCompletion() {
	if m.shell {
		m.comp = nil
		return
	}
	value := m.editor.Value()
	if strings.HasPrefix(value, "/") && !strings.ContainsAny(value, " \n") {
		m.comp = m.slashCompletion(strings.TrimPrefix(value, "/"))
		return
	}
	word, start := m.editor.WordBeforeCursor()
	if strings.HasPrefix(word, "@") {
		m.comp = m.mentionCompletion(strings.TrimPrefix(word, "@"), start, start+len([]rune(word)))
		return
	}
	m.comp = nil
}

func (m *Model) slashCompletion(query string) *completion {
	query = strings.ToLower(query)
	type scored struct {
		c     Command
		score int
	}
	var hits []scored
	for _, c := range m.allCommands() {
		score := -1
		names := append([]string{c.Name}, c.Aliases...)
		for i, n := range names {
			switch {
			case n == query:
				score = max(score, 1000-i)
			case strings.HasPrefix(n, query):
				score = max(score, 500-len(n)-i)
			case strings.Contains(n, query):
				score = max(score, 200-len(n))
			case len(query) >= 3 && strings.Contains(strings.ToLower(c.Description), query):
				// Descriptions are searched only from three letters on: "co" is in half of them.
				score = max(score, 10)
			}
		}
		if query == "" {
			score = 0
		}
		if score >= 0 {
			hits = append(hits, scored{c, score})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].score > hits[j].score })
	comp := &completion{kind: "slash", start: 0, end: len([]rune(m.editor.Value()))}
	for _, h := range hits {
		label := "/" + h.c.Name
		if h.c.Args != "" {
			label += " " + h.c.Args
		}
		comp.items = append(comp.items, compItem{insert: "/" + h.c.Name, label: label, detail: h.c.Description, tag: strings.TrimSpace(sourceTag(h.c.Source))})
	}
	return comp
}

func (m *Model) mentionCompletion(query string, start, end int) *completion {
	comp := &completion{kind: "mention", start: start, end: end}
	if m.index == nil {
		if !m.indexing {
			m.indexing = true
			cwd := m.cwdForDisplay()
			// The index is built off the update loop on the first `@`: a large folder takes a moment to
			// list, and the prompt must keep taking keys meanwhile.
			m.pendingIndex = func() tea.Msg {
				ix, err := files.Build(context.Background(), cwd, 50_000)
				if err != nil {
					return indexMsg{index: nil}
				}
				return indexMsg{index: ix}
			}
		}
		return comp
	}
	for _, match := range m.index.Match(query, 50) {
		comp.items = append(comp.items, compItem{insert: "@" + match.Path, label: match.Path, dir: strings.HasSuffix(match.Path, "/")})
	}
	return comp
}

// takePending returns the index build a completion asked for, once.
func (m *Model) takePending() tea.Cmd {
	cmd := m.pendingIndex
	m.pendingIndex = nil
	return cmd
}

// completionKey handles the keys the list owns; anything else goes on to the prompt.
func (m *Model) completionKey(msg tea.KeyPressMsg) (tea.Cmd, bool) {
	c := m.comp
	if len(c.items) == 0 {
		if msg.String() == "esc" {
			m.comp = nil
			return nil, true
		}
		return nil, false
	}
	switch msg.String() {
	case "up", "ctrl+p":
		c.cursor = (c.cursor + len(c.items) - 1) % len(c.items)
		return nil, true
	case "down", "ctrl+n":
		c.cursor = (c.cursor + 1) % len(c.items)
		return nil, true
	case "esc":
		m.comp = nil
		return nil, true
	case "tab":
		m.accept(false)
		return nil, true
	case "enter":
		return m.accept(true), true
	}
	return nil, false
}

// accept puts the chosen item into the prompt. Enter on a command runs it at once, as Claude Code
// does; Tab only completes, so arguments can follow.
func (m *Model) accept(run bool) tea.Cmd {
	c := m.comp
	item := c.items[c.cursor]
	switch c.kind {
	case "slash":
		if run {
			m.editor.SetValue(item.insert)
			m.comp = nil
			return m.submit(SendDefault)
		}
		m.editor.SetValue(item.insert + " ")
		m.comp = nil
	case "mention":
		insert := item.insert
		if !item.dir {
			insert += " "
		}
		m.editor.ReplaceRange(c.start, c.end, insert)
		m.comp = nil
		if item.dir {
			m.refreshCompletion()
		}
	}
	return nil
}

func (m *Model) completionRows() []string {
	t := m.theme
	c := m.comp
	start := 0
	if c.cursor >= maxCompletionRows {
		start = c.cursor - maxCompletionRows + 1
	}
	labelWidth := 0
	for _, item := range c.items {
		labelWidth = max(labelWidth, ansi.StringWidth(item.label))
	}
	labelWidth = min(labelWidth+2, max(m.width/2, 16))
	var out []string
	for i := start; i < len(c.items) && i < start+maxCompletionRows; i++ {
		item := c.items[i]
		label := padRight(ansi.Truncate(item.label, labelWidth-1, t.Glyphs.Ellipsis), labelWidth)
		detail := item.detail
		if item.tag != "" {
			detail += " " + item.tag
		}
		row := "  " + label + detail
		row = ansi.Truncate(row, m.width-1, t.Glyphs.Ellipsis)
		if i == c.cursor {
			out = append(out, t.Fg(t.Palette.Suggestion).Render(ansi.Strip(row)))
		} else {
			out = append(out, t.Fg(t.Palette.Text).Render("  "+label)+t.Dim().Render(ansi.Truncate(detail, max(m.width-3-labelWidth, 1), t.Glyphs.Ellipsis)))
		}
	}
	if extra := len(c.items) - maxCompletionRows; extra > 0 {
		out = append(out, t.Faint().Render(fmt.Sprintf("  +%d more", extra)))
	}
	return out
}

// reverseSearch is Ctrl+R: an incremental search back through the prompt history.
type reverseSearch struct {
	query string
	match string
	// from is the history index the next Ctrl+R searches back from.
	from int
}

func (m *Model) openSearch() {
	m.search = &reverseSearch{from: len(m.history)}
	m.comp = nil
}

func (s *reverseSearch) find(history []string) {
	s.match = ""
	if s.query == "" {
		return
	}
	needle := strings.ToLower(s.query)
	for i := min(s.from, len(history)) - 1; i >= 0; i-- {
		if strings.Contains(strings.ToLower(history[i]), needle) {
			s.match = history[i]
			s.from = i + 1
			return
		}
	}
}

// searchKey: typing narrows, Ctrl+R steps further back, Tab or Esc takes the match into the prompt
// to keep editing, Enter takes it and sends it.
func (m *Model) searchKey(msg tea.KeyPressMsg) tea.Cmd {
	s := m.search
	switch msg.String() {
	case "ctrl+r":
		if s.match != "" {
			s.from--
		}
		s.find(m.history)
		return nil
	case "esc", "tab", "right", "left":
		if s.match != "" {
			m.editor.SetValue(s.match)
		}
		m.search = nil
		return nil
	case "enter":
		m.search = nil
		if s.match == "" {
			return nil
		}
		m.editor.SetValue(s.match)
		return m.submit(SendDefault)
	case "backspace":
		if r := []rune(s.query); len(r) > 0 {
			s.query = string(r[:len(r)-1])
			s.from = len(m.history)
			s.find(m.history)
		}
		return nil
	case "ctrl+g":
		m.search = nil
		return nil
	}
	if len(msg.Text) > 0 && msg.Mod&^tea.ModShift == 0 {
		s.query += msg.Text
		s.from = len(m.history)
		s.find(m.history)
	}
	return nil
}
