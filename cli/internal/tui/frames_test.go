package tui

import (
	"strings"
	"testing"
	"time"
)

// The golden frames pin what each key screen looks like at a fixed size. They are compared as plain
// text (the colours are covered by the theme's own tests and by the captures); a deliberate change
// to a screen is recorded with `go test ./internal/tui -run Frame -update`.

func startTurn(h *harness, prompt string) {
	h.apply(
		UserMessage{ID: "u1", Text: prompt, At: h.clock.at},
		TurnStarted{RunID: "r1", At: h.clock.at},
	)
}

func TestFrameIdle(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.golden("idle")
}

func TestFrameWorkingTurn(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "Why do reconnects replay duplicate events?")
	h.apply(
		ThinkingDelta{MessageID: "m1", Text: "Look at the cursor first."},
		MessageDone{MessageID: "m1"},
		ToolStarted{ID: "t1", Name: "Read", Input: map[string]any{"path": "/work/project/internal/store/cursor.go"}, At: h.clock.at},
		ToolFinished{ID: "t1", Output: strings.Repeat("x\n", 64)},
		ToolStarted{ID: "t2", Name: "Exec", Input: map[string]any{"command": "go test ./internal/store/..."}, At: h.clock.at},
		TextDelta{MessageID: "m2", Text: "The cursor is read **before** the replay starts, so"},
	)
	h.clock.advance(12 * time.Second)
	h.m.turnTokens = 1234
	h.m.frame = 4
	h.golden("working")
}

func TestFrameCollapsedOutputAndAnswer(t *testing.T) {
	h := newHarness(t, 100, 34)
	startTurn(h, "run the whole suite")
	var out strings.Builder
	for i := 1; i <= 20; i++ {
		out.WriteString("ok  \texample.com/app/pkg" + itoa(i) + "\t0.1s\n")
	}
	h.apply(
		ToolStarted{ID: "t1", Name: "Exec", Input: map[string]any{"command": "go test ./..."}},
		ToolFinished{ID: "t1", Output: out.String()},
		TextDelta{MessageID: "m1", Text: "All **20** packages pass.\n\n- fastest: `pkg1`\n- slowest: `pkg20`\n"},
		MessageDone{MessageID: "m1"},
		TurnEnded{RunID: "r1", Outcome: TurnCompleted},
	)
	h.golden("collapsed")
}

func TestFramePermissionBash(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "run the tests")
	h.apply(
		ToolStarted{ID: "t1", Name: "Exec", Input: map[string]any{"command": "npm test"}},
		PermissionRequested{Request: PermissionRequest{ID: "p1", Tool: "Exec", Text: "npm test -- --watch=false", Similar: "npm test *"}},
	)
	h.m.frame = 1
	h.golden("permission-bash")
}

func TestFramePermissionEditFromGuard(t *testing.T) {
	h := newHarness(t, 100, 36)
	startTurn(h, "fix it")
	h.apply(
		ToolStarted{ID: "t1", Name: "Edit", Input: map[string]any{"path": "/work/project/internal/store/cursor.go"}},
		PermissionRequested{Request: PermissionRequest{
			ID: "p1", Source: SourceLocal, Tool: "Edit", Similar: "edits in this folder",
			Change: &FileChange{Path: "/work/project/internal/store/cursor.go", Before: sampleBefore, After: sampleAfter},
		}},
	)
	h.golden("permission-edit")
}

func TestFrameDiffInTranscript(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "fix the cursor")
	h.apply(
		ToolStarted{ID: "t1", Name: "Edit", Input: map[string]any{"path": "/work/project/internal/store/cursor.go"}},
		FileChanged{ToolID: "t1", Change: FileChange{Path: "/work/project/internal/store/cursor.go", Before: sampleBefore, After: sampleAfter}},
		ToolFinished{ID: "t1", Output: "edited"},
		TurnEnded{Outcome: TurnCompleted},
	)
	h.golden("diff")
}

func TestFramePalette(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/co")
	h.golden("palette")
}

func TestFrameQuestion(t *testing.T) {
	h := newHarness(t, 100, 34)
	startTurn(h, "help me decide")
	h.apply(QuestionAsked{Question: Question{ID: "q1", Items: []QuestionItem{
		{Header: "Storage", Text: "Where should the cursor live?", Options: []QuestionOption{
			{Label: "In the store", Description: "One table, one transaction"},
			{Label: "In memory", Description: "Fastest, lost on restart"},
		}, Custom: true},
		{Header: "Tests", Text: "Which tests?", Options: []QuestionOption{{Label: "Unit"}, {Label: "Integration"}}, Multi: true},
	}}})
	h.golden("question")
}

func TestFrameReconnectingPausesTheTurn(t *testing.T) {
	h := newHarness(t, 100, 24)
	startTurn(h, "keep going")
	h.apply(
		TextDelta{MessageID: "m1", Text: "Reading the stream code first."},
		ConnectionChanged{State: Reconnecting, Attempt: 2, RetryIn: 4 * time.Second, Err: "connection reset"},
	)
	h.golden("reconnecting")
	h.apply(ConnectionChanged{State: Online})
	h.contains("Reconnected · turn resumed")
}

func TestFrameTodosUnderTheSpinner(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "do the plan")
	h.apply(TodosChanged{Items: []Todo{
		{Text: "Read the store", Status: TodoDone},
		{Text: "Split the cursor out", Status: TodoActive},
		{Text: "Add reconnect tests", Status: TodoPending},
	}})
	h.golden("todos")
}

func TestFrameTranscriptPager(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "run the suite")
	h.apply(
		ThinkingDelta{MessageID: "m0", Text: "The suite is long; run it whole."},
		ToolStarted{ID: "t1", Name: "Exec", Input: map[string]any{"command": "go test ./..."}, At: h.clock.at},
		ToolFinished{ID: "t1", Output: "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\n"},
		TurnEnded{Outcome: TurnCompleted},
	)
	h.key("ctrl+o")
	h.golden("pager")
	h.key("q")
	h.lacks("Transcript")
}

func TestFrameHelp(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("?")
	h.golden("help")
}

func TestFrameNarrowWindow(t *testing.T) {
	h := newHarness(t, 60, 20)
	startTurn(h, "a prompt long enough that it has to wrap inside a narrow window")
	h.apply(ToolStarted{ID: "t1", Name: "Exec", Input: map[string]any{"command": "go build ./... && go vet ./... && go test -race ./..."}})
	h.clock.advance(3 * time.Second)
	h.golden("narrow")
}

func TestFrameQueuedWhileWorking(t *testing.T) {
	h := newHarness(t, 100, 24)
	startTurn(h, "refactor the store")
	h.apply(QueueChanged{Items: []QueuedMessage{{ID: "q1", Text: "also rename the package"}}})
	h.golden("queued")
}

func TestFrameRichAnswerFlattened(t *testing.T) {
	h := newHarness(t, 100, 30)
	startTurn(h, "compare the options")
	h.apply(
		TextDelta{MessageID: "m1", Text: ":::card Bubble Tea\nurl: https://example.com/bubbletea\nbadge: v2.1.0\nA TUI framework.\n:::\n\n:::flow Request path\n- CLI\n- Server\n- Model\n:::\n"},
		MessageDone{MessageID: "m1"},
		TurnEnded{Outcome: TurnCompleted},
	)
	h.golden("rich")
}

func TestFrameRewindPicker(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.key("esc", "esc")
	h.golden("rewind")
	// The newest prompt has no code snapshot; the one before it has.
	h.key("down", "enter")
	h.contains("Restore code and conversation")
	h.contains("Never mind")
}

func TestFrameResumePicker(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/resume")
	h.key("enter")
	h.golden("resume")
}

func TestFrameLightAndPlainThemesKeepTheLayout(t *testing.T) {
	dark := newHarness(t, 100, 30)
	startTurn(dark, "x")
	for _, name := range []string{"light", "ansi"} {
		h := newHarness(t, 100, 30, func(o *Options) { o.Theme = name })
		startTurn(h, "x")
		if h.plain() != dark.plain() {
			t.Errorf("theme %s moves the layout:\n%s", name, h.plain())
		}
	}
	plain := newHarness(t, 100, 30, func(o *Options) { o.Plain = true })
	startTurn(plain, "x")
	if strings.Contains(plain.frame(), "\x1b[38") {
		t.Error("a plain terminal got colour")
	}
}
