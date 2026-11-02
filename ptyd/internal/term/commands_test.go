//go:build unix

package term

import (
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/emulator"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/fake"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/internal/scan"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// markAt applies a shell mark as the emulator goroutine does, with the cursor where the test says
// the shell printed it. A real shell's timing plays no part: these tests are about the bookkeeping.
func markAt(t *testing.T, term *Terminal, emu *fake.Emulator, m scan.Mark, x int, row, seq int64) {
	t.Helper()
	emu.SetCursor(emulator.Cursor{X: x, AbsRow: row, Visible: true})
	if m.Params == nil {
		m.Params = map[string]string{}
	}
	if _, ok := m.Params["k"]; !ok {
		m.Params["k"] = testNonce
	}
	err := term.WithEmulator(func(e emulator.Emulator) {
		if term.verifiedMark(m) {
			term.onShellMark(e, m, seq)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
}

func prompt(letter byte) scan.Mark { return scan.Mark{Kind: scan.KindPrompt, Letter: letter} }

func ended(code int) scan.Mark {
	return scan.Mark{Kind: scan.KindPrompt, Letter: 'D', HasExit: true, Exit: code}
}

func line(text string) scan.Mark { return scan.Mark{Kind: scan.KindVscode, Letter: 'E', Text: text} }

func TestCommandRecordsFollowTheMarks(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c1", "cat")
	emu := <-h.fakes

	if term.CommandsKnown() {
		t.Fatal("a terminal whose program printed nothing and loaded no integration knows no commands")
	}
	markAt(t, term, emu, prompt('A'), 0, 3, 10)
	markAt(t, term, emu, line("make test"), 9, 3, 20)
	markAt(t, term, emu, prompt('C'), 0, 4, 30)
	if busy, ok := term.busyLocked(); !ok || !busy {
		t.Fatalf("busy after C: %v %v", busy, ok)
	}
	if !term.Info().Busy {
		t.Fatal("Info.Busy after C")
	}
	markAt(t, term, emu, ended(2), 0, 9, 40)
	if term.Info().Busy {
		t.Fatal("Info.Busy after D")
	}
	recs, err := term.Commands(CommandsOptions{Last: 20})
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1 {
		t.Fatalf("records %+v", recs)
	}
	r := recs[0]
	if r.N != 1 || r.Command != "make test" || r.ExitCode == nil || *r.ExitCode != 2 || r.Cwd != h.dir ||
		r.PromptRow == nil || *r.PromptRow != 3 || r.OutputRow != 4 || r.EndRow == nil || *r.EndRow != 9 ||
		r.StartSeq != 30 || r.EndSeq == nil || *r.EndSeq != 40 || r.FinishedAt == nil || r.DurationMs == nil {
		t.Fatalf("record %+v", r)
	}
	ev, ok := h.rec.find("c1", "terminal.command")
	if !ok {
		t.Fatal("no terminal.command event")
	}
	data := ev.data.(map[string]any)
	if data["phase"] != "end" || *(data["exit_code"].(*int)) != 2 || data["command"] != "make test" || data["seq"] != int64(40) {
		t.Fatalf("event %+v", data)
	}
	if lc := term.Info().LastCommand; lc == nil || lc.Command != "make test" || *lc.ExitCode != 2 {
		t.Fatalf("last command %+v", lc)
	}
	if !term.CommandsKnown() {
		t.Fatal("known after a verified mark")
	}
}

func TestMarksThatProveNothingAreIgnored(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c2", "cat")
	emu := <-h.fakes

	// A D with no command open: the prompt after an empty line, or the first prompt.
	markAt(t, term, emu, ended(0), 0, 1, 5)
	// Marks without the nonce, or with another launch's.
	for _, k := range []string{"", "0f1e2d3c4b5a69788796a5b4c3d2e1f1", testNonce + "0"} {
		m := prompt('C')
		m.Params = map[string]string{"k": k}
		markAt(t, term, emu, m, 0, 2, 6)
	}
	unmarked := prompt('C')
	unmarked.Params = map[string]string{"aid": "7"}
	unmarked.Params["k"] = ""
	markAt(t, term, emu, unmarked, 0, 2, 7)
	if recs, _ := term.Commands(CommandsOptions{Last: 20}); len(recs) != 0 {
		t.Fatalf("records from marks that prove nothing: %+v", recs)
	}
	if _, ok := h.rec.find("c2", "terminal.command"); ok {
		t.Fatal("an event from marks that prove nothing")
	}

	// A terminal without a nonce believes no mark at all.
	term.cmds.nonce = ""
	if term.verifiedMark(scan.Mark{Kind: scan.KindPrompt, Letter: 'C', Params: map[string]string{"k": ""}}) {
		t.Fatal("an empty nonce matched")
	}
}

func TestACommandWithoutItsEndIsClosed(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c3", "cat")
	emu := <-h.fakes

	markAt(t, term, emu, prompt('C'), 0, 1, 1)
	markAt(t, term, emu, prompt('C'), 0, 5, 2) // a second start: the first ended unreported
	markAt(t, term, emu, prompt('A'), 0, 7, 3) // a prompt: so did the second
	// Output left without a final newline: its last row is part of it.
	markAt(t, term, emu, prompt('C'), 0, 8, 4)
	markAt(t, term, emu, ended(0), 5, 8, 5)
	recs, _ := term.Commands(CommandsOptions{Last: 20})
	if len(recs) != 3 {
		t.Fatalf("records %+v", recs)
	}
	if recs[0].ExitCode != nil || *recs[0].EndRow != 5 || recs[1].ExitCode != nil || *recs[1].EndRow != 7 {
		t.Fatalf("closed without a status: %+v %+v", recs[0], recs[1])
	}
	if *recs[2].EndRow != 9 || *recs[2].ExitCode != 0 {
		t.Fatalf("a mid-line end: %+v", recs[2])
	}
}

func TestCommandBounds(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c4", "cat")
	emu := <-h.fakes

	long := strings.Repeat("é", maxCommandLine) // two bytes each: the cut falls inside one
	markAt(t, term, emu, line(long), 0, 0, 1)
	markAt(t, term, emu, prompt('C'), 0, 1, 2)
	markAt(t, term, emu, ended(0), 0, 2, 3)
	recs, _ := term.Commands(CommandsOptions{Last: 1})
	if got := recs[0].Command; len(got) > maxCommandLine || !strings.HasPrefix(long, got) || len(got) < maxCommandLine-1 {
		t.Fatalf("a long command line kept %d bytes", len(got))
	}

	for i := 0; i < maxCommandRecords+20; i++ {
		markAt(t, term, emu, prompt('C'), 0, int64(3+i), int64(10+2*i))
		markAt(t, term, emu, ended(0), 0, int64(4+i), int64(11+2*i))
	}
	recs, _ = term.Commands(CommandsOptions{Last: maxCommandRecords})
	if len(recs) != maxCommandRecords || recs[len(recs)-1].N != int64(maxCommandRecords+21) {
		t.Fatalf("%d records kept, the newest %d", len(recs), recs[len(recs)-1].N)
	}
	// The answer's budget drops the oldest.
	recs, _ = term.Commands(CommandsOptions{Last: maxCommandRecords, Budget: 10 * 256})
	if len(recs) == 0 || len(recs) > 10 || recs[len(recs)-1].N != int64(maxCommandRecords+21) {
		t.Fatalf("budgeted: %d records", len(recs))
	}
}

func TestCommandOutputIsReadFromItsRows(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c5", "cat")
	emu := <-h.fakes
	emu.SetText([]string{"$ make", "compiling", "ok", "", "$ tail", "a", "b", "c"})

	markAt(t, term, emu, prompt('C'), 0, 1, 1)
	markAt(t, term, emu, ended(0), 0, 4, 2) // rows 1..3: "compiling", "ok", ""
	markAt(t, term, emu, prompt('C'), 0, 5, 3)
	emu.SetCursor(emulator.Cursor{AbsRow: 7}) // still running: its output so far
	recs, err := term.Commands(CommandsOptions{Last: 5, WithOutput: true, OutputMax: 3})
	if err != nil {
		t.Fatal(err)
	}
	if *recs[0].Output != "ok" || !recs[0].OutputTruncated {
		t.Fatalf("the first output, cut to its end: %q %v", *recs[0].Output, recs[0].OutputTruncated)
	}
	if *recs[1].Output != "b\nc" || !recs[1].OutputTruncated || recs[1].EndRow != nil {
		t.Fatalf("the running command's output: %q %+v", *recs[1].Output, recs[1])
	}
	recs, _ = term.Commands(CommandsOptions{Last: 5, WithOutput: true, OutputMax: 1000})
	if *recs[0].Output != "compiling\nok" || recs[0].OutputTruncated {
		t.Fatalf("whole output: %q", *recs[0].Output)
	}
}

func TestWaitForACommandToEnd(t *testing.T) {
	h := newHarness(t)
	term := h.start(t, "c6", "cat")
	emu := <-h.fakes

	markAt(t, term, emu, prompt('C'), 0, 1, 5)
	markAt(t, term, emu, ended(3), 0, 2, 6)
	// Ended before the offset the wait starts from: not the one waited for.
	res := make(chan WaitResult, 1)
	go func() {
		r, _ := term.WaitFor(t.Context(), WaitSpec{CommandDone: true, SinceSeq: 6, Timeout: 10 * time.Second})
		res <- r
	}()
	time.Sleep(100 * time.Millisecond)
	select {
	case r := <-res:
		t.Fatalf("the wait ended on an earlier command: %+v", r)
	default:
	}
	markAt(t, term, emu, prompt('C'), 0, 3, 7)
	markAt(t, term, emu, ended(0), 0, 4, 8)
	r := <-res
	if r.Matched != "command_done" || r.Seq != 8 || r.Command == nil || *r.Command.ExitCode != 0 {
		t.Fatalf("wait %+v", r)
	}
	// Given the offset before the first command, the first is the one.
	r, _ = term.WaitFor(t.Context(), WaitSpec{CommandDone: true, SinceSeq: 0, Timeout: time.Second})
	if r.Matched != "command_done" || *r.Command.ExitCode != 3 {
		t.Fatalf("wait from 0: %+v", r)
	}
}

// A client that receives a snapshot receives the marks it needs to redraw, as of the same point.
func TestSnapshotIsFollowedByTheMarks(t *testing.T) {
	h := newAttachHarness(t, nil, nil)
	env := procenv.BuildEnv(nil, nil, nil, "m1")
	path, _ := procenv.LookPath("cat", append(env, "PATH=/usr/bin:/bin"), "/")
	term, err := h.reg.Create(Spec{ID: "m1", Path: path, Argv: []string{"cat"}, Cwd: h.dir, Env: env, Cols: 80,
		Rows: 24, RingBytes: 1 << 20, Nonce: testNonce, Integration: "bash"})
	if err != nil {
		t.Fatal(err)
	}
	emu := <-h.fakes
	markAt(t, term, emu, prompt('A'), 0, 0, 1)
	markAt(t, term, emu, prompt('C'), 0, 1, 2)
	markAt(t, term, emu, ended(1), 0, 3, 3)
	markAt(t, term, emu, prompt('A'), 0, 3, 4)
	_, s := attachClient(t, term, ClientOptions{Label: "laptop"}, wire.Attach{})
	marks := s.waitEvent(t, "marks", nil)
	list := marks["list"].([]any)
	if len(list) != 1 || marks["prompt_row"] != float64(3) {
		t.Fatalf("marks %+v", marks)
	}
	first := list[0].(map[string]any)
	if first["exit_code"] != float64(1) || first["output_row"] != float64(1) || first["end_row"] != float64(3) {
		t.Fatalf("mark %+v", first)
	}
	// The command's start and end reach an attached client as they happen.
	markAt(t, term, emu, prompt('C'), 0, 4, 5)
	s.waitEvent(t, "command", func(m map[string]any) bool {
		return m["phase"] == "start" && m["n"] == float64(2) && m["seq"] == float64(5)
	})
	markAt(t, term, emu, ended(7), 0, 6, 6)
	end := s.waitEvent(t, "command", func(m map[string]any) bool { return m["phase"] == "end" })
	if end["n"] != float64(2) || end["exit_code"] != float64(7) || end["abs_row"] != float64(4) ||
		end["end_row"] != float64(6) || end["prompt_row"] != float64(3) || end["seq"] != float64(6) {
		t.Fatalf("end %+v", end)
	}
	markAt(t, term, emu, prompt('A'), 0, 6, 7)
	next := s.waitEvent(t, "command", func(m map[string]any) bool { return m["phase"] == "prompt" })
	if next["abs_row"] != float64(6) || next["seq"] != float64(7) {
		t.Fatalf("prompt %+v", next)
	}
}
