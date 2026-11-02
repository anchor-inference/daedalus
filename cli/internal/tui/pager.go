package tui

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

// pager is the Ctrl+O transcript view: every block expanded (whole tool outputs, the model's
// reasoning, full diffs) with less-style keys. It is a separate screen rather than an expansion in
// place, so toggling it never moves the live transcript the person was reading.
type pager struct {
	// top is the first line shown; follow keeps the view at the end as the transcript grows.
	top    int
	follow bool
	lines  []string
	width  int
	query  string
	typing bool
	hits   []int
	hit    int
}

func (m *Model) openPager() {
	m.pager = &pager{follow: true}
}

func (p *pager) build(m *Model) {
	if p.width == m.width && len(p.lines) > 0 && !p.follow {
		return
	}
	ctx := m.renderContext(m.width, true)
	var out []string
	for i, b := range m.blocks {
		if b.kind == blockWelcome {
			continue
		}
		if i > 1 {
			out = append(out, "")
		}
		if !b.at.IsZero() && (b.kind == blockUser || b.kind == blockTool) {
			out = append(out, m.theme.Faint().Render(b.at.Format("15:04:05")))
		}
		out = append(out, b.render(ctx)...)
	}
	p.lines, p.width = out, m.width
}

func (p *pager) height(m *Model) int { return max(m.height-2, 1) }

func (p *pager) view(m *Model) string {
	p.build(m)
	t := m.theme
	h := p.height(m)
	if p.follow {
		p.top = max(len(p.lines)-h, 0)
	}
	p.top = max(min(p.top, len(p.lines)-h), 0)
	end := min(p.top+h, len(p.lines))
	window := append([]string(nil), p.lines[p.top:end]...)
	for len(window) < h {
		window = append(window, "")
	}
	title := t.Fg(t.Palette.Accent).Render(t.Glyphs.Star) + t.Bold().Render(" Transcript")
	pos := fmt.Sprintf("%d–%d of %d", p.top+1, end, len(p.lines))
	header := joinEnds(" "+title, t.Dim().Render(pos)+" ", m.width)
	footer := t.Dim().Render(" esc/q/ctrl+o to return · j/k ↑/↓ scroll · space/b page · g/G top/bottom · / search")
	if p.typing {
		footer = " " + t.Fg(t.Palette.Permission).Render("/") + p.query + t.Faint().Render("▏")
	} else if p.query != "" {
		footer = t.Dim().Render(fmt.Sprintf(" /%s · %d matches · n/N next/previous · esc to return", p.query, len(p.hits)))
	}
	return strings.Join(append(append([]string{header}, window...), ansi.Truncate(footer, m.width, "")), "\n")
}

func (p *pager) scroll(m *Model, delta int) {
	p.follow = false
	p.top += delta
	if p.top < 0 {
		p.top = 0
	}
	if limit := max(len(p.lines)-p.height(m), 0); p.top >= limit {
		p.top = limit
		p.follow = true
	}
}

func (p *pager) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	if p.typing {
		switch msg.String() {
		case "enter":
			p.typing = false
			p.search(m)
		case "esc":
			p.typing, p.query = false, ""
		case "backspace":
			if r := []rune(p.query); len(r) > 0 {
				p.query = string(r[:len(r)-1])
			}
		default:
			if len(msg.Text) > 0 {
				p.query += msg.Text
			}
		}
		return nil
	}
	h := p.height(m)
	switch msg.String() {
	case "esc", "q", "ctrl+o", "ctrl+c":
		m.pager = nil
	case "j", "down", "enter":
		p.scroll(m, 1)
	case "k", "up":
		p.scroll(m, -1)
	case "space", "pgdown", "f", "ctrl+f":
		p.scroll(m, h-1)
	case "b", "pgup", "ctrl+b":
		p.scroll(m, -(h - 1))
	case "d", "ctrl+d":
		p.scroll(m, h/2)
	case "u", "ctrl+u":
		p.scroll(m, -h/2)
	case "g", "home":
		p.follow = false
		p.top = 0
	case "G", "end":
		p.follow = true
	case "/":
		p.typing, p.query = true, ""
	case "n":
		p.jump(m, 1)
	case "N":
		p.jump(m, -1)
	}
	return nil
}

func (p *pager) search(m *Model) {
	p.hits, p.hit = nil, 0
	if p.query == "" {
		return
	}
	needle := strings.ToLower(p.query)
	for i, line := range p.lines {
		if strings.Contains(strings.ToLower(ansi.Strip(line)), needle) {
			p.hits = append(p.hits, i)
		}
	}
	if len(p.hits) > 0 {
		// Start from the last match: the transcript is read from the bottom, where the person was.
		p.hit = len(p.hits) - 1
		p.follow = false
		p.top = max(p.hits[p.hit]-p.height(m)/2, 0)
	}
}

func (p *pager) jump(m *Model, delta int) {
	if len(p.hits) == 0 {
		return
	}
	p.hit = (p.hit + delta + len(p.hits)) % len(p.hits)
	p.follow = false
	p.top = max(p.hits[p.hit]-p.height(m)/2, 0)
}

func (p *pager) wheel(m *Model, msg tea.MouseWheelMsg) tea.Cmd {
	if msg.Button == tea.MouseWheelUp {
		p.scroll(m, -3)
	} else if msg.Button == tea.MouseWheelDown {
		p.scroll(m, 3)
	}
	return nil
}
