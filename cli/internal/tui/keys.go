package tui

import (
	"context"
	"os"
	"os/exec"
	"strings"

	tea "charm.land/bubbletea/v2"

	"github.com/ascorblack/daedalus/cli/internal/tui/textedit"
)

// key routes one key press. The order is the order of precedence: the transcript view owns the
// keyboard while it is open, Ctrl+C always means something, a dialog owns the rest of the keys while
// it waits, and only then do the prompt's own keys apply.
func (m *Model) key(msg tea.KeyPressMsg) tea.Cmd {
	k := msg.String()
	if m.pager != nil {
		return m.pager.key(m, msg)
	}
	if k == "ctrl+c" {
		return m.ctrlC()
	}
	if m.dialog != nil {
		return m.dialog.key(m, msg)
	}
	if m.search != nil {
		return m.searchKey(msg)
	}
	if m.comp != nil {
		if cmd, handled := m.completionKey(msg); handled {
			return cmd
		}
	}
	if m.help && k != "?" {
		m.help = false
	}

	switch k {
	case "ctrl+o":
		m.openPager()
		return nil
	case "ctrl+t":
		m.showTodo = !m.showTodo
		return nil
	case "shift+tab":
		return m.cycleGuard()
	case "ctrl+l":
		return tea.ClearScreen
	case "pgup":
		m.scrollBy(max(m.transcriptHeight()-2, 1))
		return nil
	case "pgdown":
		m.scrollBy(-max(m.transcriptHeight()-2, 1))
		return nil
	case "ctrl+home":
		m.scrollBy(1 << 30)
		return nil
	case "ctrl+end":
		m.scroll, m.unseen = 0, 0
		return nil
	case "ctrl+r":
		m.openSearch()
		return nil
	case "ctrl+g":
		return m.externalEditor()
	case "ctrl+v":
		return m.pasteImage()
	case "esc":
		return m.esc()
	case "ctrl+d":
		if m.editor.Empty() {
			if m.pressedTwice("ctrl+d", "Press Ctrl-D again to exit") {
				return m.quit()
			}
			return m.tick()
		}
	case "tab":
		// While the agent works, Tab holds the prompt as a follow-up for after the turn instead of
		// steering it in now, as Codex does; idle, there is nothing to queue behind.
		if m.running && !m.editor.Empty() {
			return m.submit(SendQueue)
		}
		return nil
	case "?":
		if m.editor.Empty() && !m.shell {
			m.help = !m.help
			return nil
		}
	case "!":
		if m.editor.Empty() && !m.shell && m.editor.VimMode() != "NORMAL" {
			m.shell = true
			return nil
		}
	case "backspace":
		if m.editor.Empty() && m.shell {
			m.shell = false
			return nil
		}
	}

	switch m.editor.HandleKey(msg) {
	case textedit.Submit:
		return m.submit(SendDefault)
	case textedit.HistoryPrev:
		return m.historyPrev()
	case textedit.HistoryNext:
		m.historyNext()
		return nil
	case textedit.Handled:
		m.pendingKey = ""
		m.refreshCompletion()
		if m.scroll > 0 && len(msg.Text) > 0 {
			// Typing means the person is back at the prompt; the transcript follows them down.
			m.scroll, m.unseen = 0, 0
		}
		return m.takePending()
	}
	return nil
}

// ctrlC interrupts a running turn, cancels a dialog, clears a draft, and exits on a second press.
func (m *Model) ctrlC() tea.Cmd {
	switch {
	case m.dialog != nil:
		return m.dialog.cancel(m)
	case m.search != nil:
		m.search = nil
		return nil
	case m.running:
		return m.interrupt()
	case !m.editor.Empty():
		m.draft = m.editor.Value()
		m.editor.Reset()
		m.shell = false
		m.comp = nil
		return nil
	}
	if m.pressedTwice("ctrl+c", "Press Ctrl-C again to exit") {
		return m.quit()
	}
	return m.tick()
}

// esc interrupts a turn, closes what is open, and as a double press clears the prompt or, on an
// empty prompt, opens the rewind list.
func (m *Model) esc() tea.Cmd {
	if m.help {
		m.help = false
		return nil
	}
	if m.running {
		return m.interrupt()
	}
	if m.shell && m.editor.Empty() {
		m.shell = false
		return nil
	}
	if !m.editor.Empty() {
		if m.pressedTwice("esc", "Esc again to clear") {
			m.draft = m.editor.Value()
			m.editor.Reset()
			return nil
		}
		return m.tick()
	}
	if m.pressedTwice("esc", "Esc again to rewind") {
		return m.openRewind()
	}
	return m.tick()
}

func (m *Model) interrupt() tea.Cmd {
	return m.call("interrupt", func(ctx context.Context) error { return m.backend.Interrupt(ctx) })
}

// cycleGuard moves the local guard ask → auto → plan, the Shift+Tab of Claude Code. The guard is
// enforced by the executor on this machine, so the change is sent to it at once and remembered for
// the next session.
func (m *Model) cycleGuard() tea.Cmd {
	m.guard = (m.guard + 1) % 3
	m.prefs.Guard = guardName(m.guard)
	guard := m.guard
	return tea.Batch(m.savePrefs(), m.call("guard", func(ctx context.Context) error {
		return m.backend.SetGuard(ctx, guard)
	}))
}

func guardName(g Guard) string {
	switch g {
	case GuardAsk:
		return "ask"
	case GuardPlan:
		return "plan"
	}
	return "auto"
}

// historyPrev walks back through the prompts typed in this folder. On an empty prompt, a queued
// message comes back first: Up is how Claude Code takes back what was queued.
func (m *Model) historyPrev() tea.Cmd {
	if m.editor.Empty() && m.draft != "" {
		m.editor.SetValue(m.draft)
		m.draft = ""
		return nil
	}
	if m.editor.Empty() && len(m.queue) > 0 {
		last := m.queue[len(m.queue)-1]
		return func() tea.Msg {
			text, err := m.backend.Withdraw(m.ctx, last.ID)
			return withdrawMsg{text: text, err: err}
		}
	}
	if m.histPos == 0 || len(m.history) == 0 {
		return nil
	}
	if m.histPos == len(m.history) {
		m.histDraft = m.editor.Value()
	}
	m.histPos--
	m.editor.SetValue(m.history[m.histPos])
	return nil
}

func (m *Model) historyNext() {
	if m.histPos >= len(m.history) {
		return
	}
	m.histPos++
	if m.histPos == len(m.history) {
		m.editor.SetValue(m.histDraft)
		return
	}
	m.editor.SetValue(m.history[m.histPos])
}

// submit sends what is in the prompt: a slash command, a shell command, or a message.
func (m *Model) submit(mode SendMode) tea.Cmd {
	raw := m.editor.Value()
	text := strings.TrimSpace(m.editor.Expanded())
	if text == "" && len(m.images) == 0 {
		return nil
	}
	m.comp = nil
	m.help = false
	m.scroll, m.unseen = 0, 0
	m.remember(raw)

	if m.shell {
		m.shell = false
		m.editor.Reset()
		return m.runShell(text)
	}
	if strings.HasPrefix(text, "/") && len(m.images) == 0 {
		m.editor.Reset()
		return m.runSlash(text)
	}
	images := m.referencedImages()
	m.editor.Reset()
	m.images = nil

	body := text
	if len(m.shellContext) > 0 {
		body = strings.Join(m.shellContext, "\n") + "\n\n" + text
		m.shellContext = nil
	}
	id := m.nextID()
	prompt := Prompt{Text: body, Images: images, Mode: mode, ClientID: id}
	if m.running {
		m.queue = append(m.queue, QueuedMessage{ID: id, Text: text})
	} else {
		b := &block{kind: blockUser, id: id, text: text, images: len(images), at: m.now()}
		m.byID[id] = b
		m.appendBlock(b)
		m.beginTurn(m.now())
	}
	return tea.Batch(m.call("send", func(ctx context.Context) error { return m.backend.Send(ctx, prompt) }), m.commit(), m.tick())
}

// referencedImages is the pasted images whose placeholders are still in the prompt; a placeholder
// the person deleted takes its picture with it.
func (m *Model) referencedImages() []Image {
	var out []Image
	for _, n := range m.editor.Images() {
		if n >= 1 && n <= len(m.images) {
			out = append(out, m.images[n-1])
		}
	}
	return out
}

func (m *Model) remember(raw string) {
	if strings.TrimSpace(raw) == "" {
		return
	}
	entry := raw
	if m.shell {
		entry = "!" + raw
	}
	if len(m.history) == 0 || m.history[len(m.history)-1] != entry {
		m.history = append(m.history, entry)
		store, cwd := m.store, m.options.Cwd
		go func() { _ = store.AppendHistory(cwd, entry) }()
	}
	m.histPos = len(m.history)
	m.histDraft = ""
}

func (m *Model) runShell(command string) tea.Cmd {
	b := &block{kind: blockShell, text: command, at: m.now(), tool: &toolCall{name: "shell", started: m.now()}}
	id := m.nextID()
	b.id = id
	m.byID[id] = b
	m.appendBlock(b)
	return func() tea.Msg {
		result, err := m.backend.Shell(m.ctx, command)
		return shellMsg{command: id, result: result, err: err}
	}
}

// shellDone shows a `!` command's output and keeps it for the agent: Claude Code puts the command
// and its output into the context, so the next prompt can say "fix that" about it.
func (m *Model) shellDone(msg shellMsg) tea.Cmd {
	b := m.byID[msg.command]
	if b == nil {
		return nil
	}
	b.tool.finished, b.tool.ended = true, m.now()
	if msg.err != nil {
		b.tool.output, b.tool.isError = msg.err.Error(), true
	} else {
		b.tool.output = msg.result.Output
		b.tool.isError = msg.result.ExitCode != 0
		if msg.result.ExitCode != 0 {
			b.tool.output += "\n(exit " + itoa(msg.result.ExitCode) + ")"
		}
		output := msg.result.Output
		if len(output) > 16*1024 {
			output = output[:8*1024] + "\n…\n" + output[len(output)-8*1024:]
		}
		m.shellContext = append(m.shellContext, "<bash-input>"+b.text+"</bash-input>\n<bash-stdout>"+output+"</bash-stdout>")
	}
	b.touch()
	return m.commit()
}

// externalEditor opens the prompt in $EDITOR and takes back what was saved.
func (m *Model) externalEditor() tea.Cmd {
	f, err := os.CreateTemp("", "daedalus-prompt-*.md")
	if err != nil {
		m.flash("editor: " + err.Error())
		return m.tick()
	}
	path := f.Name()
	_, _ = f.WriteString(m.editor.Expanded())
	_ = f.Close()
	parts := strings.Fields(m.editorCommand())
	cmd := exec.Command(parts[0], append(parts[1:], path)...)
	return tea.ExecProcess(cmd, func(err error) tea.Msg {
		defer os.Remove(path)
		if err != nil {
			return editorMsg{err: err}
		}
		data, readErr := os.ReadFile(path)
		return editorMsg{text: string(data), err: readErr}
	})
}

// paste puts pasted text into the prompt, or into the dialog that is typing.
func (m *Model) paste(content string) tea.Cmd {
	if m.dialog != nil {
		if p, ok := m.dialog.(paster); ok {
			p.paste(content)
		}
		return nil
	}
	if m.search != nil {
		m.search.query += content
		m.search.find(m.history)
		return nil
	}
	if path := strings.TrimSpace(content); isImagePath(path) && !strings.Contains(path, "\n") {
		return m.loadImageFile(path)
	}
	m.editor.Paste(content)
	m.refreshCompletion()
	return m.takePending()
}

func (m *Model) scrollBy(delta int) {
	m.scroll += delta
	if m.scroll < 0 {
		m.scroll = 0
	}
	if limit := m.maxScroll(); m.scroll > limit {
		m.scroll = limit
	}
	if m.scroll == 0 {
		m.unseen = 0
	}
}
