// Command gateprog is the renderer gate's load: a Bubble Tea v2 program shaped like the real client
// (a streaming answer, tool calls, an animated spinner, a bordered input pinned at the bottom and a
// status line under it) that replays a synthetic session, so the renderer can be measured for
// flicker, resize and scrollback before the client itself exists.
//
// Fullscreen by default, as the client is; -inline commits finished blocks above a live region
// instead, for comparison with Claude Code's classic renderer.
package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

var (
	accent    = lipgloss.Color("#D77757")
	secondary = lipgloss.Color("#999999")
	border    = lipgloss.Color("#888888")
	auto      = lipgloss.Color("#AF87FF")
	success   = lipgloss.Color("#4EBA65")
)

var spinnerFrames = []string{"·", "✢", "✳", "✶", "✻", "✽"}

type eventKind int

const (
	evUser eventKind = iota
	evDelta
	evMessageDone
	evToolStart
	evToolDone
)

type event struct {
	kind eventKind
	text string
}

// script builds a deterministic session of n events: a prompt, then streamed paragraphs with tool
// calls between them, the mix a real turn has.
func script(n int) []event {
	words := strings.Fields("The renderer commits each finished block once and keeps only the open block live, " +
		"so the terminal repaints a few rows per frame instead of the whole screen. **Streaming** markdown " +
		"is split at the last stable boundary; `code spans` and lists stay intact while the tail grows. " +
		"Resizing re-wraps every block at the new width and the input box stays pinned to the bottom.")
	tools := []string{"Read(cli/internal/tui/model.go)", "Exec(go test ./...)", "Search(\"spinner\")", "Edit(cli/internal/tui/theme/theme.go)"}
	events := []event{{kind: evUser, text: "Measure the renderer: stream a long answer with tool calls in between."}}
	w, t := 0, 0
	for len(events) < n-1 {
		for i := 0; i < 12 && len(events) < n-1; i++ {
			events = append(events, event{kind: evDelta, text: words[w%len(words)] + " "})
			w++
		}
		if len(events) >= n-3 {
			break
		}
		events = append(events, event{kind: evMessageDone})
		events = append(events, event{kind: evToolStart, text: tools[t%len(tools)]})
		events = append(events, event{kind: evToolDone, text: fmt.Sprintf("%d lines of output", 10+t*7)})
		t++
	}
	return append(events, event{kind: evMessageDone})
}

type block struct {
	kind   string // user, text, tool
	text   string
	result string
	done   bool
}

type model struct {
	inline    bool
	events    []event
	next      int
	delay     time.Duration
	quitAfter time.Duration
	width     int
	height    int
	blocks    []block
	committed int
	frame     int
	scroll    int
	input     string
	started   time.Time
	tokens    int
	warmup    time.Duration
}

type eventMsg struct{}
type spinMsg struct{}
type quitMsg struct{}

func (m model) Init() tea.Cmd {
	first := tea.Tick(m.warmup+m.delay, func(time.Time) tea.Msg { return eventMsg{} })
	return tea.Batch(first, tea.Tick(100*time.Millisecond, func(time.Time) tea.Msg { return spinMsg{} }))
}

func (m model) tickEvent() tea.Cmd {
	return tea.Tick(m.delay, func(time.Time) tea.Msg { return eventMsg{} })
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil
	case spinMsg:
		m.frame++
		return m, tea.Tick(100*time.Millisecond, func(time.Time) tea.Msg { return spinMsg{} })
	case quitMsg:
		return m, tea.Quit
	case eventMsg:
		if m.next >= len(m.events) {
			if m.quitAfter > 0 {
				return m, tea.Tick(m.quitAfter, func(time.Time) tea.Msg { return quitMsg{} })
			}
			return m, nil
		}
		m.apply(m.events[m.next])
		m.next++
		cmds := []tea.Cmd{m.tickEvent()}
		if m.inline {
			cmds = append(cmds, m.commit())
		}
		return m, tea.Batch(cmds...)
	case tea.KeyPressMsg:
		switch msg.String() {
		case "ctrl+c":
			return m, tea.Quit
		case "pgup":
			m.scroll += max(1, m.transcriptHeight()-2)
		case "pgdown":
			m.scroll = max(0, m.scroll-max(1, m.transcriptHeight()-2))
		case "enter":
			if m.input != "" {
				m.blocks = append(m.blocks, block{kind: "user", text: m.input, done: true})
				m.input = ""
			}
		case "backspace":
			if r := []rune(m.input); len(r) > 0 {
				m.input = string(r[:len(r)-1])
			}
		default:
			m.input += msg.Text
		}
	case tea.MouseWheelMsg:
		if msg.Button == tea.MouseWheelUp {
			m.scroll += 3
		} else {
			m.scroll = max(0, m.scroll-3)
		}
	}
	return m, nil
}

func (m *model) apply(e event) {
	last := len(m.blocks) - 1
	switch e.kind {
	case evUser:
		m.blocks = append(m.blocks, block{kind: "user", text: e.text, done: true})
	case evDelta:
		if last < 0 || m.blocks[last].kind != "text" || m.blocks[last].done {
			m.blocks = append(m.blocks, block{kind: "text"})
			last = len(m.blocks) - 1
		}
		m.blocks[last].text += e.text
		m.tokens += 7
	case evMessageDone:
		if last >= 0 && m.blocks[last].kind == "text" {
			m.blocks[last].done = true
		}
	case evToolStart:
		m.blocks = append(m.blocks, block{kind: "tool", text: e.text})
	case evToolDone:
		if last >= 0 && m.blocks[last].kind == "tool" {
			m.blocks[last].result = e.text
			m.blocks[last].done = true
		}
	}
}

// commit prints the finished blocks above the live region, in order and once each.
func (m *model) commit() tea.Cmd {
	var lines []string
	for m.committed < len(m.blocks) && m.blocks[m.committed].done {
		lines = append(lines, m.renderBlock(m.blocks[m.committed])...)
		lines = append(lines, "")
		m.committed++
	}
	if len(lines) == 0 {
		return nil
	}
	return tea.Println(strings.Join(lines, "\n"))
}

func (m model) renderBlock(b block) []string {
	width := max(10, m.width-2)
	dim := lipgloss.NewStyle().Foreground(secondary)
	switch b.kind {
	case "user":
		return prefixed(dim.Render("> "), "  ", lipgloss.Wrap(dim.Render(b.text), width, ""))
	case "tool":
		bullet := lipgloss.NewStyle().Foreground(success).Render("●")
		if !b.done {
			bullet = dim.Render("●")
		}
		lines := []string{bullet + " " + lipgloss.NewStyle().Bold(true).Render(b.text)}
		result := "Running…"
		if b.done {
			result = b.result + " " + dim.Render("… +12 lines (ctrl+o to expand)")
		}
		return append(lines, dim.Render("  ⎿  ")+result)
	default:
		return prefixed("● ", "  ", lipgloss.Wrap(b.text, width, ""))
	}
}

func prefixed(first, rest, text string) []string {
	lines := strings.Split(text, "\n")
	for i := range lines {
		if i == 0 {
			lines[i] = first + lines[i]
		} else {
			lines[i] = rest + lines[i]
		}
	}
	return lines
}

// chrome is the live bottom of the screen: spinner, input box, status line.
func (m model) chrome() []string {
	var out []string
	if m.next < len(m.events) {
		elapsed := int(time.Since(m.started).Seconds())
		glyph := spinnerFrames[m.frame%len(spinnerFrames)]
		spin := lipgloss.NewStyle().Foreground(accent).Render(glyph + " Pondering…")
		out = append(out, spin+" "+lipgloss.NewStyle().Foreground(secondary).Render(
			fmt.Sprintf("(esc to interrupt · %ds · ↑ %.1fk tokens)", elapsed, float64(m.tokens)/1000)))
	} else {
		out = append(out, "")
	}
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(border).Width(max(4, m.width))
	out = append(out, strings.Split(box.Render("> "+m.input), "\n")...)
	out = append(out, lipgloss.NewStyle().Foreground(auto).Render("  ⏵⏵ auto-accept on")+
		lipgloss.NewStyle().Foreground(secondary).Render(" · gate-model · 42% context"))
	return out
}

func (m model) transcriptHeight() int { return max(1, m.height-len(m.chrome())) }

func (m model) View() tea.View {
	if m.width == 0 {
		return tea.NewView("")
	}
	chrome := m.chrome()
	var body []string
	start := 0
	if m.inline {
		start = m.committed
	}
	for _, b := range m.blocks[start:] {
		body = append(body, m.renderBlock(b)...)
		body = append(body, "")
	}
	height := m.height - len(chrome)
	if m.inline {
		// The live region must stay shorter than the window, or the renderer falls back to a clear.
		height = max(1, min(height, m.height/2))
	}
	end := len(body) - m.scroll
	if end < 0 {
		end = 0
	}
	from := max(0, end-height)
	visible := body[from:end]
	if !m.inline {
		for len(visible) < height {
			visible = append([]string{""}, visible...)
		}
	}
	v := tea.NewView(strings.Join(append(visible, chrome...), "\n"))
	v.AltScreen = !m.inline
	if !m.inline {
		v.MouseMode = tea.MouseModeCellMotion
	}
	return v
}

func main() {
	events := flag.Int("events", 200, "number of synthetic events to replay")
	delay := flag.Duration("delay", 15*time.Millisecond, "time between events")
	quitAfter := flag.Duration("quit-after", 0, "exit this long after the replay ends (0 stays open)")
	inline := flag.Bool("inline", false, "commit finished blocks above a live region instead of fullscreen")
	warmup := flag.Duration("warmup", 0, "wait this long before the replay starts")
	flag.Parse()

	m := model{
		inline:    *inline,
		events:    script(*events),
		delay:     *delay,
		quitAfter: *quitAfter,
		started:   time.Now(),
		warmup:    *warmup,
	}
	if _, err := tea.NewProgram(m).Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
