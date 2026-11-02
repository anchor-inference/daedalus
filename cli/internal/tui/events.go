package tui

import (
	"os"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"

	"github.com/ascorblack/daedalus/cli/internal/tui/markdown"
)

// applyEvent folds one backend event into the transcript. replay is true for history being drawn
// from a snapshot: it is the same events, but nothing in it should ring the bell or start a timer.
func (m *Model) applyEvent(event Event, replay bool) tea.Cmd {
	now := m.now()
	switch e := event.(type) {
	case ConnectionChanged:
		return m.connectionChanged(e)
	case SessionUpdated:
		m.session = e.Info
	case HistoryReset:
		m.resetTranscript()
		for _, h := range e.History {
			m.applyEvent(h, true)
		}
		if m.inline() {
			// The terminal's scrollback cannot be rewritten; a reset is drawn as a fresh start below.
			m.addNotice(NoticeInfo, "History replaced; the transcript below starts again.")
		}
	case UserMessage:
		m.userMessage(e)
	case TurnStarted:
		if !replay {
			at := e.At
			if at.IsZero() {
				at = now
			}
			m.beginTurn(at)
		}
	case ThinkingDelta:
		b := m.streamBlock(e.MessageID+"#thinking", blockThinking)
		b.stream.Append(e.Text)
		b.touch()
		m.turnChars += len(e.Text)
	case TextDelta:
		if t := m.byID[e.MessageID+"#thinking"]; t != nil && !t.done {
			m.closeStream(t)
		}
		m.closeThinking()
		b := m.streamBlock(e.MessageID, blockAssistant)
		b.stream.Append(e.Text)
		b.touch()
		m.turnChars += len(e.Text)
	case MessageDone:
		for _, id := range []string{e.MessageID + "#thinking", e.MessageID} {
			if b := m.byID[id]; b != nil {
				m.closeStream(b)
			}
		}
	case ToolStarted:
		m.closeThinking()
		at := e.At
		if at.IsZero() {
			at = now
		}
		b := &block{kind: blockTool, id: e.ID, at: at, tool: &toolCall{name: e.Name, input: e.Input, parent: e.Parent, started: at}}
		m.byID[e.ID] = b
		if parent := m.byID[e.Parent]; e.Parent != "" && parent != nil && parent.tool != nil {
			parent.tool.children = append(parent.tool.children, b)
			parent.touch()
		} else {
			m.appendBlock(b)
		}
	case ToolProgress:
		if b := m.byID[e.ID]; b != nil && b.tool != nil {
			b.tool.progress += e.Text
			if len(b.tool.progress) > 64*1024 {
				b.tool.progress = b.tool.progress[len(b.tool.progress)-32*1024:]
			}
			b.touch()
		}
	case ToolFinished:
		if b := m.byID[e.ID]; b != nil && b.tool != nil {
			at := e.At
			if at.IsZero() {
				at = now
			}
			b.tool.finished, b.tool.output, b.tool.isError, b.tool.ended = true, e.Output, e.IsError, at
			b.tool.progress = ""
			b.touch()
			if parent := m.byID[b.tool.parent]; parent != nil {
				parent.touch()
			}
		}
	case FileChanged:
		if b := m.byID[e.ToolID]; b != nil && b.tool != nil {
			change := e.Change
			b.tool.change = &change
			b.touch()
		}
	case PermissionRequested:
		if replay {
			return nil
		}
		m.openPermission(e.Request)
		return m.notify("Permission needed: " + e.Request.Tool)
	case PermissionResolved:
		if d, ok := m.dialog.(*permissionDialog); ok && d.request.ID == e.ID {
			m.dialog = nil
			if e.Via != "" && e.Via != "cli" {
				m.flash("Answered in the " + e.Via)
			}
		}
	case QuestionAsked:
		if replay {
			return nil
		}
		m.openQuestion(e.Question)
		return m.notify("A question is waiting")
	case QuestionResolved:
		if d, ok := m.dialog.(*questionDialog); ok && d.question.ID == e.ID {
			m.dialog = nil
		}
	case TodosChanged:
		m.todos = e.Items
		if len(e.Items) > 0 && !replay {
			m.showTodo = true
		}
		// Consecutive updates replace the last todo block instead of stacking: a list that ticks off
		// four items is one block that changes, not four copies.
		if last := m.lastBlock(); last != nil && last.kind == blockTodos {
			last.todos = e.Items
			last.touch()
		} else if len(e.Items) > 0 {
			m.appendBlock(&block{kind: blockTodos, todos: e.Items, at: now})
		}
	case QueueChanged:
		m.queue = e.Items
	case UsageChanged:
		if m.running {
			m.turnTokens += max(e.Usage.OutputTokens-m.usage.OutputTokens, 0)
		}
		m.usage = e.Usage
	case TurnEnded:
		if replay {
			return nil
		}
		return m.endTurn(e)
	case Notice:
		m.addNotice(e.Level, e.Text)
	case CompactionChanged:
		m.compacting = e.Running
		if e.Running {
			m.compactAt = now
		} else {
			m.appendBlock(&block{kind: blockCompact, text: e.Summary, at: now})
		}
	case SubagentChanged:
		if b := m.byID[e.ID]; b != nil {
			b.touch()
		}
	case ModelChanged:
		m.session.Model, m.session.Effort = e.Model, e.Effort
		if !replay {
			m.flash("Model: " + e.Model + effortSuffix(e.Effort))
		}
	case DeviceWaiting:
		if e.Waiting {
			m.waitingDevice = e.Device
			if m.waitingDevice == "" {
				m.waitingDevice = "the terminal"
			}
		} else {
			if m.waitingDevice != "" && m.running {
				m.flash(m.waitingDevice + " is back · turn resumed")
			}
			m.waitingDevice = ""
		}
	case LoginChanged:
		if d, ok := m.dialog.(*loginDialog); ok {
			d.state, d.device, d.err = e.State, e.Device, e.Err
		}
		if e.State == LoginApproved {
			m.addNotice(NoticeInfo, "Paired as "+e.Device+".")
			return m.startSession(m.options.Start)
		}
	}
	return nil
}

func effortSuffix(effort string) string {
	if effort == "" {
		return ""
	}
	return " (" + effort + ")"
}

func (m *Model) lastBlock() *block {
	if len(m.blocks) == 0 {
		return nil
	}
	return m.blocks[len(m.blocks)-1]
}

func (m *Model) userMessage(e UserMessage) {
	if b := m.byID[e.ID]; e.ID != "" && b != nil {
		// The echo of a prompt drawn when it was sent.
		return
	}
	for i, q := range m.queue {
		if q.ID == e.ID || (e.ID == "" && q.Text == e.Text) {
			m.queue = append(m.queue[:i:i], m.queue[i+1:]...)
			break
		}
	}
	at := e.At
	if at.IsZero() {
		at = m.now()
	}
	b := &block{kind: blockUser, id: e.ID, text: e.Text, origin: e.Origin, images: e.Images, at: at}
	if e.ID != "" {
		m.byID[e.ID] = b
	}
	m.appendBlock(b)
}

func (m *Model) streamBlock(id string, kind blockKind) *block {
	if b := m.byID[id]; b != nil && !b.done {
		return b
	}
	b := &block{kind: kind, id: id, at: m.now(), stream: markdown.NewStream(m.md)}
	m.byID[id] = b
	m.appendBlock(b)
	return b
}

func (m *Model) closeStream(b *block) {
	if b.done {
		return
	}
	b.done = true
	if b.kind == blockThinking {
		b.ended = m.now()
	}
	b.touch()
}

// closeThinking ends any reasoning still open: once the answer or a tool call starts, the model has
// stopped thinking even if the backend sent no explicit end for it.
func (m *Model) closeThinking() {
	for i := len(m.blocks) - 1; i >= 0 && i >= len(m.blocks)-6; i-- {
		if b := m.blocks[i]; b.kind == blockThinking && !b.done {
			m.closeStream(b)
		}
	}
}

func (m *Model) beginTurn(at time.Time) {
	if m.running {
		return
	}
	m.running = true
	m.turnStart = at
	m.turnChars, m.turnTokens = 0, 0
	m.verb = verbFor(at.UnixNano() / int64(time.Millisecond))
}

func (m *Model) endTurn(e TurnEnded) tea.Cmd {
	m.running = false
	for _, b := range m.blocks {
		if (b.kind == blockAssistant || b.kind == blockThinking) && !b.done {
			m.closeStream(b)
		}
		if b.kind == blockTool && !b.tool.finished && e.Outcome != TurnCompleted {
			b.tool.finished, b.tool.isError, b.tool.ended = true, true, m.now()
			if b.tool.output == "" {
				b.tool.output = "Interrupted"
			}
			b.touch()
		}
	}
	if d, ok := m.dialog.(*permissionDialog); ok && e.Outcome != TurnCompleted {
		_ = d
		m.dialog = nil
	}
	switch e.Outcome {
	case TurnInterrupted:
		m.appendBlock(&block{kind: blockInterrupted, at: m.now()})
	case TurnFailed:
		text := e.Err
		if text == "" {
			text = "the turn failed"
		}
		m.addNotice(NoticeError, "Error: "+text)
	}
	return m.notify("Turn finished")
}

func (m *Model) connectionChanged(e ConnectionChanged) tea.Cmd {
	was := m.conn
	m.conn, m.connAttempt, m.connRetry, m.connErr = e.State, e.Attempt, e.RetryIn, e.Err
	if was != e.State {
		m.connSince = m.now()
	}
	switch {
	case e.State == Online && was == Reconnecting:
		if m.running {
			m.flash("Reconnected · turn resumed")
		} else {
			m.flash("Reconnected")
		}
	case e.State == Unauthorized:
		m.addNotice(NoticeError, "The server refused this device. Run /login to pair it again.")
	case e.State == Offline && was != Offline:
		text := "Disconnected from the server."
		if e.Err != "" {
			text += " " + e.Err
		}
		m.addNotice(NoticeWarning, text)
	}
	return nil
}

// notify rings the terminal when the window is not focused, so a person who switched away learns
// that the turn ended or that a decision waits. OSC 9 shows a desktop notification in the terminals
// that support it; the bell is the portable fallback, and inside tmux it is the only thing that
// reliably gets through.
func (m *Model) notify(text string) tea.Cmd {
	if m.focus || !m.prefs.NotifyOn() {
		return nil
	}
	seq := "\x1b]9;" + m.options.AgentName + ": " + strings.ReplaceAll(text, "\x07", "") + "\x07"
	if os.Getenv("TMUX") != "" || os.Getenv("TERM_PROGRAM") == "Apple_Terminal" {
		seq = "\a"
	}
	return tea.Raw(seq)
}

// commit prints finished blocks into the terminal's scrollback in inline mode. Blocks are committed
// strictly in order, so a finished tool call below a still-streaming answer waits for the answer:
// what reaches the scrollback can never be reordered or redrawn.
func (m *Model) commit() tea.Cmd {
	if !m.inline() || m.width == 0 || !m.viewed {
		return nil
	}
	var cmds []tea.Cmd
	ctx := m.renderContext(m.width, false)
	for m.committed < len(m.blocks) {
		b := m.blocks[m.committed]
		if b.live() {
			break
		}
		lines := b.render(ctx)
		text := strings.Join(lines, "\n")
		if m.committed > 0 {
			text = "\n" + text
		}
		cmds = append(cmds, tea.Println(text))
		m.committed++
	}
	return tea.Sequence(cmds...)
}
