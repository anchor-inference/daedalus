package tui

import (
	"context"
	"time"
)

// Backend is everything the terminal interface asks of the world outside it: the server session it
// shows, and the local executor that runs the agent's tools in the working folder.
//
// The interface is drawn at the level of what a person sees, not at the level of the wire. The server
// speaks REST plus two SSE streams with sequence cursors, resync frames and core event kinds; the
// backend folds all of that into the events below, so the screen never learns what a cursor or a
// `content_block_delta` is and can be driven, frame for frame, by the fake in fake.go. That split is
// what lets the golden-frame and pty tests run with no server at all.
//
// Every method may be called from the Bubble Tea update loop's commands, so none of them may block on
// the person: a request that needs an answer from them arrives as an event and is answered by a
// later call. Methods return once the server (or the executor) has accepted the request; what the
// request causes comes back through Events.
type Backend interface {
	// Events is the single stream the interface reads, for as long as the program runs. The backend
	// closes it when it shuts down. It carries the session's events and the connection's state.
	Events() <-chan Event

	// Start opens a session: a new one in the working folder, or an existing one to resume. The
	// returned snapshot carries the history to draw and whatever is still waiting for the person
	// (open permission requests, questions, the queue), and the events that follow continue from it.
	Start(ctx context.Context, request StartRequest) (Session, error)
	// Sessions lists recent sessions for the resume picker, newest first.
	Sessions(ctx context.Context, limit int) ([]SessionSummary, error)

	// Send delivers a prompt. While a turn runs the server steers it in by default; Prompt.Mode
	// asks for a queued follow-up instead.
	Send(ctx context.Context, prompt Prompt) error
	// Interrupt stops the running turn and keeps what it has done so far.
	Interrupt(ctx context.Context) error
	// Withdraw takes a queued message back before the agent reads it, returning its text so it can
	// go back into the composer.
	Withdraw(ctx context.Context, queuedID string) (string, error)

	// Decide answers a permission request, from the server's policy or the local guard alike.
	Decide(ctx context.Context, requestID string, decision Decision) error
	// Answer answers an AskUser question; one Answer per question item, in order.
	Answer(ctx context.Context, requestID string, answers []Answer) error

	// Commands lists the slash commands the server offers: its own commands and the skills.
	Commands(ctx context.Context) ([]Command, error)
	// RunCommand runs one of those server commands, the whole line including the leading slash.
	RunCommand(ctx context.Context, line string) error

	// Models lists the models the server offers, with their effort levels.
	Models(ctx context.Context) ([]ModelOption, error)
	// SetModel switches the session's model and effort; an empty effort keeps the current one.
	SetModel(ctx context.Context, model, effort string) error
	// Compact asks the server to summarise the history; focus is an optional instruction.
	Compact(ctx context.Context, focus string) error
	// Clear starts a fresh session in the same folder and returns it, as /clear does.
	Clear(ctx context.Context) (Session, error)

	// Checkpoints lists the points the session can be rewound to, oldest first: one per prompt.
	Checkpoints(ctx context.Context) ([]Checkpoint, error)
	// Rewind restores a checkpoint. Code is restored from the executor's local snapshots and the
	// conversation through the server; the scope says which of the two, or both.
	Rewind(ctx context.Context, checkpointID string, scope RewindScope) error

	// Shell runs a command typed in `!` mode, locally in the working folder, and returns its output.
	// The interface shows the output and hands it to the agent with the next prompt.
	Shell(ctx context.Context, command string) (ShellResult, error)

	// SetGuard changes the local guard the executor enforces. These are guards on this machine, not
	// agent modes: Ask confirms writes and commands here before running them, Auto leaves the
	// decision to the server's policy, Plan refuses writes.
	SetGuard(ctx context.Context, guard Guard) error

	// Status reports what /status shows: server, device, session, model and versions.
	Status(ctx context.Context) (Status, error)
	// Login starts pairing this device with the server. The challenge is shown at once; the
	// outcome arrives as a LoginChanged event.
	Login(ctx context.Context) (LoginChallenge, error)
}

// StartRequest says which session Start should open.
type StartRequest struct {
	// Resume names an existing session; empty starts a new one.
	Resume string
	// Continue resumes the most recent session in this folder, when Resume is empty.
	Continue bool
	// Prompt, when set, is sent as the first message once the session is open.
	Prompt string
}

// Session is a snapshot of an opened session: enough to draw it and to continue from its events.
type Session struct {
	Info SessionInfo
	// History is the conversation so far, as the same events a live turn produces, oldest first.
	History []Event
	// Running is true when a turn is in flight at the moment of the snapshot.
	Running bool
	// Pending holds what still waits for the person: PermissionRequested and QuestionAsked events.
	Pending []Event
	Queue   []QueuedMessage
	Usage   Usage
	Todos   []Todo
}

// SessionInfo names the session and where it works.
type SessionInfo struct {
	ID     string
	Title  string
	Kind   string
	Cwd    string
	Device string
	Model  string
	Effort string
	Server string
}

// SessionSummary is one row of the resume picker.
type SessionSummary struct {
	ID       string
	Title    string
	Kind     string
	Cwd      string
	Updated  time.Time
	Messages int
	Running  bool
}

// SendMode says what a prompt sent while the agent works should do.
type SendMode int

const (
	// SendDefault starts a turn when idle and steers the running one otherwise.
	SendDefault SendMode = iota
	// SendQueue holds the prompt as a follow-up that runs after the current turn ends.
	SendQueue
)

// Prompt is one message leaving this terminal.
type Prompt struct {
	Text   string
	Images []Image
	Mode   SendMode
	// ClientID makes a retried send idempotent on the server.
	ClientID string
}

// Image is a picture pasted at the prompt; the backend uploads it as an attachment.
type Image struct {
	Name string
	MIME string
	Data []byte
}

// QueuedMessage is a prompt the server holds until the agent reads it.
type QueuedMessage struct {
	ID   string
	Text string
}

// Decision is the answer to a permission request.
type Decision struct {
	Kind DecisionKind
	// Feedback is what the person typed after "No, and tell it what to do instead".
	Feedback string
}

// DecisionKind is one of the numbered choices of the permission prompt.
type DecisionKind int

const (
	Allow DecisionKind = iota
	// AllowSimilar allows this request and the ones like it, for the rest of the session.
	AllowSimilar
	Deny
)

// RequestSource says who is asking for permission.
type RequestSource int

const (
	// SourceServer is the server's policy, answered through its grant and refuse routes.
	SourceServer RequestSource = iota
	// SourceLocal is the local guard in Ask mode, answered on this machine.
	SourceLocal
)

// PermissionRequest is a tool call waiting for the person's yes or no.
type PermissionRequest struct {
	ID     string
	Source RequestSource
	// Kind is the server's request kind (policy, harness, handoff); a kind the interface does not
	// know is drawn as a plain question.
	Kind string
	// Tool is the tool's name, Text the command or the action it wants to perform.
	Tool string
	Text string
	// Similar describes what "don't ask again" would cover, e.g. "npm test *"; empty hides the choice.
	Similar string
	// Elevated marks a request about the machine itself, drawn with the warning accent.
	Elevated bool
	// Change, when set, is the file edit the request would make, drawn as a diff.
	Change *FileChange
}

// Question is an AskUser call: one or more items, each with options.
type Question struct {
	ID    string
	Items []QuestionItem
}

// QuestionItem is one question with its options.
type QuestionItem struct {
	Header  string
	Text    string
	Options []QuestionOption
	// Multi allows several options; Custom allows a typed answer besides the options.
	Multi  bool
	Custom bool
}

// QuestionOption is one selectable answer.
type QuestionOption struct {
	Label       string
	Description string
}

// Answer is the answer to one QuestionItem.
type Answer struct {
	Selected []string
	Custom   string
}

// CommandSource says where a slash command comes from, which decides how it runs.
type CommandSource int

const (
	CommandBuiltin CommandSource = iota
	CommandSkill
	CommandServer
)

// Command is one entry of the `/` palette.
type Command struct {
	Name        string
	Description string
	Source      CommandSource
	// Args is a hint for what follows the name, e.g. "[focus]".
	Args    string
	Aliases []string
}

// ModelOption is one model the server offers.
type ModelOption struct {
	ID            string
	Label         string
	Efforts       []string
	Default       bool
	ContextWindow int
}

// Checkpoint is a point the session can be rewound to.
type Checkpoint struct {
	ID     string
	Prompt string
	At     time.Time
	// Code is true when the executor holds a snapshot of the folder for it.
	Code bool
}

// RewindScope says what a rewind restores.
type RewindScope int

const (
	RewindBoth RewindScope = iota
	RewindConversation
	RewindCode
	// RewindSummarize keeps the code and replaces the conversation after the point with a summary.
	RewindSummarize
)

// ShellResult is what a `!` command printed and how it ended.
type ShellResult struct {
	Output   string
	ExitCode int
}

// Guard is the local guard the executor enforces, cycled with Shift+Tab.
type Guard int

const (
	GuardAsk Guard = iota
	GuardAuto
	GuardPlan
)

// Status is what /status lists, as labelled values in display order.
type Status struct {
	Items []StatusItem
}

// StatusItem is one labelled value of Status.
type StatusItem struct {
	Label string
	Value string
}

// LoginChallenge is what the person needs to approve this device in the app.
type LoginChallenge struct {
	Code      string
	URL       string
	ExpiresAt time.Time
}

// Usage is the token count of the session and how full its context is.
type Usage struct {
	InputTokens   int
	OutputTokens  int
	ContextTokens int
	ContextWindow int
	CostUSD       float64
}

// TodoStatus is where one todo item stands.
type TodoStatus int

const (
	TodoPending TodoStatus = iota
	TodoActive
	TodoDone
)

// Todo is one item of the agent's todo list.
type Todo struct {
	Text   string
	Status TodoStatus
}

// FileChange is one file the agent changed, before and after, for the inline diff.
type FileChange struct {
	Path   string
	Before string
	After  string
}

// Event is anything the backend reports. The concrete types below are the whole set; the interface
// ignores a type it does not know, so the backend may grow new ones first.
type Event interface{ isEvent() }

// ConnState is the state of the link to the server.
type ConnState int

const (
	Connecting ConnState = iota
	Online
	// Reconnecting is a dropped link being retried. A turn in flight is paused, not lost: the
	// server holds it, and the backend replays what was missed once the link is back.
	Reconnecting
	// Offline is a link the backend has stopped retrying until asked.
	Offline
	// Unauthorized is a link the server refused: the device needs /login.
	Unauthorized
)

// ConnectionChanged reports the link's state.
type ConnectionChanged struct {
	State   ConnState
	Attempt int
	// RetryIn is the wait before the next attempt, while Reconnecting.
	RetryIn time.Duration
	Err     string
}

// SessionUpdated reports a changed title, model or binding.
type SessionUpdated struct{ Info SessionInfo }

// HistoryReset replaces the whole transcript: after a compaction, a rewind, or a resync the backend
// could not replay.
type HistoryReset struct{ History []Event }

// UserMessage is a prompt in the conversation: typed here, in the app, or in Telegram.
type UserMessage struct {
	ID     string
	Text   string
	Images int
	// Origin names where it was typed when that was not this terminal ("app", "telegram").
	Origin string
	At     time.Time
}

// TurnStarted marks the agent beginning to work.
type TurnStarted struct {
	RunID string
	At    time.Time
}

// TextDelta is a piece of the answer as it streams.
type TextDelta struct {
	MessageID string
	Text      string
}

// ThinkingDelta is a piece of the model's reasoning as it streams.
type ThinkingDelta struct {
	MessageID string
	Text      string
}

// MessageDone closes a streamed message; its text is final.
type MessageDone struct{ MessageID string }

// ToolStarted is a tool call beginning.
type ToolStarted struct {
	ID    string
	Name  string
	Input map[string]any
	// Parent names the subagent that made the call; empty for the main agent.
	Parent string
	At     time.Time
}

// ToolProgress is output a running tool has produced so far.
type ToolProgress struct {
	ID   string
	Text string
}

// ToolFinished is a tool call's result.
type ToolFinished struct {
	ID      string
	Output  string
	IsError bool
	At      time.Time
}

// FileChanged reports a file the executor wrote for a tool call, for the inline diff.
type FileChanged struct {
	ToolID string
	Change FileChange
}

// PermissionRequested is a request waiting for the person.
type PermissionRequested struct{ Request PermissionRequest }

// PermissionResolved closes a request, answered here or elsewhere (the app, Telegram, expiry).
type PermissionResolved struct {
	ID       string
	Decision string
	Via      string
}

// QuestionAsked is an AskUser call waiting for the person.
type QuestionAsked struct{ Question Question }

// QuestionResolved closes a question, answered here or elsewhere.
type QuestionResolved struct {
	ID  string
	Via string
}

// TodosChanged replaces the todo list.
type TodosChanged struct{ Items []Todo }

// QueueChanged replaces the list of queued messages.
type QueueChanged struct{ Items []QueuedMessage }

// UsageChanged reports the token count and the context fill.
type UsageChanged struct{ Usage Usage }

// TurnOutcome is how a turn ended.
type TurnOutcome int

const (
	TurnCompleted TurnOutcome = iota
	TurnInterrupted
	TurnFailed
)

// TurnEnded marks the agent finishing.
type TurnEnded struct {
	RunID   string
	Outcome TurnOutcome
	Err     string
	At      time.Time
}

// NoticeLevel says how loudly a notice is drawn.
type NoticeLevel int

const (
	NoticeInfo NoticeLevel = iota
	NoticeWarning
	NoticeError
)

// Notice is a line from the server or the backend that belongs in the transcript.
type Notice struct {
	Level NoticeLevel
	Text  string
}

// CompactionChanged reports a compaction starting or finishing.
type CompactionChanged struct {
	Running bool
	Summary string
}

// SubagentChanged reports a subagent's state; its tool calls carry its ID as Parent.
type SubagentChanged struct {
	ID     string
	Name   string
	State  string
	Detail string
}

// ModelChanged reports the session's model and effort.
type ModelChanged struct {
	Model  string
	Effort string
}

// LoginState is where a pairing stands.
type LoginState int

const (
	LoginWaiting LoginState = iota
	LoginApproved
	LoginRefused
	LoginExpired
)

// LoginChanged reports the outcome of a pairing started by Login.
type LoginChanged struct {
	State  LoginState
	Device string
	Err    string
}

func (ConnectionChanged) isEvent()   {}
func (SessionUpdated) isEvent()      {}
func (HistoryReset) isEvent()        {}
func (UserMessage) isEvent()         {}
func (TurnStarted) isEvent()         {}
func (TextDelta) isEvent()           {}
func (ThinkingDelta) isEvent()       {}
func (MessageDone) isEvent()         {}
func (ToolStarted) isEvent()         {}
func (ToolProgress) isEvent()        {}
func (ToolFinished) isEvent()        {}
func (FileChanged) isEvent()         {}
func (PermissionRequested) isEvent() {}
func (PermissionResolved) isEvent()  {}
func (QuestionAsked) isEvent()       {}
func (QuestionResolved) isEvent()    {}
func (TodosChanged) isEvent()        {}
func (QueueChanged) isEvent()        {}
func (UsageChanged) isEvent()        {}
func (TurnEnded) isEvent()           {}
func (Notice) isEvent()              {}
func (CompactionChanged) isEvent()   {}
func (SubagentChanged) isEvent()     {}
func (ModelChanged) isEvent()        {}
func (LoginChanged) isEvent()        {}
