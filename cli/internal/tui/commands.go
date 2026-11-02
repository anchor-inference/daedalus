package tui

import (
	"context"
	"fmt"
	"sort"
	"strings"

	tea "charm.land/bubbletea/v2"

	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// builtins are the slash commands this client answers itself. Server commands and skills are merged
// in from Backend.Commands; a builtin of the same name wins, since these change the client's own
// state and the server cannot.
var builtins = []Command{
	{Name: "help", Description: "Show keys and commands"},
	{Name: "clear", Description: "Start a new session in this folder", Aliases: []string{"new", "reset"}},
	{Name: "compact", Description: "Summarise the conversation to free context", Args: "[focus]"},
	{Name: "resume", Description: "Resume a session", Aliases: []string{"continue"}},
	{Name: "rewind", Description: "Restore the code and/or conversation to an earlier prompt", Aliases: []string{"checkpoint", "undo"}},
	{Name: "model", Description: "Choose the model and its effort"},
	{Name: "config", Description: "Settings: theme, editor mode, fullscreen, thinking", Aliases: []string{"settings"}},
	{Name: "status", Description: "Show the server, device, session and model"},
	{Name: "login", Description: "Pair this terminal with the server"},
	{Name: "theme", Description: "Change the colour theme"},
	{Name: "vim", Description: "Toggle vim editing in the prompt"},
	{Name: "todos", Description: "Show the agent's todo list"},
	{Name: "copy", Description: "Copy the last answer to the clipboard"},
	{Name: "cost", Description: "Show the session's token use", Aliases: []string{"usage"}},
	{Name: "context", Description: "Show how full the context window is"},
	{Name: "exit", Description: "Exit", Aliases: []string{"quit"}},
}

// allCommands is the palette's list: the builtins, then the server's commands and skills by name.
func (m *Model) allCommands() []Command {
	out := append([]Command(nil), builtins...)
	seen := map[string]bool{}
	for _, c := range builtins {
		seen[c.Name] = true
		for _, a := range c.Aliases {
			seen[a] = true
		}
	}
	extra := make([]Command, 0, len(m.commands))
	for _, c := range m.commands {
		if !seen[c.Name] {
			extra = append(extra, c)
			seen[c.Name] = true
		}
	}
	sort.SliceStable(extra, func(i, j int) bool { return extra[i].Name < extra[j].Name })
	return append(out, extra...)
}

func (m *Model) findCommand(name string) (Command, bool) {
	for _, c := range m.allCommands() {
		if c.Name == name {
			return c, true
		}
		for _, a := range c.Aliases {
			if a == name {
				return c, true
			}
		}
	}
	return Command{}, false
}

// runSlash runs a line that starts with a slash.
func (m *Model) runSlash(line string) tea.Cmd {
	name, args, _ := strings.Cut(strings.TrimPrefix(line, "/"), " ")
	args = strings.TrimSpace(args)
	command, ok := m.findCommand(name)
	if !ok {
		m.addNotice(NoticeWarning, fmt.Sprintf("Unknown command: /%s · /help lists the commands", name))
		return m.commit()
	}
	if command.Source != CommandBuiltin {
		m.appendBlock(&block{kind: blockUser, text: line, at: m.now()})
		return tea.Batch(m.call("/"+command.Name, func(ctx context.Context) error { return m.backend.RunCommand(ctx, line) }), m.commit())
	}
	switch command.Name {
	case "help":
		m.dialog = m.helpPanel()
	case "clear":
		return m.clearSession()
	case "compact":
		m.compacting, m.compactAt = true, m.now()
		return tea.Batch(m.call("compact", func(ctx context.Context) error { return m.backend.Compact(ctx, args) }), m.tick())
	case "resume":
		return m.openResume()
	case "rewind":
		return m.openRewind()
	case "model":
		if args != "" {
			model, effort, _ := strings.Cut(args, " ")
			return m.call("model", func(ctx context.Context) error { return m.backend.SetModel(ctx, model, strings.TrimSpace(effort)) })
		}
		return m.openModels()
	case "config":
		m.openConfig()
	case "theme":
		if args != "" {
			m.prefs.Theme = args
			m.applyTheme()
			return m.savePrefs()
		}
		m.openThemes()
	case "vim":
		m.prefs.Vim = !m.prefs.Vim
		m.editor.SetVim(m.prefs.Vim)
		if m.prefs.Vim {
			m.flash("Vim mode on · esc for NORMAL")
		} else {
			m.flash("Vim mode off")
		}
		return tea.Batch(m.savePrefs(), m.tick())
	case "todos":
		m.dialog = m.todosPanel()
	case "copy":
		return m.copyLastAnswer()
	case "cost":
		m.dialog = m.usagePanel()
	case "context":
		m.dialog = m.contextPanel()
	case "status":
		return m.openStatus()
	case "login":
		return m.openLogin()
	case "exit":
		return m.quit()
	}
	return nil
}

func (m *Model) clearSession() tea.Cmd {
	return func() tea.Msg {
		session, err := m.backend.Clear(m.ctx)
		return startedMsg{session: session, err: err, cleared: true}
	}
}

func (m *Model) helpPanel() *panel {
	t := m.theme
	lines := []string{t.Bold().Render("Keys")}
	keys := [][2]string{
		{"enter", "send · steer the running turn"},
		{"tab (while working)", "queue for after the turn"},
		{"shift+enter, \\ enter, ctrl+j", "new line"},
		{"esc", "interrupt · esc esc: clear, or rewind when empty"},
		{"ctrl+c", "interrupt · clear · twice to exit"},
		{"ctrl+d", "twice on an empty prompt to exit"},
		{"shift+tab", "cycle the local guard: ask, auto, plan"},
		{"ctrl+o", "transcript with full outputs and thinking"},
		{"ctrl+t", "toggle the todo list"},
		{"ctrl+r", "search prompt history"},
		{"ctrl+g", "edit the prompt in $EDITOR"},
		{"ctrl+v", "paste an image from the clipboard"},
		{"up / down", "history · up on empty: take back a queued message"},
		{"pgup / pgdn, wheel", "scroll · ctrl+end to jump to the bottom"},
		{"! / @ / ?", "shell mode · mention a file · shortcuts"},
	}
	for _, k := range keys {
		lines = append(lines, "  "+t.Fg(t.Palette.Text).Render(padRight(k[0], 30))+t.Dim().Render(k[1]))
	}
	lines = append(lines, "", t.Bold().Render("Commands"))
	for _, c := range m.allCommands() {
		name := "/" + c.Name
		if c.Args != "" {
			name += " " + c.Args
		}
		lines = append(lines, "  "+t.Fg(t.Palette.Text).Render(padRight(name, 30))+t.Dim().Render(c.Description+sourceTag(c.Source)))
	}
	return &panel{title: m.options.AgentName + " · help", lines: lines}
}

func sourceTag(s CommandSource) string {
	switch s {
	case CommandSkill:
		return " (skill)"
	case CommandServer:
		return " (server)"
	}
	return ""
}

func (m *Model) todosPanel() *panel {
	p := &panel{title: "Todos"}
	if len(m.todos) == 0 {
		p.lines = []string{m.theme.Dim().Render("The agent has no todo list in this session.")}
		return p
	}
	p.lines = drawTodoLines(m.renderContext(m.width, false), m.todos, 2, false)
	return p
}

func (m *Model) usagePanel() *panel {
	t := m.theme
	u := m.usage
	lines := []string{
		"  " + padRight("Input tokens", 18) + t.Fg(t.Palette.Text).Render(formatTokens(u.InputTokens)),
		"  " + padRight("Output tokens", 18) + t.Fg(t.Palette.Text).Render(formatTokens(u.OutputTokens)),
	}
	if u.CostUSD > 0 {
		lines = append(lines, "  "+padRight("Cost", 18)+t.Fg(t.Palette.Text).Render(fmt.Sprintf("$%.2f", u.CostUSD)))
	}
	return &panel{title: "Usage", lines: lines}
}

// contextPanel draws the context window as a bar of cells, the shape of Claude Code's /context grid
// reduced to the one number this client knows.
func (m *Model) contextPanel() *panel {
	t := m.theme
	u := m.usage
	if u.ContextWindow <= 0 {
		return &panel{title: "Context", lines: []string{t.Dim().Render("The server has not reported the context window yet.")}}
	}
	cells := 40
	used := min(u.ContextTokens*cells/u.ContextWindow, cells)
	bar := t.Fg(t.Palette.Accent).Render(strings.Repeat("█", used)) + t.Faint().Render(strings.Repeat("░", cells-used))
	pct := u.ContextTokens * 100 / u.ContextWindow
	return &panel{title: "Context", lines: []string{
		"  " + bar,
		"",
		"  " + t.Fg(t.Palette.Text).Render(fmt.Sprintf("%s of %s tokens used (%d%%)", formatTokens(u.ContextTokens), formatTokens(u.ContextWindow), pct)),
		"  " + t.Dim().Render("/compact summarises the history to free space"),
	}}
}

func (m *Model) openStatus() tea.Cmd {
	p := &panel{title: "Status", loading: true}
	p.loaded = func(m *Model, p *panel, data any) {
		status := data.(Status)
		t := m.theme
		width := 10
		for _, item := range status.Items {
			width = max(width, len(item.Label)+2)
		}
		for _, item := range status.Items {
			p.lines = append(p.lines, "  "+t.Dim().Render(padRight(item.Label, width))+t.Fg(t.Palette.Text).Render(item.Value))
		}
		p.lines = append(p.lines,
			"  "+t.Dim().Render(padRight("Guard", width))+t.Fg(t.Palette.Text).Render(guardName(m.guard)),
			"  "+t.Dim().Render(padRight("Theme", width))+t.Fg(t.Palette.Text).Render(m.theme.Name),
		)
	}
	m.dialog = p
	return func() tea.Msg {
		status, err := m.backend.Status(m.ctx)
		return dialogMsg{kind: "status", data: status, err: err}
	}
}

func (m *Model) openLogin() tea.Cmd {
	m.dialog = &loginDialog{loading: true}
	return func() tea.Msg {
		challenge, err := m.backend.Login(m.ctx)
		return dialogMsg{kind: "login", data: challenge, err: err}
	}
}

func (m *Model) openResume() tea.Cmd {
	p := &picker{title: "Resume a session", loading: true, filterable: true, footer: "type to filter · enter to resume · esc to cancel"}
	p.loaded = func(m *Model, p *picker, data any) {
		for _, s := range data.([]SessionSummary) {
			detail := fmt.Sprintf("%d %s", s.Messages, plural(s.Messages, "message", "messages"))
			if s.Kind != "" && s.Kind != "terminal" {
				detail += " · " + s.Kind
			}
			if s.Running {
				detail += " · running"
			}
			p.items = append(p.items, pickItem{label: s.Title, detail: detail, right: relativeTime(m.now(), s.Updated), value: s.ID})
		}
	}
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		m.dialog = nil
		id := item.value.(string)
		return func() tea.Msg {
			session, err := m.backend.Start(m.ctx, StartRequest{Resume: id})
			return startedMsg{session: session, err: err, cleared: true}
		}
	}
	m.dialog = p
	return func() tea.Msg {
		sessions, err := m.backend.Sessions(m.ctx, 50)
		return dialogMsg{kind: "sessions", data: sessions, err: err}
	}
}

// openRewind lists the session's prompts, newest first, and then asks what to restore.
func (m *Model) openRewind() tea.Cmd {
	p := &picker{title: "Rewind", subtitle: "Restore the code and/or conversation to the point before…", loading: true}
	p.loaded = func(m *Model, p *picker, data any) {
		points := data.([]Checkpoint)
		for i := len(points) - 1; i >= 0; i-- {
			cp := points[i]
			detail := ""
			if !cp.Code {
				detail = "no code snapshot"
			}
			p.items = append(p.items, pickItem{label: oneLine(cp.Prompt), detail: detail, right: relativeTime(m.now(), cp.At), value: cp})
		}
		if len(p.items) == 0 {
			p.err = "Nothing to rewind to yet."
		}
	}
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		m.dialog = m.rewindScope(item.value.(Checkpoint))
		return nil
	}
	m.dialog = p
	return func() tea.Msg {
		points, err := m.backend.Checkpoints(m.ctx)
		return dialogMsg{kind: "checkpoints", data: points, err: err}
	}
}

// rewindScope is the second step of a rewind: Claude Code's five choices. The chosen prompt goes back
// into the composer, so the person can edit and resend it.
func (m *Model) rewindScope(cp Checkpoint) *picker {
	p := &picker{title: "Rewind to: " + oneLine(cp.Prompt), footer: "enter to choose · esc to cancel"}
	if cp.Code {
		p.items = append(p.items, pickItem{label: "Restore code and conversation", value: RewindBoth})
	}
	p.items = append(p.items, pickItem{label: "Restore conversation", value: RewindConversation})
	if cp.Code {
		p.items = append(p.items, pickItem{label: "Restore code", value: RewindCode})
	}
	p.items = append(p.items,
		pickItem{label: "Summarize from here", value: RewindSummarize},
		pickItem{label: "Never mind", value: -1},
	)
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		m.dialog = nil
		scope, ok := item.value.(RewindScope)
		if !ok {
			return nil
		}
		if scope != RewindCode {
			m.editor.SetValue(cp.Prompt)
		}
		id := cp.ID
		return m.call("rewind", func(ctx context.Context) error { return m.backend.Rewind(ctx, id, scope) })
	}
	return p
}

func (m *Model) openModels() tea.Cmd {
	p := &picker{title: "Select model", subtitle: "← → to change the effort", loading: true, footer: "enter to choose · ← → effort · esc to cancel"}
	type choice struct {
		option ModelOption
		effort int
	}
	p.loaded = func(m *Model, p *picker, data any) {
		for _, o := range data.([]ModelOption) {
			c := &choice{option: o}
			for i, e := range o.Efforts {
				if o.ID == m.session.Model && e == m.session.Effort {
					c.effort = i
				}
			}
			item := pickItem{label: o.Label, detail: o.ID, value: c}
			if len(o.Efforts) > 0 {
				item.right = "effort: " + o.Efforts[c.effort]
			}
			if o.ID == m.session.Model {
				item.label += " " + m.theme.Glyphs.Check
				p.cursor = len(p.items)
			}
			p.items = append(p.items, item)
		}
	}
	p.side = func(m *Model, item *pickItem, delta int) {
		c := item.value.(*choice)
		if n := len(c.option.Efforts); n > 0 {
			c.effort = (c.effort + delta + n) % n
			item.right = "effort: " + c.option.Efforts[c.effort]
		}
	}
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		m.dialog = nil
		c := item.value.(*choice)
		effort := ""
		if len(c.option.Efforts) > 0 {
			effort = c.option.Efforts[c.effort]
		}
		id := c.option.ID
		return m.call("model", func(ctx context.Context) error { return m.backend.SetModel(ctx, id, effort) })
	}
	m.dialog = p
	return func() tea.Msg {
		models, err := m.backend.Models(m.ctx)
		return dialogMsg{kind: "models", data: models, err: err}
	}
}

// openConfig lists the local settings; enter changes the one under the pointer and saves at once.
func (m *Model) openConfig() {
	p := &picker{title: "Settings", footer: "enter or ← → to change · esc to close"}
	refresh := func(m *Model, p *picker) {
		onOff := func(b bool) string {
			if b {
				return "on"
			}
			return "off"
		}
		themeName := m.prefs.Theme
		if themeName == "" {
			themeName = "auto (" + m.theme.Name + ")"
		}
		editor := "normal"
		if m.prefs.Vim {
			editor = "vim"
		}
		p.items = []pickItem{
			{label: "Theme", right: themeName, value: "theme"},
			{label: "Editor mode", right: editor, value: "vim"},
			{label: "Fullscreen", right: onOff(!m.prefs.Inline), value: "inline"},
			{label: "Show thinking", right: onOff(m.prefs.ShowThinking), value: "thinking"},
			{label: "Verbose tool output", right: onOff(m.prefs.Verbose), value: "verbose"},
			{label: "Notifications", right: onOff(m.prefs.NotifyOn()), value: "notify"},
		}
	}
	change := func(m *Model, item *pickItem, delta int) {
		switch item.value {
		case "theme":
			names := append([]string{""}, theme.Names...)
			i := 0
			for j, n := range names {
				if n == m.prefs.Theme {
					i = j
				}
			}
			m.prefs.Theme = names[(i+delta+len(names))%len(names)]
			m.applyTheme()
		case "vim":
			m.prefs.Vim = !m.prefs.Vim
			m.editor.SetVim(m.prefs.Vim)
		case "inline":
			m.prefs.Inline = !m.prefs.Inline
			m.committed = len(m.blocks)
		case "thinking":
			m.prefs.ShowThinking = !m.prefs.ShowThinking
		case "verbose":
			m.prefs.Verbose = !m.prefs.Verbose
		case "notify":
			on := !m.prefs.NotifyOn()
			m.prefs.Notify = &on
		}
		refresh(m, p)
	}
	refresh(m, p)
	p.side = func(m *Model, item *pickItem, delta int) { change(m, item, delta) }
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		cursor := p.cursor
		change(m, &item, 1)
		p.cursor = cursor
		return m.savePrefs()
	}
	m.dialog = p
}

func (m *Model) openThemes() {
	p := &picker{title: "Theme", footer: "enter to choose · esc to cancel"}
	for _, name := range append([]string{"auto"}, theme.Names...) {
		item := pickItem{label: name, value: name}
		if name == m.prefs.Theme || (name == "auto" && m.prefs.Theme == "") {
			item.label += " " + m.theme.Glyphs.Check
		}
		p.items = append(p.items, item)
	}
	p.choose = func(m *Model, item pickItem) tea.Cmd {
		m.dialog = nil
		name := item.value.(string)
		if name == "auto" {
			name = ""
		}
		m.prefs.Theme = name
		m.applyTheme()
		return m.savePrefs()
	}
	m.dialog = p
}

// copyLastAnswer copies the last answer with OSC 52, which reaches the local clipboard even over SSH.
func (m *Model) copyLastAnswer() tea.Cmd {
	for i := len(m.blocks) - 1; i >= 0; i-- {
		if b := m.blocks[i]; b.kind == blockAssistant {
			m.flash("Copied the last answer")
			return tea.Batch(tea.SetClipboard(b.stream.Text()), m.tick())
		}
	}
	m.flash("No answer to copy yet")
	return m.tick()
}
