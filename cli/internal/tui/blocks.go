package tui

import (
	"fmt"
	"strings"
	"time"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/diff"
	"github.com/ascorblack/daedalus/cli/internal/tui/markdown"
	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// blockKind is what one entry of the transcript is.
type blockKind int

const (
	blockWelcome blockKind = iota
	blockUser
	blockAssistant
	blockThinking
	blockTool
	blockNotice
	blockShell
	blockInterrupted
	blockTodos
	blockCompact
)

// block is one entry of the transcript: a prompt, an answer, a tool call, a notice.
//
// Each block renders itself to lines for a width and keeps the result until something about it
// changes, because the transcript of a long session is thousands of lines and is drawn ten times a
// second while the spinner turns. Only a block that is still changing (a streaming answer, a running
// tool) is rendered again on a frame; everything above it is a slice copy.
type block struct {
	kind blockKind
	id   string
	at   time.Time

	// text is the prompt, the notice or the shell command, by kind.
	text   string
	origin string
	images int
	level  NoticeLevel

	// stream is the answer or the reasoning as it arrives; done closes it.
	stream *markdown.Stream
	done   bool
	// ended is when a reasoning block finished, for "Thought for 12s".
	ended time.Time

	tool  *toolCall
	todos []Todo

	// printed is how many of the block's lines inline mode has already put into the scrollback.
	printed int

	// version counts changes; the cache is valid for one version.
	version int
	cache   renderCache
}

type renderCache struct {
	key   renderKey
	lines []string
	valid bool
}

// renderKey is everything a rendering depends on besides the block itself.
type renderKey struct {
	width        int
	version      int
	expanded     bool
	verbose      bool
	showThinking bool
	theme        string
	blink        bool
}

// toolCall is a tool block's state.
type toolCall struct {
	name     string
	input    map[string]any
	parent   string
	output   string
	progress string
	isError  bool
	finished bool
	change   *FileChange
	started  time.Time
	ended    time.Time
	// children are the calls of a subagent, drawn under its own call line.
	children []*block
}

func (b *block) touch() { b.version++ }

// renderContext carries what every block rendering needs.
type renderContext struct {
	width        int
	theme        *theme.Theme
	md           *markdown.Renderer
	cwd          string
	expanded     bool
	verbose      bool
	showThinking bool
	blink        bool
	now          time.Time
	agentName    string
}

func (b *block) live() bool {
	switch b.kind {
	case blockAssistant, blockThinking:
		return !b.done
	case blockTool:
		return !b.tool.finished || b.tool.anyChildRunning()
	}
	return false
}

func (t *toolCall) anyChildRunning() bool {
	for _, c := range t.children {
		if !c.tool.finished {
			return true
		}
	}
	return false
}

// render returns the block's lines at the context's width, from the cache when nothing changed.
func (b *block) render(ctx renderContext) []string {
	key := renderKey{
		width: ctx.width, version: b.version, expanded: ctx.expanded, verbose: ctx.verbose,
		showThinking: ctx.showThinking, theme: ctx.theme.Name,
	}
	if b.kind == blockTool && b.live() {
		key.blink = ctx.blink
	}
	// A streaming answer re-renders only its open tail (the Stream caches the rest), so it is
	// rendered on every call; everything else is cached whole.
	streaming := (b.kind == blockAssistant || b.kind == blockThinking) && !b.done
	if !streaming && b.cache.valid && b.cache.key == key {
		return b.cache.lines
	}
	lines := b.draw(ctx)
	b.cache = renderCache{key: key, lines: lines, valid: true}
	return lines
}

// Layout constants. The gutter is where the bullet sits; results hang under the call at the result
// indent, which is what lines a `⎿` up with the text of the call above it.
const (
	gutter       = 2
	resultIndent = 5
)

func (b *block) draw(ctx renderContext) []string {
	t := ctx.theme
	g := t.Glyphs
	switch b.kind {
	case blockWelcome:
		return drawWelcome(ctx, b.text)
	case blockUser:
		return drawUser(ctx, b)
	case blockAssistant:
		body := b.stream.Render(max(ctx.width-gutter, 10))
		return hang(t.Fg(t.Palette.Text).Render(g.Bullet)+" ", body, ctx.width)
	case blockThinking:
		return drawThinking(ctx, b)
	case blockTool:
		return drawTool(ctx, b, 0)
	case blockNotice:
		style := t.Dim()
		switch b.level {
		case NoticeWarning:
			style = t.Fg(t.Palette.Warning)
		case NoticeError:
			style = t.Fg(t.Palette.Error)
		}
		return resultLines(ctx, wrapPlain(b.text, ctx.width-resultIndent), style)
	case blockShell:
		head := t.Fg(t.Palette.ShellBorder).Render(g.Bang) + " " + t.Fg(t.Palette.Text).Render(oneLine(b.text))
		out := []string{head}
		output := strings.TrimRight(b.tool.output, "\n")
		if output == "" {
			output = "(no output)"
		}
		style := t.Dim()
		if b.tool.isError {
			style = t.Fg(t.Palette.Error)
		}
		return append(out, collapse(ctx, strings.Split(output, "\n"), style)...)
	case blockInterrupted:
		text := t.Fg(t.Palette.Error).Render("Interrupted") + t.Dim().Render(" · What should "+ctx.agentName+" do instead?")
		if b.text != "" {
			text = t.Fg(t.Palette.Error).Render(b.text)
		}
		return []string{hangPrefix(ctx) + text}
	case blockTodos:
		head := t.Fg(t.Palette.Success).Render(g.Bullet) + " " + t.Bold().Render("Update Todos")
		return append([]string{head}, drawTodoLines(ctx, b.todos, resultIndent, true)...)
	case blockCompact:
		line := t.Dim().Render(g.Star + " Conversation compacted · ctrl+o for history")
		if b.text != "" && ctx.expanded {
			return append([]string{line}, resultLines(ctx, wrapPlain(b.text, ctx.width-resultIndent), t.Dim())...)
		}
		return []string{line}
	}
	return nil
}

func hangPrefix(ctx renderContext) string {
	return strings.Repeat(" ", gutter) + ctx.theme.Dim().Render(ctx.theme.Glyphs.Result) + "  "
}

// resultLines hangs lines under a call: the first behind `⎿`, the rest at the same indent.
func resultLines(ctx renderContext, lines []string, style lipgloss.Style) []string {
	out := make([]string, 0, len(lines))
	for i, line := range lines {
		prefix := strings.Repeat(" ", resultIndent)
		if i == 0 {
			prefix = hangPrefix(ctx)
		}
		out = append(out, prefix+style.Render(line))
	}
	return out
}

// hang puts a marker before the first line and indents the rest under it.
func hang(marker string, body []string, width int) []string {
	out := make([]string, 0, len(body))
	pad := strings.Repeat(" ", gutter)
	for i, line := range body {
		if i == 0 {
			out = append(out, marker+line)
		} else {
			out = append(out, pad+line)
		}
	}
	if len(out) == 0 {
		out = append(out, marker)
	}
	return out
}

// wrapPlain wraps unstyled text to width, keeping its own line breaks.
func wrapPlain(text string, width int) []string {
	if width < 10 {
		width = 10
	}
	var out []string
	for _, line := range strings.Split(text, "\n") {
		wrapped := ansi.Wrap(line, width, " ")
		out = append(out, strings.Split(wrapped, "\n")...)
	}
	return out
}

// collapsedRows is how many lines of a tool's output show before the rest folds behind ctrl+o, the
// number Claude Code uses.
const collapsedRows = 3

// collapse hangs output under a call, folding everything after the first few lines into one
// `… +N lines (ctrl+o to expand)` row unless the transcript is expanded.
func collapse(ctx renderContext, lines []string, style lipgloss.Style) []string {
	width := max(ctx.width-resultIndent, 10)
	var clipped []string
	for _, line := range lines {
		line = strings.ReplaceAll(line, "\t", "    ")
		if ansi.StringWidth(line) > width {
			if ctx.expanded {
				clipped = append(clipped, strings.Split(ansi.Wrap(line, width, ""), "\n")...)
				continue
			}
			line = ansi.Truncate(line, width-1, "") + ctx.theme.Glyphs.Ellipsis
		}
		clipped = append(clipped, line)
	}
	if ctx.expanded || ctx.verbose || len(clipped) <= collapsedRows+1 {
		return resultLines(ctx, clipped, style)
	}
	hidden := len(clipped) - collapsedRows
	out := resultLines(ctx, clipped[:collapsedRows], style)
	more := fmt.Sprintf("%s +%d %s (ctrl+o to expand)", ctx.theme.Glyphs.Ellipsis, hidden, plural(hidden, "line", "lines"))
	return append(out, strings.Repeat(" ", resultIndent)+ctx.theme.Dim().Render(more))
}

func drawWelcome(ctx renderContext, cwd string) []string {
	t := ctx.theme
	width := min(ctx.width, 64)
	if width < 24 {
		width = ctx.width
	}
	inner := width - 4
	accent := t.Fg(t.Palette.Accent)
	lines := []string{
		accent.Render(t.Glyphs.Star) + " Welcome to " + t.Bold().Render(ctx.agentName) + "!",
		"",
		t.Dim().Italic(true).Render("/help for help, /status for your current setup"),
		"",
		t.Dim().Render("cwd: " + ansi.Truncate(cwd, inner-5, t.Glyphs.Ellipsis)),
	}
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).Padding(0, 1).Width(width)
	if !t.Plain {
		box = box.BorderForeground(t.Palette.Accent)
	}
	out := strings.Split(box.Render(strings.Join(lines, "\n")), "\n")
	tips := []string{
		"",
		t.Dim().Render(" Tips for getting started:"),
		"",
		t.Dim().Render(" 1. Ask a question about this folder, or ask for a change"),
		t.Dim().Render(" 2. Type @ to mention a file, ! to run a shell command, / for commands"),
		t.Dim().Render(" 3. Shift+Tab cycles the local guard: ask, auto, plan"),
	}
	return append(out, tips...)
}

func drawUser(ctx renderContext, b *block) []string {
	t := ctx.theme
	text := b.text
	if b.images > 0 {
		text = strings.TrimSpace(text + fmt.Sprintf(" [%d %s]", b.images, plural(b.images, "image", "images")))
	}
	body := wrapPlain(text, ctx.width-gutter-1)
	style := t.Dim()
	band := t.Bg(t.Palette.UserBackground)
	out := make([]string, 0, len(body)+1)
	for i, line := range body {
		prefix := "  "
		if i == 0 {
			prefix = t.Glyphs.Prompt + " "
		}
		row := prefix + line
		pad := max(ctx.width-ansi.StringWidth(row), 0)
		out = append(out, band.Render(style.Render(row)+strings.Repeat(" ", pad)))
	}
	if b.origin != "" {
		out = append(out, strings.Repeat(" ", gutter)+t.Faint().Render("from "+b.origin))
	}
	return out
}

func drawThinking(ctx renderContext, b *block) []string {
	t := ctx.theme
	style := t.Dim().Italic(true)
	head := t.Glyphs.Star + " Thinking" + t.Glyphs.Ellipsis
	if b.done {
		d := b.ended.Sub(b.at)
		if b.ended.IsZero() || d < time.Second {
			d = time.Second
		}
		head = fmt.Sprintf("%s Thought for %s", t.Glyphs.Star, formatElapsed(d))
		if !ctx.expanded && !ctx.showThinking {
			head += " (ctrl+o to show thinking)"
		}
	}
	out := []string{style.Render(head)}
	if ctx.expanded || ctx.showThinking {
		for _, line := range wrapPlain(strings.TrimSpace(b.stream.Text()), ctx.width-gutter) {
			out = append(out, strings.Repeat(" ", gutter)+style.Render(line))
		}
	}
	return out
}

// drawTool is a call line, its result and, for a subagent, its own calls hung under it.
func drawTool(ctx renderContext, b *block, depth int) []string {
	t := ctx.theme
	g := t.Glyphs
	call := b.tool
	display, arg := toolTitle(call.name, call.input, ctx.cwd)

	bulletStyle := t.Fg(t.Palette.Success)
	switch {
	case !call.finished:
		// A running call's bullet blinks between bright and dim, which is how Claude Code shows a
		// call in flight without spending a line on it.
		bulletStyle = t.Fg(t.Palette.Text)
		if !ctx.blink {
			bulletStyle = t.Faint()
		}
	case call.isError:
		bulletStyle = t.Fg(t.Palette.Error)
	}
	title := t.Bold().Render(display)
	if arg != "" {
		width := ctx.width - gutter - ansi.StringWidth(display) - 2
		if !ctx.expanded && ansi.StringWidth(arg) > width && width > 4 {
			arg = ansi.Truncate(arg, width-1, "") + g.Ellipsis
		}
		title += t.Fg(t.Palette.Text).Render("(" + arg + ")")
	}
	out := []string{bulletStyle.Render(g.Bullet) + " " + title}

	if len(call.children) > 0 {
		out = append(out, drawChildren(ctx, call)...)
	}
	if !call.finished {
		if call.progress != "" {
			return append(out, collapse(ctx, tailLines(call.progress, collapsedRows+1), t.Dim())...)
		}
		if len(call.children) == 0 {
			out = append(out, hangPrefix(ctx)+t.Dim().Render("Running"+g.Ellipsis))
		}
		return out
	}
	if call.change != nil && !call.isError {
		return append(out, drawChange(ctx, call)...)
	}
	if call.name == "SubAgent" || call.name == "Task" {
		d := call.ended.Sub(call.started)
		summary := fmt.Sprintf("Done (%d tool %s · %s)", len(call.children), plural(len(call.children), "use", "uses"), formatElapsed(d))
		if call.isError {
			return append(out, collapse(ctx, strings.Split(strings.TrimRight(call.output, "\n"), "\n"), t.Fg(t.Palette.Error))...)
		}
		out = append(out, hangPrefix(ctx)+t.Dim().Render(summary))
		if ctx.expanded && strings.TrimSpace(call.output) != "" {
			out = append(out, resultLines(ctx, wrapPlain(call.output, ctx.width-resultIndent), t.Dim())[0:]...)
		}
		return out
	}
	if call.isError {
		text := strings.TrimRight(call.output, "\n")
		if text == "" {
			text = "failed"
		}
		lines := strings.Split(text, "\n")
		lines[0] = "Error: " + lines[0]
		return append(out, collapse(ctx, lines, t.Fg(t.Palette.Error))...)
	}
	if summary := toolSummary(call.name, call.input, call.output, nil); summary != "" && !ctx.expanded {
		return append(out, hangPrefix(ctx)+t.Dim().Render(summary))
	}
	text := strings.TrimRight(call.output, "\n")
	if text == "" {
		return append(out, hangPrefix(ctx)+t.Dim().Render("(no output)"))
	}
	return append(out, collapse(ctx, strings.Split(text, "\n"), t.Dim())...)
}

// drawChildren lists a subagent's calls, the last few while it works and all of them expanded.
func drawChildren(ctx renderContext, call *toolCall) []string {
	t := ctx.theme
	children := call.children
	hidden := 0
	if !ctx.expanded && len(children) > collapsedRows {
		hidden = len(children) - collapsedRows
		children = children[hidden:]
	}
	var lines []string
	for _, c := range children {
		display, arg := toolTitle(c.tool.name, c.tool.input, ctx.cwd)
		line := display
		if arg != "" {
			line += "(" + arg + ")"
		}
		if !c.tool.finished {
			line += " " + t.Glyphs.Ellipsis
		}
		lines = append(lines, line)
	}
	out := resultLines(ctx, truncateAll(lines, ctx.width-resultIndent, t.Glyphs.Ellipsis), t.Dim())
	if hidden > 0 {
		out = append(out, strings.Repeat(" ", resultIndent)+t.Faint().Render(fmt.Sprintf("+%d more tool %s", hidden, plural(hidden, "use", "uses"))))
	}
	return out
}

func truncateAll(lines []string, width int, tail string) []string {
	for i, line := range lines {
		if ansi.StringWidth(line) > width {
			lines[i] = ansi.Truncate(line, width-1, "") + tail
		}
	}
	return lines
}

// drawChange is the summary and the inline diff of a file edit.
func drawChange(ctx renderContext, call *toolCall) []string {
	t := ctx.theme
	lines := diff.Lines(call.change.Before, call.change.After)
	added, removed := diff.Count(lines)
	path := relPath(call.change.Path, ctx.cwd)
	var summary string
	if call.change.Before == "" {
		summary = fmt.Sprintf("Wrote %d %s to %s", countLines(call.change.After), plural(countLines(call.change.After), "line", "lines"), path)
	} else {
		summary = fmt.Sprintf("Updated %s with %d %s and %d %s", t.Bold().Render(path), added, plural(added, "addition", "additions"), removed, plural(removed, "removal", "removals"))
	}
	out := []string{hangPrefix(ctx) + t.Dim().Render(summary)}
	limit := 40
	if ctx.expanded {
		limit = 0
	}
	if call.change.Before == "" && !ctx.expanded {
		limit = 10
	}
	for _, row := range diff.Render(t, lines, max(ctx.width-resultIndent-1, 20), limit) {
		out = append(out, strings.Repeat(" ", resultIndent+1)+row)
	}
	return out
}

func tailLines(text string, n int) []string {
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return lines
}

// drawTodoLines draws a todo list at an indent; hang puts the first row behind `⎿`.
func drawTodoLines(ctx renderContext, items []Todo, indent int, hangFirst bool) []string {
	t := ctx.theme
	g := t.Glyphs
	var out []string
	for i, item := range items {
		var mark, text string
		switch item.Status {
		case TodoDone:
			mark = t.Fg(t.Palette.Success).Render(g.BoxChecked)
			text = t.Dim().Strikethrough(true).Render(item.Text)
		case TodoActive:
			mark = t.Fg(t.Palette.Permission).Render(g.BoxActive)
			text = t.Bold().Render(item.Text)
		default:
			mark = t.Dim().Render(g.Box)
			text = t.Fg(t.Palette.Text).Render(item.Text)
		}
		prefix := strings.Repeat(" ", indent)
		if i == 0 && hangFirst {
			prefix = hangPrefix(ctx)
		}
		out = append(out, prefix+mark+" "+text)
	}
	return out
}
