package tui

import (
	"context"

	tea "charm.land/bubbletea/v2"
	"strings"
	"testing"
	"time"
)

func TestEnterSendsThePromptAndDrawsItOnce(t *testing.T) {
	h := newHarness(t, 100, 60)
	h.typeText("explain the store")
	h.key("enter")
	sent := h.fake.Sent()
	if len(sent) != 1 || sent[0].Text != "explain the store" || sent[0].ClientID == "" {
		t.Fatalf("sent %+v", sent)
	}
	h.pumpTurn()
	if n := strings.Count(h.plain(), "> explain the store"); n != 1 {
		t.Fatalf("the prompt is drawn %d times:\n%s", n, h.plain())
	}
	h.contains("one cursor per session")
	h.contains("Read(internal/store/store.go)")
	if h.m.running {
		t.Fatal("the turn did not end")
	}
}

func TestEscInterruptsARunningTurn(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.fake.Delay = 20 * time.Millisecond
	h.typeText("explain")
	h.key("enter")
	h.pumpTo(ToolStarted{})
	h.key("esc")
	h.pumpTurn()
	h.contains("Interrupted · What should Daedalus do instead?")
}

func TestCtrlCClearsThenExitsOnSecondPress(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("draft")
	h.key("ctrl+c")
	if !h.m.editor.Empty() {
		t.Fatal("ctrl+c kept the draft")
	}
	h.key("up")
	if h.m.editor.Value() != "draft" {
		t.Fatalf("up after ctrl+c brought back %q", h.m.editor.Value())
	}
	h.key("ctrl+c", "ctrl+c")
	h.contains("Press Ctrl-C again to exit")
	h.key("ctrl+c")
	if !h.m.quitting {
		t.Fatal("the second ctrl+c did not exit")
	}
}

func TestCtrlDTwiceExitsOnlyOnAnEmptyPrompt(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("ab")
	h.key("ctrl+d", "ctrl+d")
	if h.m.quitting {
		t.Fatal("ctrl+d on a non-empty prompt exited")
	}
	h.key("ctrl+a", "ctrl+k")
	h.key("ctrl+d")
	h.clock.advance(time.Second)
	h.key("ctrl+d")
	if h.m.quitting {
		t.Fatal("two ctrl+d a second apart exited")
	}
	h.key("ctrl+d")
	if !h.m.quitting {
		t.Fatal("a double ctrl+d did not exit")
	}
}

func TestShiftTabCyclesTheLocalGuard(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.contains("⏵⏵ auto-accept on")
	h.key("shift+tab")
	h.contains("⏸ plan mode on")
	if h.fake.Guard() != GuardPlan {
		t.Fatalf("the executor guard is %v", h.fake.Guard())
	}
	h.key("shift+tab")
	h.contains("⏸ ask before acting")
	h.key("shift+tab")
	h.contains("auto-accept on")
	if h.m.store.LoadPrefs().Guard != "auto" {
		t.Fatal("the guard was not remembered")
	}
}

func TestPermissionChoicesMapToDecisions(t *testing.T) {
	for _, tc := range []struct {
		keys []string
		want string
	}{
		{[]string{"1"}, "All 19 tests pass."},
		{[]string{"2"}, "All 19 tests pass."},
		{[]string{"3"}, "The operator refused this command."},
	} {
		h := newHarness(t, 100, 36)
		h.typeText("run the tests")
		h.key("enter")
		h.pumpTo(PermissionRequested{})
		h.contains("Do you want to proceed?")
		h.contains("2. Yes, and don't ask again for npm test *")
		h.key(tc.keys...)
		if tc.keys[0] == "3" {
			h.contains("Tell Daedalus what to do instead")
			h.typeText("use make test")
			h.key("enter")
		}
		h.pumpTurn()
		h.contains(tc.want)
		if tc.keys[0] == "3" {
			h.contains("Understood — use make test")
		}
	}
}

func TestEscOnAPermissionRefuses(t *testing.T) {
	h := newHarness(t, 100, 36)
	h.typeText("run the tests")
	h.key("enter")
	h.pumpTo(PermissionRequested{})
	h.key("esc")
	h.pumpTurn()
	h.contains("Error: The operator refused this command.")
}

func TestQuestionAnswersReachTheBackend(t *testing.T) {
	h := newHarness(t, 100, 40)
	h.typeText("ask me")
	h.key("enter")
	h.pumpTo(QuestionAsked{})
	h.key("2")                               // In memory, then on to the second question
	h.key("space", "down", "space", "enter") // both tests
	h.contains("Review your answers")
	h.key("enter")
	h.pumpTurn()
	h.contains("Going with: In memory, Unit, Integration.")
}

func TestQuestionTypedAnswer(t *testing.T) {
	h := newHarness(t, 100, 40)
	h.typeText("ask me")
	h.key("enter")
	h.pumpTo(QuestionAsked{})
	h.key("4")
	h.typeText("in redis")
	h.key("enter")
	h.key("enter", "enter")
	h.pumpTurn()
	h.contains("in redis")
}

func TestPromptsTypedWhileWorkingAreQueuedAndTakenBack(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.fake.Delay = 30 * time.Millisecond
	h.typeText("explain")
	h.key("enter")
	h.pumpTo(ToolStarted{})
	h.typeText("also check retry.go")
	h.key("enter")
	h.pumpTo(QueueChanged{})
	h.contains("> also check retry.go")
	h.key("up")
	if h.m.editor.Value() != "also check retry.go" {
		t.Fatalf("up took back %q", h.m.editor.Value())
	}
	h.pumpTurn()
}

func TestTabQueuesAFollowUpWhileWorking(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.fake.Delay = 30 * time.Millisecond
	h.typeText("explain")
	h.key("enter")
	h.pumpTo(ToolStarted{})
	h.typeText("then summarise")
	h.key("tab")
	sent := h.fake.Sent()
	if last := sent[len(sent)-1]; last.Mode != SendQueue || last.Text != "then summarise" {
		t.Fatalf("tab sent %+v", last)
	}
	h.pumpTurn()
}

func TestShellModeRunsLocallyAndFeedsTheNextPrompt(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("!")
	h.contains("! for shell mode")
	h.typeText("ls")
	h.key("enter")
	h.contains("! ls")
	h.contains("README.md")
	h.typeText("what is in here?")
	h.key("enter")
	sent := h.fake.Sent()
	if !strings.Contains(sent[0].Text, "<bash-input>ls</bash-input>") || !strings.HasSuffix(sent[0].Text, "what is in here?") {
		t.Fatalf("the shell output did not go with the prompt: %q", sent[0].Text)
	}
	h.pumpTurn()
}

func TestLongPasteCollapsesAndExpandsOnSend(t *testing.T) {
	h := newHarness(t, 100, 30)
	long := strings.Repeat("a line of a log\n", 40)
	h.send(pasteMsg(long))
	h.contains("[Pasted text #1 +40 lines]")
	h.key("enter")
	if sent := h.fake.Sent(); len(sent) != 1 || !strings.Contains(sent[0].Text, "a line of a log") || strings.Contains(sent[0].Text, "Pasted text") {
		t.Fatalf("sent %+v", sent)
	}
	h.pumpTurn()
}

func TestPastedImageGoesWithThePrompt(t *testing.T) {
	h := newHarness(t, 100, 30, func(o *Options) {
		o.Clipboard = func(ctx context.Context) (Image, error) {
			return Image{Name: "shot.png", MIME: "image/png", Data: []byte("png")}, nil
		}
	})
	h.key("ctrl+v")
	h.contains("[Image #1]")
	h.typeText(" what is this")
	h.key("enter")
	sent := h.fake.Sent()
	if len(sent) != 1 || len(sent[0].Images) != 1 || sent[0].Images[0].Name != "shot.png" {
		t.Fatalf("sent %+v", sent)
	}
	if h.m.byID[sent[0].ClientID] == nil || !strings.Contains(drawUser(h.m.renderContext(100, false), h.m.byID[sent[0].ClientID])[0], "[1 image]") {
		t.Fatal("the prompt does not show its image")
	}
	h.pumpTurn()
}

func TestSlashPaletteRunsABuiltinOnEnter(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/sta")
	h.contains("/status")
	h.key("enter")
	h.contains("Status")
	h.contains("http://127.0.0.1:8765")
	h.key("esc")
	h.lacks("esc to close")
}

func TestSlashPaletteListsServerCommandsAndSkills(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.run(h.m.loadCommands(), 0)
	h.typeText("/re")
	h.contains("/review")
	h.contains("(skill)")
	h.contains("/resume")
	h.key("esc")
	h.key("ctrl+u")
	h.typeText("/nosuch")
	h.contains("No commands match")
}

func TestReverseSearchFindsAnEarlierPrompt(t *testing.T) {
	h := newHarness(t, 100, 30, func(o *Options) {
		o.Store = &MemoryStore{Entries: map[string][]string{"/work/project": {"fix the cursor", "run the tests", "explain"}}}
	})
	h.key("ctrl+r")
	h.typeText("cur")
	h.contains("(reverse-i-search)`cur': fix the cursor")
	h.key("tab")
	if h.m.editor.Value() != "fix the cursor" {
		t.Fatalf("search took %q", h.m.editor.Value())
	}
}

func TestHistoryWalksPromptsOfThisFolder(t *testing.T) {
	h := newHarness(t, 100, 30, func(o *Options) {
		o.Store = &MemoryStore{Entries: map[string][]string{"/work/project": {"first", "second"}}}
	})
	h.typeText("draft")
	h.key("up")
	if h.m.editor.Value() != "second" {
		t.Fatalf("up gave %q", h.m.editor.Value())
	}
	h.key("up")
	if h.m.editor.Value() != "first" {
		t.Fatalf("up gave %q", h.m.editor.Value())
	}
	h.key("down", "down")
	if h.m.editor.Value() != "draft" {
		t.Fatalf("down did not return to the draft: %q", h.m.editor.Value())
	}
}

func TestMentionCompletesAPathFromTheFolder(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"main.go", "internal/store/cursor.go", "README.md"} {
		writeFile(t, dir, f)
	}
	h := newHarness(t, 100, 30, func(o *Options) { o.Cwd = dir })
	h.m.session.Cwd = dir
	h.typeText("look at @curs")
	h.contains("internal/store/cursor.go")
	h.key("tab")
	if got := h.m.editor.Value(); got != "look at @internal/store/cursor.go " {
		t.Fatalf("completion gave %q", got)
	}
}

func TestModelPickerChangesModelAndEffort(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/model")
	h.key("enter")
	h.contains("Select model")
	h.key("up", "right", "enter")
	h.pumpTo(ModelChanged{})
	if h.m.session.Model != "claude-opus" {
		t.Fatalf("model is %q", h.m.session.Model)
	}
	h.contains("claude-opus")
}

func TestRewindRestoresAndPutsThePromptBack(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.key("esc", "esc")
	h.contains("Rewind")
	h.key("down", "enter") // the prompt before the newest, which has a code snapshot
	h.key("enter")         // restore code and conversation
	h.pumpTo(Notice{})
	h.contains("Restored code and conversation to the checkpoint.")
	if h.m.editor.Value() != "Split the cursor logic out of the store" {
		t.Fatalf("the prompt did not come back: %q", h.m.editor.Value())
	}
}

func TestConfigTogglesAreSaved(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/config")
	h.key("enter")
	h.key("down", "enter") // editor mode
	h.run(h.m.savePrefs(), 0)
	if !h.m.store.LoadPrefs().Vim || !h.m.editor.Vim() {
		t.Fatal("vim mode was not switched on and saved")
	}
	h.key("down", "down", "enter") // show thinking
	if !h.m.prefs.ShowThinking {
		t.Fatal("show thinking did not switch")
	}
}

func TestResumeLoadsTheChosenSession(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("/resume")
	h.key("enter")
	h.typeText("refactor")
	h.key("enter")
	h.contains("Split the cursor logic out of the store")
	h.contains("from app")
}

func TestScrollingShowsJumpToBottomAndCountsNewMessages(t *testing.T) {
	h := newHarness(t, 80, 16)
	for i := 0; i < 12; i++ {
		h.apply(Notice{Text: "notice " + itoa(i)})
	}
	h.key("pgup")
	h.contains("Jump to bottom")
	h.apply(Notice{Text: "late notice"})
	h.contains("1 new message")
	h.key("ctrl+end")
	h.lacks("Jump to bottom")
	h.contains("late notice")
}

func TestReconnectAfterADropResumesTheTurn(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.typeText("what if it drops")
	h.key("enter")
	h.pumpUntil(func(e Event) bool {
		c, ok := e.(ConnectionChanged)
		return ok && c.State == Reconnecting
	})
	h.contains("Reconnecting… turn paused")
	h.contains("reconnecting")
	h.pumpTurn()
	h.contains("Back after the drop")
	h.lacks("turn paused")
}

func TestHistoryResetReplacesTheTranscript(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.apply(UserMessage{ID: "x", Text: "old prompt"})
	h.apply(HistoryReset{History: []Event{UserMessage{ID: "y", Text: "new prompt"}}})
	h.lacks("old prompt")
	h.contains("new prompt")
}

func TestPromptFromTheAppIsLabelled(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.apply(UserMessage{ID: "a1", Text: "typed on the phone", Origin: "app"})
	h.contains("typed on the phone")
	h.contains("from app")
}

func TestDragSelectsAndCopiesTranscriptText(t *testing.T) {
	h := newHarness(t, 100, 30)
	h.apply(Notice{Text: "copy this line please"})
	lines := strings.Split(h.plain(), "\n")
	row := -1
	for i, line := range lines {
		if strings.Contains(line, "copy this line please") {
			row = i
		}
	}
	if row < 0 {
		t.Fatal("the notice is not on screen")
	}
	col := strings.Index(lines[row], "copy")
	col = len([]rune(lines[row][:col]))
	h.send(tea.MouseClickMsg{X: col, Y: row, Button: tea.MouseLeft})
	h.send(tea.MouseMotionMsg{X: col + 13, Y: row, Button: tea.MouseLeft})
	if !strings.Contains(h.frame(), "copy this line") {
		t.Fatal("the selection hid the text")
	}
	text := selectedText(h.m.transcriptWindow(h.m.transcriptHeight()), h.m.sel, 100)
	if text != "copy this line" {
		t.Fatalf("selected %q", text)
	}
	h.send(tea.MouseReleaseMsg{X: col + 13, Y: row, Button: tea.MouseLeft})
	h.contains("Copied 14 characters")
}
