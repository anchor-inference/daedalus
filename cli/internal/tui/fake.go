package tui

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// Fake is a backend with no server behind it: a scripted agent that answers prompts with the same
// events a real session produces. The tests drive the interface with it, the pty harness records it,
// and `demo` runs it so the screens can be looked at without pairing a device.
//
// What the scripted agent does depends on words in the prompt, so one fake can show every screen:
// "edit" or "diff" makes a file edit, "test" or "permission" asks for permission to run a command,
// "ask" asks a question, "todo" works through a todo list, "long" prints a long output, "agent"
// runs a subagent, "drop" or "connection" loses the connection mid-turn and comes back, "fail" fails
// the turn.
// Anything else gets a streamed answer after a couple of reads.
type Fake struct {
	// Delay is the base pause between streamed events. Zero streams with no pauses at all, which is
	// what tests want.
	Delay time.Duration
	// RealShell runs `!` commands for real with sh -c; otherwise they get a canned output.
	RealShell bool
	// Now is the clock the fake stamps events with.
	Now func() time.Time
	// History is what Start returns for a resumed session.
	History []Event

	events chan Event

	mu        sync.Mutex
	info      SessionInfo
	running   bool
	cancel    context.CancelFunc
	queue     []QueuedMessage
	decisions map[string]chan Decision
	answers   map[string]chan []Answer
	seq       int
	guard     Guard
	prompts   []Checkpoint
	sent      []Prompt
	steered   []string
	usage     Usage
}

// NewFake builds a fake for a session in cwd.
func NewFake(cwd string) *Fake {
	return &Fake{
		events:    make(chan Event, 4096),
		decisions: map[string]chan Decision{},
		answers:   map[string]chan []Answer{},
		info: SessionInfo{
			ID: "s-7f3a", Title: "New session", Kind: "terminal", Cwd: cwd,
			Device: "laptop", Model: "claude-sonnet", Effort: "medium", Server: "http://127.0.0.1:8765",
		},
		usage: Usage{ContextWindow: 200_000, ContextTokens: 18_400},
		guard: GuardAuto,
	}
}

func (f *Fake) now() time.Time {
	if f.Now != nil {
		return f.Now()
	}
	return time.Now()
}

func (f *Fake) emit(e Event) { f.events <- e }

func (f *Fake) nextID(prefix string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	return fmt.Sprintf("%s%d", prefix, f.seq)
}

// Sent returns the prompts sent so far, for tests.
func (f *Fake) Sent() []Prompt {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Prompt(nil), f.sent...)
}

// Guard returns the guard last set, for tests.
func (f *Fake) Guard() Guard {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.guard
}

func (f *Fake) Events() <-chan Event { return f.events }

func (f *Fake) Start(ctx context.Context, request StartRequest) (Session, error) {
	f.mu.Lock()
	info := f.info
	if request.Resume != "" {
		info.ID = request.Resume
		info.Title = "Refactor the session store"
	}
	f.info = info
	f.mu.Unlock()
	session := Session{Info: info, Usage: f.usage}
	if request.Resume != "" || request.Continue {
		session.History = f.History
		if session.History == nil {
			session.History = sampleHistory(f.now())
		}
	}
	go f.emit(ConnectionChanged{State: Online})
	if request.Prompt != "" {
		go func() { _ = f.Send(context.Background(), Prompt{Text: request.Prompt, ClientID: "first"}) }()
	}
	return session, nil
}

func (f *Fake) Sessions(ctx context.Context, limit int) ([]SessionSummary, error) {
	now := f.now()
	rows := []SessionSummary{
		{ID: "s-91c2", Title: "Refactor the session store", Kind: "terminal", Cwd: f.info.Cwd, Updated: now.Add(-25 * time.Minute), Messages: 42},
		{ID: "s-88b0", Title: "Why does the build fail on Windows?", Kind: "chat", Updated: now.Add(-3 * time.Hour), Messages: 12},
		{ID: "s-77aa", Title: "Draft the release notes", Kind: "chat", Updated: now.Add(-26 * time.Hour), Messages: 8, Running: true},
		{ID: "s-6602", Title: "Port the diff renderer", Kind: "terminal", Cwd: f.info.Cwd, Updated: now.Add(-72 * time.Hour), Messages: 97},
	}
	if limit > 0 && len(rows) > limit {
		rows = rows[:limit]
	}
	return rows, nil
}

func (f *Fake) Send(ctx context.Context, prompt Prompt) error {
	f.mu.Lock()
	f.sent = append(f.sent, prompt)
	if prompt.ClientID == "" {
		f.seq++
		prompt.ClientID = fmt.Sprintf("c%d", f.seq)
	}
	if f.running {
		f.queue = append(f.queue, QueuedMessage{ID: prompt.ClientID, Text: prompt.Text})
		queue := append([]QueuedMessage(nil), f.queue...)
		f.mu.Unlock()
		f.emit(QueueChanged{Items: queue})
		return nil
	}
	f.running = true
	turnCtx, cancel := context.WithCancel(context.Background())
	f.cancel = cancel
	f.prompts = append(f.prompts, Checkpoint{ID: fmt.Sprintf("cp%d", len(f.prompts)+1), Prompt: prompt.Text, At: f.now(), Code: true})
	f.mu.Unlock()
	f.emit(UserMessage{ID: prompt.ClientID, Text: prompt.Text, Images: len(prompt.Images), At: f.now()})
	go f.turn(turnCtx, prompt.Text)
	return nil
}

func (f *Fake) Interrupt(ctx context.Context) error {
	f.mu.Lock()
	cancel := f.cancel
	f.mu.Unlock()
	if cancel == nil {
		return errors.New("nothing is running")
	}
	cancel()
	return nil
}

func (f *Fake) Withdraw(ctx context.Context, queuedID string) (string, error) {
	f.mu.Lock()
	for i, q := range f.queue {
		if q.ID == queuedID {
			f.queue = append(f.queue[:i:i], f.queue[i+1:]...)
			queue := append([]QueuedMessage(nil), f.queue...)
			f.mu.Unlock()
			f.emit(QueueChanged{Items: queue})
			return q.Text, nil
		}
	}
	f.mu.Unlock()
	return "", errors.New("already read by the agent")
}

func (f *Fake) Decide(ctx context.Context, requestID string, decision Decision) error {
	f.mu.Lock()
	ch := f.decisions[requestID]
	delete(f.decisions, requestID)
	f.mu.Unlock()
	if ch == nil {
		return errors.New("no such request")
	}
	ch <- decision
	return nil
}

func (f *Fake) Answer(ctx context.Context, requestID string, answers []Answer) error {
	f.mu.Lock()
	ch := f.answers[requestID]
	delete(f.answers, requestID)
	f.mu.Unlock()
	if ch == nil {
		return errors.New("no such question")
	}
	ch <- answers
	return nil
}

func (f *Fake) Commands(ctx context.Context) ([]Command, error) {
	return []Command{
		{Name: "review", Description: "Review the changes on this branch", Source: CommandSkill},
		{Name: "release-notes", Description: "Draft release notes from the commits since the last tag", Source: CommandSkill},
		{Name: "memory", Description: "Show what the agent remembers about this project", Source: CommandServer},
		{Name: "jobs", Description: "List background jobs of this session", Source: CommandServer},
	}, nil
}

func (f *Fake) RunCommand(ctx context.Context, line string) error {
	f.emit(Notice{Level: NoticeInfo, Text: "ran " + line})
	return nil
}

func (f *Fake) Models(ctx context.Context) ([]ModelOption, error) {
	return []ModelOption{
		{ID: "claude-opus", Label: "Opus", Efforts: []string{"low", "medium", "high", "max"}, ContextWindow: 200_000},
		{ID: "claude-sonnet", Label: "Sonnet", Efforts: []string{"low", "medium", "high"}, Default: true, ContextWindow: 200_000},
		{ID: "gpt-5", Label: "GPT-5", Efforts: []string{"minimal", "low", "medium", "high"}, ContextWindow: 400_000},
	}, nil
}

func (f *Fake) SetModel(ctx context.Context, model, effort string) error {
	f.mu.Lock()
	f.info.Model = model
	if effort != "" {
		f.info.Effort = effort
	}
	info := f.info
	f.mu.Unlock()
	f.emit(ModelChanged{Model: info.Model, Effort: info.Effort})
	return nil
}

func (f *Fake) Compact(ctx context.Context, focus string) error {
	go func() {
		f.emit(CompactionChanged{Running: true})
		f.sleep(context.Background(), 20)
		f.mu.Lock()
		f.usage.ContextTokens = 3_100
		usage := f.usage
		f.mu.Unlock()
		f.emit(CompactionChanged{Running: false, Summary: "Summarised 42 messages."})
		f.emit(UsageChanged{Usage: usage})
	}()
	return nil
}

func (f *Fake) Clear(ctx context.Context) (Session, error) {
	f.mu.Lock()
	f.info.ID = fmt.Sprintf("s-%04x", f.seq+0x1000)
	f.info.Title = "New session"
	f.prompts = nil
	f.usage.ContextTokens = 0
	info, usage := f.info, f.usage
	f.mu.Unlock()
	return Session{Info: info, Usage: usage}, nil
}

func (f *Fake) Checkpoints(ctx context.Context) ([]Checkpoint, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.prompts) == 0 {
		now := f.now()
		return []Checkpoint{
			{ID: "cp1", Prompt: "Read the session store and tell me what it does", At: now.Add(-20 * time.Minute), Code: true},
			{ID: "cp2", Prompt: "Split the cursor logic out of the store", At: now.Add(-12 * time.Minute), Code: true},
			{ID: "cp3", Prompt: "Now run the tests", At: now.Add(-4 * time.Minute), Code: false},
		}, nil
	}
	return append([]Checkpoint(nil), f.prompts...), nil
}

func (f *Fake) Rewind(ctx context.Context, checkpointID string, scope RewindScope) error {
	what := map[RewindScope]string{RewindBoth: "code and conversation", RewindConversation: "conversation", RewindCode: "code", RewindSummarize: "conversation, summarised"}[scope]
	f.emit(HistoryReset{History: sampleHistory(f.now())[:2]})
	f.emit(Notice{Level: NoticeInfo, Text: "Restored " + what + " to the checkpoint."})
	return nil
}

func (f *Fake) Shell(ctx context.Context, command string) (ShellResult, error) {
	if f.RealShell {
		cmd := exec.CommandContext(ctx, "sh", "-c", command)
		cmd.Dir = f.info.Cwd
		out, err := cmd.CombinedOutput()
		code := 0
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			code = exitErr.ExitCode()
		} else if err != nil {
			return ShellResult{}, err
		}
		return ShellResult{Output: string(out), ExitCode: code}, nil
	}
	return ShellResult{Output: "README.md\ncli\ndaedalus\ndocs\nminiapp\n", ExitCode: 0}, nil
}

func (f *Fake) SetGuard(ctx context.Context, guard Guard) error {
	f.mu.Lock()
	f.guard = guard
	f.mu.Unlock()
	return nil
}

func (f *Fake) Status(ctx context.Context) (Status, error) {
	f.mu.Lock()
	info := f.info
	f.mu.Unlock()
	return Status{Items: []StatusItem{
		{Label: "Server", Value: info.Server},
		{Label: "Device", Value: info.Device},
		{Label: "Session", Value: info.ID + " · " + info.Title},
		{Label: "Folder", Value: info.Cwd},
		{Label: "Model", Value: info.Model + " (" + info.Effort + ")"},
		{Label: "Client", Value: "daedalus-cli (demo backend)"},
	}}, nil
}

func (f *Fake) Login(ctx context.Context) (LoginChallenge, error) {
	go func() {
		f.sleep(context.Background(), 40)
		f.emit(LoginChanged{State: LoginApproved, Device: "laptop"})
	}()
	return LoginChallenge{Code: "WDJB-MJHT", URL: "http://127.0.0.1:8765/app/?pair-device=WDJB-MJHT", ExpiresAt: f.now().Add(10 * time.Minute)}, nil
}

// sleep pauses for n units of Delay, or returns false at once when the turn was interrupted.
func (f *Fake) sleep(ctx context.Context, n int) bool {
	if f.Delay <= 0 {
		return ctx.Err() == nil
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(time.Duration(n) * f.Delay):
		return true
	}
}

// stream sends text in small pieces, as a model streams it.
func (f *Fake) stream(ctx context.Context, id, text string, thinking bool) bool {
	words := strings.SplitAfter(text, " ")
	for i := 0; i < len(words); i += 3 {
		end := min(i+3, len(words))
		piece := strings.Join(words[i:end], "")
		if thinking {
			f.emit(ThinkingDelta{MessageID: id, Text: piece})
		} else {
			f.emit(TextDelta{MessageID: id, Text: piece})
		}
		f.mu.Lock()
		f.usage.OutputTokens += 4
		f.mu.Unlock()
		if !f.sleep(ctx, 1) {
			return false
		}
	}
	f.emit(MessageDone{MessageID: id})
	return true
}

func (f *Fake) tool(ctx context.Context, name string, input map[string]any, output string, isError bool, parent string) (string, bool) {
	id := f.nextID("t")
	f.emit(ToolStarted{ID: id, Name: name, Input: input, Parent: parent, At: f.now()})
	if !f.sleep(ctx, 6) {
		return id, false
	}
	f.emit(ToolFinished{ID: id, Output: output, IsError: isError, At: f.now()})
	return id, true
}

// drainQueue reads the queued messages into the turn, as the server steers them in after a tool,
// and remembers them so the turn answers them before it ends.
func (f *Fake) drainQueue() {
	f.mu.Lock()
	queue := f.queue
	f.queue = nil
	f.mu.Unlock()
	if len(queue) == 0 {
		return
	}
	f.emit(QueueChanged{})
	for _, q := range queue {
		f.emit(UserMessage{ID: q.ID, Text: q.Text, At: f.now()})
		f.mu.Lock()
		f.steered = append(f.steered, q.Text)
		f.mu.Unlock()
	}
}

// takeSteered returns the last message steered into the turn and forgets them all: a real agent
// reads every steer, and the scripted one answers the last.
func (f *Fake) takeSteered() string {
	f.drainQueue()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.steered) == 0 {
		return ""
	}
	last := f.steered[len(f.steered)-1]
	f.steered = nil
	return last
}

func (f *Fake) end(outcome TurnOutcome, err string) {
	f.mu.Lock()
	f.running = false
	f.cancel = nil
	f.usage.ContextTokens += 2_400
	f.usage.InputTokens += 12_000
	usage := f.usage
	f.mu.Unlock()
	f.emit(UsageChanged{Usage: usage})
	f.emit(TurnEnded{Outcome: outcome, Err: err, At: f.now()})
}

func (f *Fake) turn(ctx context.Context, prompt string) {
	runID := f.nextID("r")
	f.emit(TurnStarted{RunID: runID, At: f.now()})
	for {
		outcome, ok := f.scenario(ctx, prompt)
		if !ok || ctx.Err() != nil {
			f.end(TurnInterrupted, "")
			return
		}
		if outcome != "" {
			f.end(TurnFailed, outcome)
			return
		}
		// Messages steered in while the turn ran are answered before it ends, as the server's agent
		// reads them; one that arrived after the last tool would otherwise go unanswered.
		next := f.takeSteered()
		if next == "" {
			break
		}
		prompt = next
	}
	f.end(TurnCompleted, "")
}

// scenario plays the scripted reply to one prompt. It returns a failure text for a turn that fails,
// and false when it was interrupted.
func (f *Fake) scenario(ctx context.Context, prompt string) (string, bool) {
	lower := strings.ToLower(prompt)
	has := func(words ...string) bool {
		for _, w := range words {
			if strings.Contains(lower, w) {
				return true
			}
		}
		return false
	}
	if !f.stream(ctx, f.nextID("m"), "The person wants me to look at the code first. I should read the store and search for the cursor handling before changing anything.", true) {
		return "", false
	}
	ok := true
	switch {
	case has("fail"):
		return "the model provider returned 529: overloaded", true
	case has("edit", "diff", "fix"):
		ok = f.editTurn(ctx)
	case has("test", "permission"):
		ok = f.permissionTurn(ctx)
	case has("ask", "question"):
		ok = f.questionTurn(ctx)
	case has("todo"):
		ok = f.todoTurn(ctx)
	case has("long"):
		_, ok = f.tool(ctx, "Exec", map[string]any{"command": "go test ./..."}, longOutput(), false, "")
		if ok {
			ok = f.stream(ctx, f.nextID("m"), "All **37** packages pass. The slowest is `internal/store` at 2.1s.", false)
		}
	case has("agent"):
		ok = f.agentTurn(ctx)
	case has("drop", "connection"):
		ok = f.dropTurn(ctx)
	default:
		ok = f.answerTurn(ctx)
	}
	return "", ok
}

func (f *Fake) answerTurn(ctx context.Context) bool {
	cwd := f.info.Cwd
	if _, ok := f.tool(ctx, "Read", map[string]any{"path": cwd + "/internal/store/store.go"}, strings.Repeat("line\n", 182), false, ""); !ok {
		return false
	}
	if _, ok := f.tool(ctx, "Search", map[string]any{"pattern": "cursor", "path": cwd + "/internal"}, "internal/store/store.go:41\ninternal/store/store.go:88\ninternal/store/cursor.go:12\n", false, ""); !ok {
		return false
	}
	f.drainQueue()
	return f.stream(ctx, f.nextID("m"), sampleAnswer, false)
}

func (f *Fake) editTurn(ctx context.Context) bool {
	cwd := f.info.Cwd
	if _, ok := f.tool(ctx, "Read", map[string]any{"path": cwd + "/internal/store/cursor.go"}, sampleBefore, false, ""); !ok {
		return false
	}
	id := f.nextID("t")
	input := map[string]any{"path": cwd + "/internal/store/cursor.go", "old_string": "if after < 0 {\n\t\treturn 0\n\t}", "new_string": "if after < 0 || after > c.head {\n\t\treturn c.head\n\t}"}
	f.emit(ToolStarted{ID: id, Name: "Edit", Input: input, At: f.now()})
	if f.guardAsks() {
		decision, ok := f.ask(ctx, PermissionRequest{
			ID: f.nextID("p"), Source: SourceLocal, Tool: "Edit", Text: "internal/store/cursor.go",
			Similar: "edits in this folder", Change: &FileChange{Path: "internal/store/cursor.go", Before: sampleBefore, After: sampleAfter},
		})
		if !ok {
			return false
		}
		if decision.Kind == Deny {
			f.emit(ToolFinished{ID: id, Output: "refused on this machine: " + decision.Feedback, IsError: true, At: f.now()})
			return f.stream(ctx, f.nextID("m"), "Understood, I left the file as it was.", false)
		}
	}
	if !f.sleep(ctx, 4) {
		return false
	}
	f.emit(FileChanged{ToolID: id, Change: FileChange{Path: cwd + "/internal/store/cursor.go", Before: sampleBefore, After: sampleAfter}})
	f.emit(ToolFinished{ID: id, Output: "edited internal/store/cursor.go", At: f.now()})
	f.drainQueue()
	return f.stream(ctx, f.nextID("m"), "Fixed: a cursor **ahead of the head** now clamps to the head instead of replaying from zero, which is what caused the duplicate events after a reconnect.", false)
}

func (f *Fake) guardAsks() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.guard == GuardAsk
}

func (f *Fake) ask(ctx context.Context, request PermissionRequest) (Decision, bool) {
	ch := make(chan Decision, 1)
	f.mu.Lock()
	f.decisions[request.ID] = ch
	f.mu.Unlock()
	f.emit(PermissionRequested{Request: request})
	select {
	case <-ctx.Done():
		return Decision{}, false
	case d := <-ch:
		label := map[DecisionKind]string{Allow: "allow", AllowSimilar: "allow", Deny: "deny"}[d.Kind]
		f.emit(PermissionResolved{ID: request.ID, Decision: label, Via: "cli"})
		return d, true
	}
}

func (f *Fake) permissionTurn(ctx context.Context) bool {
	id := f.nextID("t")
	f.emit(ToolStarted{ID: id, Name: "Exec", Input: map[string]any{"command": "npm test -- --watch=false"}, At: f.now()})
	decision, ok := f.ask(ctx, PermissionRequest{
		ID: f.nextID("p"), Source: SourceServer, Kind: "policy", Tool: "Exec",
		Text: "npm test -- --watch=false", Similar: "npm test *",
	})
	if !ok {
		return false
	}
	if decision.Kind == Deny {
		f.emit(ToolFinished{ID: id, Output: "The operator refused this command.", IsError: true, At: f.now()})
		if decision.Feedback != "" {
			return f.stream(ctx, f.nextID("m"), "Understood — "+decision.Feedback, false)
		}
		return true
	}
	if !f.sleep(ctx, 8) {
		return false
	}
	f.emit(ToolFinished{ID: id, Output: "> app@1.4.0 test\n> vitest run\n\n ✓ src/store.test.ts (12 tests) 41ms\n ✓ src/cursor.test.ts (7 tests) 18ms\n\n Test Files  2 passed (2)\n      Tests  19 passed (19)\n", At: f.now()})
	f.drainQueue()
	return f.stream(ctx, f.nextID("m"), "All 19 tests pass.", false)
}

func (f *Fake) questionTurn(ctx context.Context) bool {
	ch := make(chan []Answer, 1)
	id := f.nextID("q")
	f.mu.Lock()
	f.answers[id] = ch
	f.mu.Unlock()
	f.emit(QuestionAsked{Question: Question{ID: id, Items: []QuestionItem{
		{Header: "Storage", Text: "Where should the session cursor live?", Options: []QuestionOption{
			{Label: "In the store", Description: "One table, one transaction; simplest to reason about"},
			{Label: "In memory", Description: "Fastest, but lost on restart"},
			{Label: "In a sidecar file", Description: "Survives restarts without a schema change"},
		}, Custom: true},
		{Header: "Tests", Text: "Which tests should I add?", Options: []QuestionOption{
			{Label: "Unit", Description: "Cursor arithmetic in isolation"},
			{Label: "Integration", Description: "A reconnect against a real store"},
		}, Multi: true},
	}}})
	var answers []Answer
	select {
	case <-ctx.Done():
		return false
	case answers = <-ch:
	}
	f.emit(QuestionResolved{ID: id, Via: "cli"})
	var picked []string
	for _, a := range answers {
		picked = append(picked, a.Selected...)
		if a.Custom != "" {
			picked = append(picked, a.Custom)
		}
	}
	return f.stream(ctx, f.nextID("m"), "Going with: "+strings.Join(picked, ", ")+".", false)
}

func (f *Fake) todoTurn(ctx context.Context) bool {
	items := []Todo{{Text: "Read the session store"}, {Text: "Split out the cursor logic"}, {Text: "Add tests for reconnects"}, {Text: "Run the suite"}}
	for i := range items {
		items[i].Status = TodoActive
		f.emit(TodosChanged{Items: append([]Todo(nil), items...)})
		if _, ok := f.tool(ctx, "Read", map[string]any{"path": f.info.Cwd + fmt.Sprintf("/internal/store/part%d.go", i+1)}, "package store\n", false, ""); !ok {
			return false
		}
		items[i].Status = TodoDone
	}
	f.emit(TodosChanged{Items: items})
	return f.stream(ctx, f.nextID("m"), "Done: all four steps are complete.", false)
}

func (f *Fake) agentTurn(ctx context.Context) bool {
	parent := f.nextID("t")
	f.emit(ToolStarted{ID: parent, Name: "SubAgent", Input: map[string]any{"description": "Survey the transport package"}, At: f.now()})
	f.emit(SubagentChanged{ID: parent, Name: "Survey the transport package", State: "running"})
	for _, p := range []string{"stream.go", "retry.go", "client.go"} {
		if _, ok := f.tool(ctx, "Read", map[string]any{"path": f.info.Cwd + "/internal/transport/" + p}, "package transport\n", false, parent); !ok {
			return false
		}
	}
	f.emit(SubagentChanged{ID: parent, Name: "Survey the transport package", State: "done"})
	f.emit(ToolFinished{ID: parent, Output: "The transport reads SSE with a cursor and retries with backoff.", At: f.now()})
	return f.stream(ctx, f.nextID("m"), "The subagent's survey: the transport reads SSE with a cursor and retries with backoff capped at 15 s.", false)
}

func (f *Fake) dropTurn(ctx context.Context) bool {
	id := f.nextID("m")
	if !f.stream(ctx, id+"a", "Reading the stream code first. ", false) {
		return false
	}
	for attempt := 1; attempt <= 3; attempt++ {
		f.emit(ConnectionChanged{State: Reconnecting, Attempt: attempt, RetryIn: time.Duration(attempt) * time.Second, Err: "connection reset by peer"})
		if !f.sleep(ctx, 25) {
			return false
		}
	}
	f.emit(ConnectionChanged{State: Online})
	return f.stream(ctx, id+"b", "Back after the drop: nothing was lost, the server held the turn while this terminal was away.", false)
}

func longOutput() string {
	var b strings.Builder
	for i := 1; i <= 37; i++ {
		fmt.Fprintf(&b, "ok  \tgithub.com/example/app/internal/pkg%02d\t0.%03ds\n", i, 100+i*7)
	}
	return b.String()
}

const sampleAnswer = "The store keeps **one cursor per session** and replays from it after a reconnect.\n\n" +
	"## What happens on a drop\n\n" +
	"1. The stream breaks and the reader backs off, starting at one second.\n" +
	"2. On reconnect it sends `Last-Event-ID` with the cursor.\n" +
	"3. The server replays everything after it, or answers `resync_required`.\n\n" +
	"```go\nfunc (c *Cursor) Next(after int) int {\n\tif after < 0 {\n\t\treturn 0\n\t}\n\treturn after + 1\n}\n```\n\n" +
	"| Case | Result |\n|---|---|\n| cursor behind | replay |\n| cursor ahead | resync |\n\n" +
	"The bug you saw is the *ahead* case: a negative cursor replays from zero."

const sampleBefore = "package store\n\n// Next is the cursor after the given event.\nfunc (c *Cursor) Next(after int) int {\n\tif after < 0 {\n\t\treturn 0\n\t}\n\treturn after + 1\n}\n"

const sampleAfter = "package store\n\n// Next is the cursor after the given event.\nfunc (c *Cursor) Next(after int) int {\n\tif after < 0 || after > c.head {\n\t\treturn c.head\n\t}\n\treturn after + 1\n}\n"

func sampleHistory(now time.Time) []Event {
	return []Event{
		UserMessage{ID: "h1", Text: "Read the session store and tell me what it does", At: now.Add(-20 * time.Minute)},
		ToolStarted{ID: "h2", Name: "Read", Input: map[string]any{"path": "internal/store/store.go"}, At: now.Add(-20 * time.Minute)},
		ToolFinished{ID: "h2", Output: strings.Repeat("line\n", 182), At: now.Add(-20 * time.Minute)},
		TextDelta{MessageID: "h3", Text: "It keeps one cursor per session and replays from it after a reconnect."},
		MessageDone{MessageID: "h3"},
		UserMessage{ID: "h4", Text: "Split the cursor logic out of the store", Origin: "app", At: now.Add(-12 * time.Minute)},
		TextDelta{MessageID: "h5", Text: "Done — the cursor now lives in `cursor.go`."},
		MessageDone{MessageID: "h5"},
	}
}
