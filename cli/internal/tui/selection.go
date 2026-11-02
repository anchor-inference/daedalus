package tui

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
)

// selection is text being selected with the mouse in the fullscreen transcript.
//
// Fullscreen takes mouse reporting so the wheel scrolls the transcript, and with it the terminal's
// own drag-selection goes. Claude Code's fullscreen answers that by selecting itself, and so does
// this: a drag highlights the cells it covers and the release copies their text with OSC 52, which
// reaches the local clipboard even through SSH and tmux. Shift+drag still gets the terminal's own
// selection in every terminal that honours it.
type selection struct {
	fromX, fromY int
	toX, toY     int
}

// ordered returns the selection's ends in reading order.
func (s *selection) ordered() (x0, y0, x1, y1 int) {
	x0, y0, x1, y1 = s.fromX, s.fromY, s.toX, s.toY
	if y1 < y0 || (y1 == y0 && x1 < x0) {
		x0, y0, x1, y1 = x1, y1, x0, y0
	}
	return x0, y0, x1, y1
}

func (m *Model) mouseClick(msg tea.MouseClickMsg) tea.Cmd {
	if msg.Button != tea.MouseLeft || m.inline() || m.pager != nil {
		return nil
	}
	if msg.Y >= m.transcriptHeight() {
		m.sel = nil
		return nil
	}
	m.sel = &selection{fromX: msg.X, fromY: msg.Y, toX: msg.X, toY: msg.Y}
	return nil
}

func (m *Model) mouseMotion(msg tea.MouseMotionMsg) tea.Cmd {
	if m.sel == nil || msg.Button != tea.MouseLeft {
		return nil
	}
	m.sel.toX = max(msg.X, 0)
	m.sel.toY = min(max(msg.Y, 0), m.transcriptHeight()-1)
	return nil
}

func (m *Model) mouseRelease(msg tea.MouseReleaseMsg) tea.Cmd {
	if m.sel == nil {
		return nil
	}
	s := m.sel
	m.sel = nil
	if s.fromX == s.toX && s.fromY == s.toY {
		// A click, not a drag: nothing to copy.
		return nil
	}
	text := selectedText(m.transcriptWindow(m.transcriptHeight()), s, m.width)
	if strings.TrimSpace(text) == "" {
		return nil
	}
	m.flash("Copied " + itoa(len([]rune(text))) + " characters")
	return tea.Batch(tea.SetClipboard(text), m.tick())
}

// selectedText is the plain text a selection covers: from its start to the end of the first row,
// whole rows between, and up to its end on the last row, as a terminal selects.
func selectedText(lines []string, s *selection, width int) string {
	x0, y0, x1, y1 := s.ordered()
	var rows []string
	for y := y0; y <= y1 && y < len(lines); y++ {
		if y < 0 {
			continue
		}
		left, right := 0, width
		if y == y0 {
			left = x0
		}
		if y == y1 {
			right = x1 + 1
		}
		rows = append(rows, strings.TrimRight(ansi.Strip(ansi.Cut(lines[y], left, right)), " "))
	}
	return strings.Join(rows, "\n")
}

// highlight draws the selection over the window's lines.
func (m *Model) highlight(lines []string) []string {
	if m.sel == nil {
		return lines
	}
	x0, y0, x1, y1 := m.sel.ordered()
	style := m.theme.Bg(m.theme.Palette.Selection).Foreground(m.theme.Palette.Text)
	if m.theme.Plain {
		style = style.Reverse(true)
	}
	out := append([]string(nil), lines...)
	for y := max(y0, 0); y <= y1 && y < len(out); y++ {
		left, right := 0, m.width
		if y == y0 {
			left = x0
		}
		if y == y1 {
			right = x1 + 1
		}
		line := padRight(out[y], m.width)
		mid := ansi.Strip(ansi.Cut(line, left, right))
		out[y] = ansi.Cut(line, 0, left) + style.Render(mid) + ansi.Cut(line, right, m.width)
	}
	return out
}
