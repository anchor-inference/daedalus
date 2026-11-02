package tui

import (
	"fmt"
	"path/filepath"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

func (m *Model) renderContext(width int, expanded bool) renderContext {
	return renderContext{
		width:        width,
		theme:        m.theme,
		md:           m.md,
		cwd:          m.cwdForDisplay(),
		expanded:     expanded,
		verbose:      m.prefs.Verbose,
		showThinking: m.prefs.ShowThinking,
		blink:        m.frame%10 < 6,
		now:          m.now(),
		agentName:    m.options.AgentName,
	}
}

func (m *Model) cwdForDisplay() string {
	if m.session.Cwd != "" {
		return m.session.Cwd
	}
	return m.options.Cwd
}

// View draws the frame: the transcript filling the window, and the live area pinned under it.
func (m *Model) View() tea.View {
	if m.quitting {
		return tea.NewView("")
	}
	if m.width == 0 || m.height == 0 {
		v := tea.NewView("")
		v.AltScreen = !m.inline()
		return v
	}
	if m.pager != nil {
		v := tea.NewView(m.pager.view(m))
		v.AltScreen = true
		v.MouseMode = tea.MouseModeCellMotion
		return v
	}
	if !m.viewed {
		m.viewed, m.viewedAt = true, time.Now()
	}
	bottom, cursorRow, cursorCol := m.bottom()
	var lines []string
	if m.inline() {
		lines = m.liveLines(max(m.height-len(bottom)-1, 1))
	} else {
		lines = m.highlight(m.transcriptWindow(max(m.height-len(bottom), 1)))
	}
	top := len(lines)
	content := strings.Join(append(lines, bottom...), "\n")
	v := tea.NewView(content)
	v.AltScreen = !m.inline()
	if !m.inline() {
		// Mouse reporting gives the wheel to the transcript. It takes plain drag-selection from the
		// terminal, which is why the help names Shift+drag, the override every terminal honours.
		v.MouseMode = tea.MouseModeCellMotion
	}
	v.ReportFocus = true
	v.WindowTitle = m.windowTitle()
	if cursorRow >= 0 && m.focus {
		c := tea.NewCursor(cursorCol, top+cursorRow)
		c.Blink = false
		c.Shape = tea.CursorBar
		v.Cursor = c
	}
	return v
}

func (m *Model) windowTitle() string {
	title := m.session.Title
	if title == "" || title == "New session" {
		title = filepath.Base(m.cwdForDisplay())
	}
	mark := m.theme.Glyphs.Star
	if m.running {
		mark = m.theme.Glyphs.SpinnerFrame(m.frame / 2)
	}
	return mark + " " + title
}

// transcriptLines renders every block with a blank line between them.
func (m *Model) transcriptLines(width int, from int) []string {
	ctx := m.renderContext(width, false)
	var out []string
	for i := from; i < len(m.blocks); i++ {
		if i > from {
			out = append(out, "")
		}
		out = append(out, m.blocks[i].render(ctx)...)
	}
	return out
}

// transcriptWindow is the slice of the transcript the window shows, bottom-anchored and scrolled up
// by m.scroll lines. Short transcripts are padded at the top so the prompt stays at the bottom, the
// way a terminal fills.
func (m *Model) transcriptWindow(height int) []string {
	all := m.transcriptLines(m.width, 0)
	end := len(all) - m.scroll
	if end < 0 {
		end = 0
	}
	start := max(end-height, 0)
	window := append([]string(nil), all[start:end]...)
	if m.scroll > 0 && len(window) > 0 {
		label := " ↓ Jump to bottom (ctrl+end)"
		if m.unseen > 0 {
			label = fmt.Sprintf(" ↓ Jump to bottom · %d new %s (ctrl+end)", m.unseen, plural(m.unseen, "message", "messages"))
		}
		window[len(window)-1] = m.theme.Fg(m.theme.Palette.Permission).Bold(true).Render(padRight(label, m.width))
	}
	for len(window) < height {
		window = append([]string{""}, window...)
	}
	return window
}

// liveLines is what the inline view draws above the prompt: the blocks not yet committed to the
// scrollback, cut to their last lines so the live region stays shorter than the window. A live
// region taller than the window forces the terminal to scroll a redraw, which loses scrollback.
func (m *Model) liveLines(limit int) []string {
	ctx := m.renderContext(m.width, false)
	var lines []string
	for i := m.committed; i < len(m.blocks); i++ {
		b := m.blocks[i]
		if b.kind == blockWelcome {
			// The banner is printed straight into the scrollback a moment after start and never
			// drawn live: inserting thirteen lines above a frame that already fills most of the
			// window trips Bubble Tea's inline renderer, which then leaves the banner's top rows
			// behind twice.
			continue
		}
		rendered := b.render(ctx)
		if b.printed > 0 {
			rendered = rendered[min(b.printed, len(rendered)):]
		} else if i > 0 {
			lines = append(lines, "")
		}
		lines = append(lines, rendered...)
	}
	// Half the window at most: the open block of a streaming answer is usually a paragraph, and a
	// long code block shows its tail until it closes and is printed whole.
	limit = min(limit, max(m.height/2, 6))
	if len(lines) > limit {
		lines = lines[len(lines)-limit:]
	}
	return lines
}

func (m *Model) transcriptHeight() int {
	bottom, _, _ := m.bottom()
	return max(m.height-len(bottom), 1)
}

func (m *Model) maxScroll() int {
	total := len(m.transcriptLines(m.width, 0))
	return max(total-m.transcriptHeight(), 0)
}

// bottom is the live area under the transcript: status rows, then the dialog or the prompt box, then
// the footer or the completion list. It returns where the cursor goes, or -1 when the prompt does
// not have the focus.
func (m *Model) bottom() (lines []string, cursorRow, cursorCol int) {
	cursorRow = -1
	lines = append(lines, m.statusRows()...)
	if len(lines) == 0 {
		// One blank row between the transcript and the prompt, as Claude Code leaves it; the status
		// rows bring their own when they are shown.
		lines = append(lines, "")
	}
	if m.dialog != nil {
		lines = append(lines, m.dialog.view(m)...)
		return lines, -1, 0
	}
	box, row, col := m.promptBox()
	cursorRow, cursorCol = len(lines)+row, col
	lines = append(lines, box...)
	switch {
	case m.comp != nil && len(m.comp.items) > 0:
		lines = append(lines, m.completionRows()...)
	case m.comp != nil && m.comp.kind == "slash":
		lines = append(lines, m.theme.Dim().Render(fmt.Sprintf("  No commands match %q", m.editor.Value())), "")
	case m.help:
		lines = append(lines, m.helpRows()...)
	default:
		lines = append(lines, m.footerRows()...)
	}
	return lines, cursorRow, cursorCol
}

// statusRows are the queued messages, the spinner and the todo list above the prompt.
func (m *Model) statusRows() []string {
	t := m.theme
	g := t.Glyphs
	var out []string
	for _, q := range m.queue {
		text := ansi.Truncate(oneLine(q.Text), m.width-4, g.Ellipsis)
		out = append(out, t.Faint().Render(g.Prompt+" "+text))
	}
	switch {
	case m.running && m.waitingDevice != "" && m.conn == Online:
		line := t.Fg(t.Palette.Warning).Render(g.Pause+" Waiting for "+m.waitingDevice+" to reconnect"+g.Ellipsis+" turn paused") +
			t.Dim().Render(" (its tools run there · esc to stop)")
		out = append(out, "", ansi.Truncate(line, m.width, g.Ellipsis))
	case m.running && m.conn == Reconnecting:
		retry := ""
		if m.connRetry > 0 {
			retry = " · retry in " + formatElapsed(m.connRetry)
		}
		line := t.Fg(t.Palette.Warning).Render(g.Pause+" Reconnecting"+g.Ellipsis+" turn paused") +
			t.Dim().Render(fmt.Sprintf(" (attempt %d%s · the server keeps the turn · esc to interrupt)", m.connAttempt, retry))
		out = append(out, "", ansi.Truncate(line, m.width, g.Ellipsis))
	case m.running:
		elapsed := m.now().Sub(m.turnStart)
		tokens := max(m.turnTokens, m.turnChars/4)
		glyph := t.Fg(t.Palette.Accent).Render(g.SpinnerFrame(m.frame))
		verb := m.shimmer(m.verb+g.Ellipsis, m.frame)
		detail := "esc to interrupt · " + formatElapsed(elapsed)
		if tokens > 0 {
			detail += " · " + g.ArrowUp + " " + formatTokens(tokens) + " tokens"
		}
		line := glyph + " " + verb + " " + t.Dim().Render("("+detail+")")
		out = append(out, "", ansi.Truncate(line, m.width, g.Ellipsis))
	case m.compacting:
		line := t.Fg(t.Palette.Accent).Render(g.SpinnerFrame(m.frame)) + " " + m.shimmer("Compacting conversation"+g.Ellipsis, m.frame) +
			t.Dim().Render(" ("+formatElapsed(m.now().Sub(m.compactAt))+")")
		out = append(out, "", line)
	}
	if m.showTodo && len(m.todos) > 0 && (m.running || m.todosOpen()) {
		items := m.todos
		if len(items) > 5 {
			items = visibleTodos(items, 5)
		}
		if len(out) == 0 {
			out = append(out, "")
		}
		out = append(out, drawTodoLines(m.renderContext(m.width, false), items, resultIndent, true)...)
	}
	return out
}

func (m *Model) todosOpen() bool {
	for _, item := range m.todos {
		if item.Status != TodoDone {
			return true
		}
	}
	return false
}

// visibleTodos picks n items around the active one, so the panel shows what is happening now rather
// than the first five of a long list.
func visibleTodos(items []Todo, n int) []Todo {
	active := 0
	for i, item := range items {
		if item.Status == TodoActive {
			active = i
			break
		}
		if item.Status == TodoPending {
			active = i
			break
		}
	}
	start := max(min(active-1, len(items)-n), 0)
	return items[start : start+n]
}

// promptBox is the bordered input with its `>` marker, and where the cursor sits inside it.
func (m *Model) promptBox() (lines []string, cursorRow, cursorCol int) {
	t := m.theme
	inner := max(m.width-4, 10)
	prefix := t.Glyphs.Prompt + " "
	border := t.Palette.Border
	if m.shell {
		prefix = t.Glyphs.Bang + " "
		border = t.Palette.ShellBorder
	}
	textWidth := max(inner-2, 4)

	var body []string
	if m.search != nil {
		label := fmt.Sprintf("(reverse-i-search)`%s': ", m.search.query)
		match := m.search.match
		if match == "" && m.search.query != "" {
			label = fmt.Sprintf("(failed reverse-i-search)`%s': ", m.search.query)
		}
		body = []string{t.Dim().Render(label) + ansi.Truncate(oneLine(match), max(inner-ansi.StringWidth(label), 1), t.Glyphs.Ellipsis)}
		cursorRow, cursorCol = 1, 2+min(ansi.StringWidth(label), inner)
	} else {
		rows, row, col := m.editor.Render(textWidth)
		placeholder := ""
		if m.editor.Empty() {
			placeholder = m.placeholder()
		}
		for i, r := range rows {
			lead := "  "
			if i == 0 {
				lead = prefix
				if m.shell {
					lead = t.Fg(t.Palette.ShellBorder).Render(prefix)
				}
			}
			if i == 0 && placeholder != "" {
				r = t.Faint().Render(ansi.Truncate(placeholder, textWidth, t.Glyphs.Ellipsis))
			}
			body = append(body, lead+r)
		}
		cursorRow, cursorCol = 1+row, 2+2+col
	}
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).Padding(0, 1).Width(m.width)
	if !t.Plain {
		box = box.BorderForeground(border)
	}
	lines = strings.Split(box.Render(strings.Join(body, "\n")), "\n")
	return lines, cursorRow, cursorCol
}

func (m *Model) placeholder() string {
	switch {
	case m.shell:
		return "Run a shell command in this folder"
	case m.running:
		return "Type to steer · tab to queue for after this turn"
	case !m.hasPrompted():
		return `Try "explain what this project does"`
	}
	return ""
}

func (m *Model) hasPrompted() bool {
	for _, b := range m.blocks {
		if b.kind == blockUser {
			return true
		}
	}
	return false
}

// footerRows are the guard indicator and hints on the left, the context meter on the right, and the
// status line under them.
func (m *Model) footerRows() []string {
	t := m.theme
	g := t.Glyphs
	now := m.now()
	var left string
	switch {
	case m.hint != "" && now.Before(m.hintUntil):
		left = t.Dim().Render(m.hint)
	case m.toast != "" && now.Before(m.toastUntil):
		left = t.Fg(t.Palette.Permission).Render(m.toast)
	case m.shell:
		left = t.Fg(t.Palette.ShellBorder).Render("! for shell mode") + t.Dim().Render(" · esc to exit")
	case m.editor.VimMode() == "NORMAL":
		left = t.Dim().Render("-- NORMAL --")
	case m.editor.VimMode() == "INSERT" && !m.editor.Empty():
		left = t.Dim().Render("-- INSERT --")
	default:
		left = m.guardIndicator()
	}
	if len(m.queue) > 0 && m.editor.Empty() && (m.hint == "" || now.After(m.hintUntil)) {
		left += t.Dim().Render(" · " + g.ArrowUp + " to edit queued")
	}
	right := m.contextMeter()
	row := joinEnds(" "+left, right+" ", m.width)
	return []string{row, " " + m.statusLine()}
}

// guardIndicator is the one-line mark of the local guard, in Claude Code's words and colours.
func (m *Model) guardIndicator() string {
	t := m.theme
	g := t.Glyphs
	hint := t.Dim().Render(" (shift+tab to cycle)")
	switch m.guard {
	case GuardAsk:
		return t.Fg(t.Palette.Permission).Render(g.Pause+" ask before acting") + hint
	case GuardPlan:
		return t.Fg(t.Palette.Plan).Render(g.Pause+" plan mode on · read-only") + hint
	}
	return t.Fg(t.Palette.Auto).Render(g.Play+" auto-accept on") + hint
}

// contextMeter shows how much context is left once it matters, as Claude Code does: nothing while
// there is plenty, a quiet percentage under a fifth, a warning under a tenth.
func (m *Model) contextMeter() string {
	t := m.theme
	if m.usage.ContextWindow <= 0 {
		return ""
	}
	left := 100 - m.usage.ContextTokens*100/m.usage.ContextWindow
	if left < 0 {
		left = 0
	}
	switch {
	case left <= 10:
		return t.Fg(t.Palette.Error).Render(fmt.Sprintf("Context low (%d%% remaining) · Run /compact to compact & continue", left))
	case left <= 20:
		return t.Fg(t.Palette.Warning).Render(fmt.Sprintf("Context left until auto-compact: %d%%", left))
	}
	return t.Faint().Render(fmt.Sprintf("%d%% context left", left))
}

// statusLine names the model, the folder and branch, and the link, dimmed.
func (m *Model) statusLine() string {
	t := m.theme
	g := t.Glyphs
	var parts []string
	if m.session.Model != "" {
		parts = append(parts, m.session.Model+effortSuffix(m.session.Effort))
	}
	place := shortPath(m.cwdForDisplay())
	if m.branch != "" {
		place += " (" + m.branch + ")"
	}
	parts = append(parts, place)
	line := t.Dim().Render(strings.Join(parts, " "+g.Separator+" "))
	line += t.Dim().Render(" "+g.Separator+" ") + m.connLabel()
	return ansi.Truncate(line, m.width-2, g.Ellipsis)
}

func (m *Model) connLabel() string {
	t := m.theme
	switch m.conn {
	case Online:
		return t.Fg(t.Palette.Success).Render(t.Glyphs.Bullet) + t.Dim().Render(" online")
	case Connecting:
		return t.Fg(t.Palette.Warning).Render(t.Glyphs.SpinnerFrame(m.frame)) + t.Dim().Render(" connecting")
	case Reconnecting:
		return t.Fg(t.Palette.Warning).Render(t.Glyphs.SpinnerFrame(m.frame)) + t.Fg(t.Palette.Warning).Render(" reconnecting")
	case Unauthorized:
		return t.Fg(t.Palette.Error).Render(t.Glyphs.Cross + " not paired · /login")
	}
	return t.Fg(t.Palette.Error).Render(t.Glyphs.Cross + " offline")
}

// shortPath writes the home folder as ~.
func shortPath(p string) string {
	if home := homeDir(); home != "" && (p == home || strings.HasPrefix(p, home+string(filepath.Separator))) {
		return "~" + p[len(home):]
	}
	return p
}

// joinEnds puts left and right on one row of width, dropping the right side when they do not fit.
func joinEnds(left, right string, width int) string {
	lw, rw := ansi.StringWidth(left), ansi.StringWidth(right)
	if lw+rw+1 > width {
		return ansi.Truncate(left, width, "")
	}
	return left + strings.Repeat(" ", width-lw-rw) + right
}

func (m *Model) helpRows() []string {
	t := m.theme
	cols := [][2]string{
		{"! for shell mode", "double esc to rewind"},
		{"/ for commands", "shift+tab to cycle guard"},
		{"@ for file paths", "ctrl+o for transcript"},
		{"\\⏎ or shift+⏎ for newline", "ctrl+r to search history"},
		{"ctrl+g to edit in $EDITOR", "ctrl+t to toggle todos"},
		{"ctrl+v to paste an image", "pgup/pgdn to scroll · drag to copy"},
	}
	half := max(m.width/2-2, 20)
	var out []string
	for _, c := range cols {
		out = append(out, " "+t.Dim().Render(padRight(c[0], half))+t.Dim().Render(c[1]))
	}
	return out
}

var homeDirCache = struct {
	value string
	set   bool
}{}

func homeDir() string {
	if !homeDirCache.set {
		homeDirCache.value, _ = userHome()
		homeDirCache.set = true
	}
	return homeDirCache.value
}

// relativeTime is how long ago, in the words of a session list: "25m ago", "3h ago", "2d ago".
func relativeTime(now, t time.Time) string {
	d := now.Sub(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d/time.Minute))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d/time.Hour))
	}
	return fmt.Sprintf("%dd ago", int(d/(24*time.Hour)))
}
