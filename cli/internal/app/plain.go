package app

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"

	"github.com/ascorblack/daedalus/cli/internal/guard"
	"github.com/ascorblack/daedalus/cli/internal/tui"
)

// Plain is the line-mode screen: one line in, the answer streamed out, approvals asked as a y/n
// line. It is what `--plain` runs, and what a terminal without cursor control (a pipe, a dumb
// terminal, an editor's shell) gets, since the full interface needs both.
type Plain struct {
	Backend *Backend
	In      io.Reader
	Out     io.Writer
}

// Run shows the session until /exit, end of input or a second Ctrl+C.
func (p *Plain) Run(ctx context.Context, req tui.StartRequest) error {
	b := p.Backend
	snap, err := b.Start(ctx, req)
	if err != nil {
		return err
	}
	where := snap.Info.Cwd
	if where == "" {
		where = b.opt.Cwd
	}
	fmt.Fprintf(p.Out, "✻ Daedalus · %s · %s\n  session %s · guard %s · /help for commands\n\n", snap.Info.Title, where, snap.Info.ID, b.guardName())
	for _, e := range lastTurns(snap.History, 6) {
		p.draw(e)
	}

	lines := make(chan string)
	go func() {
		defer close(lines)
		scanner := bufio.NewScanner(p.In)
		scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
		for scanner.Scan() {
			lines <- scanner.Text()
		}
	}()
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt)
	defer signal.Stop(interrupts)

	running := snap.Running
	var asking string
	var question *tui.Question
	for _, e := range snap.Pending {
		p.draw(e)
		if pr, ok := e.(tui.PermissionRequested); ok {
			asking = pr.Request.ID
		}
		if q, ok := e.(tui.QuestionAsked); ok {
			question = &q.Question
		}
	}
	p.prompt(running, asking != "" || question != nil)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-interrupts:
			if running {
				_ = b.Interrupt(ctx)
				fmt.Fprintln(p.Out, "\n  ⎿ interrupted")
				continue
			}
			return nil
		case ev, ok := <-b.Events():
			if !ok {
				return nil
			}
			switch e := ev.(type) {
			case tui.TurnStarted:
				running = true
			case tui.TurnEnded:
				running = false
				p.draw(e)
				p.prompt(running, false)
				continue
			case tui.PermissionRequested:
				asking = e.Request.ID
			case tui.PermissionResolved:
				if e.ID == asking {
					asking = ""
				}
			case tui.QuestionAsked:
				q := e.Question
				question = &q
			case tui.QuestionResolved:
				if question != nil && question.ID == e.ID {
					question = nil
				}
			}
			p.draw(ev)
			if asking != "" || question != nil {
				p.prompt(running, true)
			}
		case line, ok := <-lines:
			if !ok {
				return nil
			}
			line = strings.TrimSpace(line)
			switch {
			case asking != "":
				d := tui.Decision{Kind: tui.Deny}
				switch strings.ToLower(line) {
				case "y", "yes", "1":
					d.Kind = tui.Allow
				case "a", "always", "2":
					d.Kind = tui.AllowSimilar
				default:
					if l := strings.ToLower(line); l != "n" && l != "no" && l != "3" {
						d.Feedback = line
					}
				}
				id := asking
				asking = ""
				if err := b.Decide(ctx, id, d); err != nil {
					fmt.Fprintln(p.Out, "  ⎿ "+err.Error())
				}
			case question != nil:
				answers := make([]tui.Answer, len(question.Items))
				for i, item := range question.Items {
					answers[i] = pickAnswer(item, line)
				}
				q := question
				question = nil
				if err := b.Answer(ctx, q.ID, answers); err != nil {
					fmt.Fprintln(p.Out, "  ⎿ "+err.Error())
				}
			case line == "":
			case strings.HasPrefix(line, "/"):
				if done := p.command(ctx, line); done {
					return nil
				}
			case strings.HasPrefix(line, "!"):
				res, err := b.Shell(ctx, strings.TrimPrefix(line, "!"))
				if err != nil {
					fmt.Fprintln(p.Out, "  ⎿ "+err.Error())
				} else {
					fmt.Fprint(p.Out, res.Output)
				}
			default:
				if err := b.Send(ctx, tui.Prompt{Text: line}); err != nil {
					fmt.Fprintln(p.Out, "  ⎿ "+err.Error())
				}
				running = true
			}
			p.prompt(running, asking != "" || question != nil)
		}
	}
}

func pickAnswer(item tui.QuestionItem, line string) tui.Answer {
	for i, o := range item.Options {
		if line == fmt.Sprint(i+1) || strings.EqualFold(line, o.Label) {
			return tui.Answer{Selected: []string{o.Label}}
		}
	}
	return tui.Answer{Custom: line}
}

func (p *Plain) prompt(running, asking bool) {
	switch {
	case asking:
		fmt.Fprint(p.Out, "? ")
	case running:
	default:
		fmt.Fprint(p.Out, "\n> ")
	}
}

// command runs a line-mode command; true ends the session.
func (p *Plain) command(ctx context.Context, line string) bool {
	b := p.Backend
	name, arg, _ := strings.Cut(strings.TrimPrefix(line, "/"), " ")
	switch name {
	case "exit", "quit":
		return true
	case "help":
		fmt.Fprintln(p.Out, "  /guard [ask|auto|plan]  the local guard\n  /status                 where this terminal is connected\n  /stop                   interrupt the turn\n  /compact [focus]        summarise the history\n  /model <preset>         switch the model\n  /exit                   leave (the session stays on the server)\n  !<command>              run a command here and show its output\n  any other /command is the server's")
	case "guard":
		if arg == "" {
			fmt.Fprintln(p.Out, "  guard: "+b.guardName())
			return false
		}
		level, err := guard.ParseLevel(arg)
		if err != nil {
			fmt.Fprintln(p.Out, "  "+err.Error())
			return false
		}
		_ = b.SetGuard(ctx, TUIGuard(level))
		fmt.Fprintln(p.Out, "  guard: "+level.String())
	case "status":
		st, _ := b.Status(ctx)
		for _, item := range st.Items {
			fmt.Fprintf(p.Out, "  %-18s %s\n", item.Label, item.Value)
		}
	case "stop":
		_ = b.Interrupt(ctx)
	case "compact":
		if err := b.Compact(ctx, arg); err != nil {
			fmt.Fprintln(p.Out, "  "+err.Error())
		}
	case "model":
		if err := b.SetModel(ctx, arg, ""); err != nil {
			fmt.Fprintln(p.Out, "  "+err.Error())
		}
	default:
		if err := b.RunCommand(ctx, line); err != nil {
			fmt.Fprintln(p.Out, "  "+err.Error())
		}
	}
	return false
}

// draw prints one event as a line or a piece of one.
func (p *Plain) draw(ev tui.Event) {
	w := p.Out
	switch e := ev.(type) {
	case tui.UserMessage:
		fmt.Fprintf(w, "> %s\n", e.Text)
	case tui.TextDelta:
		fmt.Fprint(w, e.Text)
	case tui.MessageDone:
		fmt.Fprintln(w)
	case tui.ToolStarted:
		fmt.Fprintf(w, "● %s(%s)\n", e.Name, describeInput(e.Input))
	case tui.ToolFinished:
		mark := "⎿"
		if e.IsError {
			mark = "⎿ ✗"
		}
		fmt.Fprintf(w, "  %s %s\n", mark, firstLine(e.Output))
	case tui.PermissionRequested:
		r := e.Request
		who := "The server's policy asks"
		if r.Source == tui.SourceLocal {
			who = "Your guard asks"
		}
		fmt.Fprintf(w, "\n%s: %s %s\n  y = yes", who, r.Tool, r.Text)
		if r.Similar != "" {
			fmt.Fprintf(w, " · a = yes, and don't ask again for %s", r.Similar)
		}
		fmt.Fprintln(w, " · n = no · or type what to do instead")
	case tui.QuestionAsked:
		for _, item := range e.Question.Items {
			fmt.Fprintf(w, "\n%s\n", item.Text)
			for i, o := range item.Options {
				fmt.Fprintf(w, "  %d. %s\n", i+1, o.Label)
			}
		}
	case tui.Notice:
		fmt.Fprintf(w, "  ⎿ %s\n", e.Text)
	case tui.ConnectionChanged:
		switch e.State {
		case tui.Reconnecting:
			fmt.Fprintf(w, "  ⎿ connection lost, reconnecting in %s\n", e.RetryIn.Round(1e9))
		case tui.Online:
		case tui.Unauthorized:
			fmt.Fprintln(w, "  ⎿ the server refused this device: run daedalus-cli login")
		default:
			if e.Err != "" {
				fmt.Fprintf(w, "  ⎿ %s\n", e.Err)
			}
		}
	case tui.TurnEnded:
		switch e.Outcome {
		case tui.TurnFailed:
			fmt.Fprintf(w, "  ⎿ the turn failed: %s\n", e.Err)
		case tui.TurnInterrupted:
			fmt.Fprintln(w, "  ⎿ interrupted")
		}
	case tui.HistoryReset:
		fmt.Fprintln(w, "  ⎿ (the conversation was read again)")
	}
}

// lastTurns is the tail of a history from its last few prompts, enough to see where things stand.
func lastTurns(history []tui.Event, prompts int) []tui.Event {
	seen := 0
	for i := len(history) - 1; i >= 0; i-- {
		if _, ok := history[i].(tui.UserMessage); ok {
			seen++
			if seen == prompts {
				return history[i:]
			}
		}
	}
	return history
}
