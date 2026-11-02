// Package tui is daedalus-cli's terminal interface: a transcript of the session, a prompt box pinned
// under it, and the dialogs that ask the person for decisions, in the look and with the keys of
// Claude Code.
//
// It decides nothing about the agent. Everything in the transcript comes from an event of the
// Backend, and everything the person does leaves through one of its methods; the screen itself is a
// function of those events and the keys pressed, which is what lets it be tested frame by frame
// against the fake.
package tui

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/files"
	"github.com/ascorblack/daedalus/cli/internal/tui/markdown"
	"github.com/ascorblack/daedalus/cli/internal/tui/textedit"
	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// Options configure one run of the interface.
type Options struct {
	// Cwd is the working folder the session is bound to.
	Cwd string
	// AgentName is what the interface calls the agent ("Daedalus").
	AgentName string
	Version   string
	// Theme overrides the saved theme when set.
	Theme string
	// Inline, when set, overrides the saved choice between fullscreen and inline.
	Inline *bool
	// Dark is the terminal's background when the caller has detected it; nil asks the terminal.
	Dark *bool
	// Guard, when set (ask, auto or plan), is the guard to start in, over the one the prefs remember:
	// the --guard flag or the profile's choice. The interface pushes its guard to the backend when a
	// session loads, so without this the remembered level silently replaced the flag.
	Guard string
	// Plain and ASCII come from theme.Environment unless the caller decides.
	Plain, ASCII bool
	// Platform is the GOOS whose markers are drawn; empty is the running system.
	Platform string
	Store    Store
	Start    StartRequest
	// Now is the clock; tests fix it so frames are reproducible.
	Now func() time.Time
	// Editor is the external editor for Ctrl+G; empty reads $VISUAL and $EDITOR.
	Editor string
	// Clipboard reads a pasted image; nil uses the platform's clipboard tools.
	Clipboard func(ctx context.Context) (Image, error)
}

// Run shows the interface until the person exits.
func Run(ctx context.Context, backend Backend, options Options) error {
	if !options.Plain && !options.ASCII {
		options.Plain, options.ASCII = theme.Environment()
	}
	m := New(backend, options)
	program := tea.NewProgram(m, tea.WithContext(ctx))
	_, err := program.Run()
	if err == tea.ErrProgramKilled && ctx.Err() != nil {
		return nil
	}
	return err
}

// Model is the interface's whole state.
type Model struct {
	backend Backend
	options Options
	store   Store
	prefs   Prefs
	ctx     context.Context
	cancel  context.CancelFunc

	theme  *theme.Theme
	dark   bool
	md     *markdown.Renderer
	width  int
	height int
	focus  bool

	session  SessionInfo
	started  bool
	startErr string

	blocks []*block
	// byID finds the block an event refers to: a tool call, a streamed message.
	byID map[string]*block
	// committed is how many blocks have been printed into the terminal's scrollback in inline
	// mode; they are no longer drawn by the view.
	committed int

	// scroll is how many lines the transcript is scrolled up from the bottom; unseen counts the
	// blocks that arrived while it was scrolled.
	scroll int
	unseen int

	running    bool
	turnStart  time.Time
	turnChars  int
	turnTokens int
	verb       string
	frame      int
	ticking    bool
	compacting bool
	compactAt  time.Time

	conn ConnState
	// connAttempt and connRetry describe the current reconnect.
	connAttempt int
	connRetry   time.Duration
	connErr     string
	connSince   time.Time
	// waitingDevice names the session's home device while the server holds the turn for it.
	waitingDevice string

	usage    Usage
	queue    []QueuedMessage
	todos    []Todo
	showTodo bool
	guard    Guard

	editor    *textedit.Editor
	shell     bool
	history   []string
	histPos   int
	histDraft string
	// draft is what Ctrl+C or Esc Esc cleared, brought back by the next Up.
	draft string
	// shellContext is `!` output waiting to go to the agent with the next prompt.
	shellContext []string
	images       []Image

	dialog dialog
	pager  *pager
	sel    *selection
	search *reverseSearch
	comp   *completion
	help   bool

	commands     []Command
	index        *files.Index
	indexing     bool
	pendingIndex tea.Cmd

	// pendingKey and pendingAt make a key a double press: the second Ctrl+C or Ctrl+D within the
	// window exits, the second Esc clears or rewinds.
	pendingKey string
	pendingAt  time.Time
	hint       string
	hintUntil  time.Time

	toast      string
	toastUntil time.Time

	quitting bool
	// viewed is set once a frame has been drawn. Inline mode prints nothing into the scrollback
	// before that: Bubble Tea clears the lines already on screen when a print precedes the first
	// frame, which the renderer gate measured as a wiped shell history.
	viewed        bool
	viewedAt      time.Time
	commitPending bool
	branch        string
	// manual is set by tests that feed events and frames by hand: no event reader and no ticker run,
	// so a frame is a function of the events applied and nothing else.
	manual bool
	seq    int
}

// doublePress is the window in which a second press counts, Claude Code's 800 ms.
const doublePress = 800 * time.Millisecond

// New builds the model; Run is the usual entry point.
func New(backend Backend, options Options) *Model {
	if options.AgentName == "" {
		options.AgentName = "Daedalus"
	}
	if options.Now == nil {
		options.Now = time.Now
	}
	if options.Store == nil {
		options.Store = &MemoryStore{}
	}
	ctx, cancel := context.WithCancel(context.Background())
	m := &Model{
		backend: backend,
		options: options,
		store:   options.Store,
		ctx:     ctx,
		cancel:  cancel,
		byID:    map[string]*block{},
		editor:  textedit.New(),
		focus:   true,
		conn:    Connecting,
		guard:   GuardAuto,
	}
	m.prefs = m.store.LoadPrefs()
	if options.Theme != "" {
		m.prefs.Theme = options.Theme
	}
	if options.Inline != nil {
		m.prefs.Inline = *options.Inline
	}
	if options.Guard != "" {
		m.prefs.Guard = options.Guard
	}
	switch m.prefs.Guard {
	case "ask":
		m.guard = GuardAsk
	case "plan":
		m.guard = GuardPlan
	}
	m.dark = true
	if options.Dark != nil {
		m.dark = *options.Dark
	}
	m.editor.SetVim(m.prefs.Vim)
	m.history = m.store.History(options.Cwd)
	m.histPos = len(m.history)
	m.applyTheme()
	m.session = SessionInfo{Cwd: options.Cwd}
	m.blocks = []*block{{kind: blockWelcome, text: options.Cwd}}
	return m
}

func (m *Model) applyTheme() {
	m.theme = theme.New(theme.Options{Name: m.prefs.Theme, Dark: m.dark, Plain: m.options.Plain, ASCII: m.options.ASCII, Platform: m.options.Platform})
	m.md = markdown.New(m.theme)
	for _, b := range m.blocks {
		if b.stream != nil {
			b.stream = rebind(b.stream, m.md)
		}
		b.cache.valid = false
	}
}

// rebind moves a stream's text onto a renderer built for a new theme.
func rebind(s *markdown.Stream, r *markdown.Renderer) *markdown.Stream {
	fresh := markdown.NewStream(r)
	fresh.Append(s.Text())
	return fresh
}

func (m *Model) now() time.Time { return m.options.Now() }

// Init starts the session and the event reader.
func (m *Model) Init() tea.Cmd {
	cmds := []tea.Cmd{m.startSession(m.options.Start), m.readEvents(), m.detectBranch(), m.loadCommands()}
	if m.options.Dark == nil {
		cmds = append(cmds, tea.RequestBackgroundColor)
	}
	return tea.Batch(cmds...)
}

type (
	eventMsg        struct{ event Event }
	eventsClosedMsg struct{}
	tickMsg         time.Time
	startedMsg      struct {
		session Session
		err     error
		cleared bool
	}
	resultMsg struct {
		op  string
		err error
	}
	commandsMsg struct{ commands []Command }
	branchMsg   struct{ branch string }
	indexMsg    struct{ index *files.Index }
	shellMsg    struct {
		command string
		result  ShellResult
		err     error
	}
	withdrawMsg struct {
		text string
		err  error
	}
	imageMsg struct {
		image Image
		err   error
	}
	editorMsg struct {
		text string
		err  error
	}
)

func (m *Model) readEvents() tea.Cmd {
	if m.manual {
		return nil
	}
	ch := m.backend.Events()
	return func() tea.Msg {
		event, ok := <-ch
		if !ok {
			return eventsClosedMsg{}
		}
		return eventMsg{event}
	}
}

func (m *Model) startSession(request StartRequest) tea.Cmd {
	return func() tea.Msg {
		session, err := m.backend.Start(m.ctx, request)
		return startedMsg{session: session, err: err}
	}
}

func (m *Model) loadCommands() tea.Cmd {
	return func() tea.Msg {
		commands, err := m.backend.Commands(m.ctx)
		if err != nil {
			return nil
		}
		return commandsMsg{commands}
	}
}

func (m *Model) detectBranch() tea.Cmd {
	cwd := m.options.Cwd
	return func() tea.Msg {
		if cwd == "" {
			return nil
		}
		cmd := exec.Command("git", "rev-parse", "--abbrev-ref", "HEAD")
		cmd.Dir = cwd
		out, err := cmd.Output()
		if err != nil {
			return nil
		}
		return branchMsg{strings.TrimSpace(string(out))}
	}
}

// call runs a backend method off the update loop and reports its error.
func (m *Model) call(op string, fn func(ctx context.Context) error) tea.Cmd {
	return func() tea.Msg {
		return resultMsg{op: op, err: fn(m.ctx)}
	}
}

// tick keeps the spinner turning while anything animates, and stops when nothing does so an idle
// screen costs no redraws.
func (m *Model) tick() tea.Cmd {
	if m.ticking || m.manual {
		return nil
	}
	m.ticking = true
	return tea.Tick(100*time.Millisecond, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func (m *Model) animating() bool {
	now := m.now()
	return m.running || m.compacting || m.conn == Connecting || m.conn == Reconnecting ||
		m.hasLiveTool() || now.Before(m.toastUntil) || now.Before(m.hintUntil)
}

func (m *Model) hasLiveTool() bool {
	for i := len(m.blocks) - 1; i >= 0 && i >= len(m.blocks)-20; i-- {
		if m.blocks[i].kind == blockTool && m.blocks[i].live() {
			return true
		}
	}
	return false
}

// Update is the one place state changes.
func (m *Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil
	case tea.BackgroundColorMsg:
		m.dark = msg.IsDark()
		m.applyTheme()
		return m, nil
	case tea.FocusMsg:
		m.focus = true
		return m, nil
	case tea.BlurMsg:
		m.focus = false
		return m, nil
	case tickMsg:
		m.ticking = false
		m.frame++
		if m.animating() {
			return m, m.tick()
		}
		return m, nil
	case eventMsg:
		cmd := m.applyEvent(msg.event, false)
		return m, tea.Batch(cmd, m.readEvents(), m.commit(), m.tickIfNeeded())
	case eventsClosedMsg:
		m.conn = Offline
		return m, nil
	case startedMsg:
		return m, m.started_(msg)
	case resultMsg:
		if msg.err != nil {
			m.addNotice(NoticeError, msg.op+": "+msg.err.Error())
		}
		return m, m.commit()
	case commandsMsg:
		m.commands = msg.commands
		return m, nil
	case branchMsg:
		m.branch = msg.branch
		return m, nil
	case indexMsg:
		m.index, m.indexing = msg.index, false
		m.refreshCompletion()
		return m, nil
	case shellMsg:
		return m, m.shellDone(msg)
	case withdrawMsg:
		if msg.err == nil && m.editor.Empty() {
			m.editor.SetValue(msg.text)
		} else if msg.err != nil {
			m.flash("Already read by the agent")
		}
		return m, m.tickIfNeeded()
	case imageMsg:
		if msg.err != nil {
			m.flash(msg.err.Error())
			return m, m.tickIfNeeded()
		}
		m.images = append(m.images, msg.image)
		m.editor.InsertImage(len(m.images))
		return m, nil
	case editorMsg:
		if msg.err != nil {
			m.flash("editor: " + msg.err.Error())
		} else {
			m.editor.SetValue(strings.TrimRight(msg.text, "\n"))
		}
		return m, m.tickIfNeeded()
	case commitMsg:
		m.commitPending = false
		return m, m.commit()
	case dialogMsg:
		if m.dialog != nil {
			return m, m.dialog.update(m, msg)
		}
		return m, nil
	case tea.PasteMsg:
		return m, m.paste(msg.Content)
	case tea.MouseWheelMsg:
		if m.pager != nil {
			return m, m.pager.wheel(m, msg)
		}
		if msg.Button == tea.MouseWheelUp {
			m.scrollBy(3)
		} else if msg.Button == tea.MouseWheelDown {
			m.scrollBy(-3)
		}
		return m, nil
	case tea.MouseClickMsg:
		return m, m.mouseClick(msg)
	case tea.MouseMotionMsg:
		return m, m.mouseMotion(msg)
	case tea.MouseReleaseMsg:
		return m, m.mouseRelease(msg)
	case tea.KeyPressMsg:
		return m, m.key(msg)
	}
	return m, nil
}

func (m *Model) tickIfNeeded() tea.Cmd {
	if m.animating() {
		return m.tick()
	}
	return nil
}

func (m *Model) started_(msg startedMsg) tea.Cmd {
	if msg.err != nil {
		m.startErr = msg.err.Error()
		m.addNotice(NoticeError, "Could not open the session: "+msg.err.Error())
		if strings.Contains(strings.ToLower(msg.err.Error()), "unauthor") || strings.Contains(msg.err.Error(), "401") {
			m.addNotice(NoticeInfo, "Run /login to pair this device with the server.")
		}
		return m.commit()
	}
	if msg.cleared {
		m.resetTranscript()
	}
	m.loadSession(msg.session)
	return tea.Batch(m.commit(), m.tickIfNeeded(), m.call("guard", func(ctx context.Context) error {
		return m.backend.SetGuard(ctx, m.guard)
	}))
}

// loadSession draws a session's snapshot: its history, then whatever waits for the person.
func (m *Model) loadSession(session Session) {
	m.started = true
	m.session = session.Info
	if m.session.Cwd == "" {
		m.session.Cwd = m.options.Cwd
	}
	for _, event := range session.History {
		m.applyEvent(event, true)
	}
	m.usage = session.Usage
	m.queue = session.Queue
	m.todos = session.Todos
	if session.Running {
		m.beginTurn(m.now())
	}
	for _, event := range session.Pending {
		m.applyEvent(event, false)
	}
}

func (m *Model) resetTranscript() {
	m.blocks = []*block{{kind: blockWelcome, text: m.options.Cwd}}
	m.byID = map[string]*block{}
	m.committed = 0
	m.scroll, m.unseen = 0, 0
	m.todos, m.queue = nil, nil
	m.running = false
}

func (m *Model) nextID() string {
	m.seq++
	return "cli-" + itoa(m.seq) + "-" + itoa(int(m.now().UnixNano()%1_000_000))
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}

// flash shows a short message in the footer for two seconds.
func (m *Model) flash(text string) {
	m.toast = text
	m.toastUntil = m.now().Add(2 * time.Second)
}

func (m *Model) setHint(key, text string) {
	m.pendingKey = key
	m.pendingAt = m.now()
	m.hint = text
	m.hintUntil = m.now().Add(doublePress)
}

// pressedTwice reports whether key completes a double press, and arms it otherwise.
func (m *Model) pressedTwice(key, hint string) bool {
	if m.pendingKey == key && m.now().Sub(m.pendingAt) <= doublePress {
		m.pendingKey, m.hint = "", ""
		return true
	}
	m.setHint(key, hint)
	return false
}

func (m *Model) addNotice(level NoticeLevel, text string) {
	m.appendBlock(&block{kind: blockNotice, level: level, text: text, at: m.now()})
}

func (m *Model) appendBlock(b *block) {
	m.blocks = append(m.blocks, b)
	if m.scroll > 0 {
		m.unseen++
	}
}

func (m *Model) inline() bool { return m.prefs.Inline }

func (m *Model) quit() tea.Cmd {
	m.quitting = true
	m.cancel()
	return tea.Quit
}

// savePrefs writes the prefs off the update loop; a failure to save is reported, not fatal.
func (m *Model) savePrefs() tea.Cmd {
	prefs := m.prefs
	store := m.store
	return func() tea.Msg {
		if err := store.SavePrefs(prefs); err != nil {
			return resultMsg{op: "save settings", err: err}
		}
		return nil
	}
}

func (m *Model) editorCommand() string {
	if m.options.Editor != "" {
		return m.options.Editor
	}
	if v := os.Getenv("VISUAL"); v != "" {
		return v
	}
	if v := os.Getenv("EDITOR"); v != "" {
		return v
	}
	return "vi"
}

func padRight(s string, width int) string {
	w := ansi.StringWidth(s)
	if w >= width {
		return s
	}
	return s + strings.Repeat(" ", width-w)
}
