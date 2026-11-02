// Package guard is the local guard: what this machine lets the server's agent do in the working
// folder, decided here and not on the server. The server's own policy still applies on top; the guard
// is the operator's say on the machine whose files are at stake, and the server cannot override it.
//
// Three levels, cycled with Shift+Tab:
//
//   - Ask confirms every write and every command that is not plainly read-only, through the screen.
//   - Auto runs what arrives, leaving the decision to the server's policy.
//   - Plan refuses writes and commands outright, except commands that only read.
package guard

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
)

// Level is one guard level.
type Level int

const (
	Ask Level = iota
	Auto
	Plan
)

// String is the level's name, as flags and the configuration spell it.
func (l Level) String() string {
	switch l {
	case Ask:
		return "ask"
	case Auto:
		return "auto"
	case Plan:
		return "plan"
	}
	return fmt.Sprintf("level(%d)", int(l))
}

// Next is the level Shift+Tab moves to.
func (l Level) Next() Level { return (l + 1) % 3 }

// ParseLevel reads a level's name; "read-only" is Plan's other name.
func ParseLevel(s string) (Level, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "ask", "manual", "default":
		return Ask, nil
	case "auto", "":
		return Auto, nil
	case "plan", "read-only", "readonly":
		return Plan, nil
	}
	return Auto, fmt.Errorf("guard %q: want ask, auto or plan", s)
}

// Kind is what an action does to the machine.
type Kind int

const (
	// Read only looks: a file read, a listing, a stat.
	Read Kind = iota
	// Write changes a file or makes a directory.
	Write
	// Exec runs a command.
	Exec
)

func (k Kind) String() string {
	switch k {
	case Read:
		return "read"
	case Write:
		return "write"
	case Exec:
		return "exec"
	}
	return "unknown"
}

// Action is one request from the server, described for the guard and for the person asked.
type Action struct {
	Kind Kind
	// Method is the side-channel method, e.g. "exec.run" or "fs.read".
	Method string
	// Command is the shell command for Exec (the script of `bash -c`, or the argv joined).
	Command string
	// Path is the file for Read and Write.
	Path string
	// Session is the server session the call belongs to, when the server said.
	Session string
}

// Summary is the one line the person sees.
func (a Action) Summary() string {
	switch a.Kind {
	case Exec:
		return a.Command
	case Write:
		return "write " + a.Path
	}
	return a.Method + " " + a.Path
}

// Decision is the person's answer to a confirmation.
type Decision struct {
	Allow bool
	// Remember allows the actions like this one for the rest of the client's run.
	Remember bool
	// Reason is what the person typed with a refusal; it goes back to the agent.
	Reason string
}

// Prompter asks the person. The terminal interface implements it; headless mode has none, and an
// action Ask would confirm is then refused, because nobody is there to say yes.
type Prompter interface {
	Confirm(ctx context.Context, a Action) (Decision, error)
}

// ErrRefused is the guard's refusal. The device connection answers the call with it as a refusal
// the agent reads, never as a transport failure the server might retry.
var ErrRefused = errors.New("refused by the local guard")

// Refusal is a refused action and why.
type Refusal struct {
	Level  Level
	Reason string
}

func (r *Refusal) Error() string {
	return "refused by the local guard (" + r.Level.String() + "): " + r.Reason
}

func (r *Refusal) Unwrap() error { return ErrRefused }

// Guard holds the current level and the remembered approvals.
type Guard struct {
	mu       sync.Mutex
	level    Level
	prompter Prompter
	// remembered are the "don't ask again" keys, for this run of the client only: a remembered yes
	// that outlived the run would be a standing permission nobody sees.
	remembered map[string]bool
	onChange   func(Level)
}

// New returns a guard at level.
func New(level Level, prompter Prompter) *Guard {
	return &Guard{level: level, prompter: prompter, remembered: map[string]bool{}}
}

// Level is the current level.
func (g *Guard) Level() Level {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.level
}

// Set changes the level.
func (g *Guard) Set(l Level) {
	g.mu.Lock()
	g.level = l
	notify := g.onChange
	g.mu.Unlock()
	if notify != nil {
		notify(l)
	}
}

// OnChange registers a function called after every level change.
func (g *Guard) OnChange(fn func(Level)) {
	g.mu.Lock()
	g.onChange = fn
	g.mu.Unlock()
}

// SetPrompter installs the interface that asks the person.
func (g *Guard) SetPrompter(p Prompter) {
	g.mu.Lock()
	g.prompter = p
	g.mu.Unlock()
}

// Check decides one action: nil to run it, a *Refusal otherwise. It blocks while the person is asked.
func (g *Guard) Check(ctx context.Context, a Action) error {
	g.mu.Lock()
	level, prompter := g.level, g.prompter
	key := rememberKey(a)
	remembered := g.remembered[key]
	g.mu.Unlock()

	readOnly := a.Kind == Read || (a.Kind == Exec && ReadOnlyCommand(a.Command))
	switch level {
	case Auto:
		return nil
	case Plan:
		if readOnly {
			return nil
		}
		if a.Kind == Write {
			return &Refusal{Level: Plan, Reason: "plan mode is read-only: no file is written on this machine (" + a.Path + ")"}
		}
		return &Refusal{Level: Plan, Reason: "plan mode is read-only: only commands that read run on this machine"}
	}
	// Ask.
	if readOnly || remembered {
		return nil
	}
	if prompter == nil {
		return &Refusal{Level: Ask, Reason: "this needs the operator's approval and nobody is at the terminal to give it; run with --permission-mode auto to allow it"}
	}
	d, err := prompter.Confirm(ctx, a)
	if err != nil {
		return &Refusal{Level: Ask, Reason: "no answer: " + err.Error()}
	}
	if !d.Allow {
		reason := "the operator said no"
		if d.Reason != "" {
			reason += ": " + d.Reason
		}
		return &Refusal{Level: Ask, Reason: reason}
	}
	if d.Remember && key != "" {
		g.mu.Lock()
		g.remembered[key] = true
		g.mu.Unlock()
	}
	return nil
}

// Similar is what "don't ask again" covers for an action, in the person's words; empty when nothing
// narrower than the action itself would be meaningful.
func Similar(a Action) string {
	switch a.Kind {
	case Write:
		return "writes in this folder"
	case Exec:
		if p := commandPrefix(a.Command); p != "" {
			return p + " *"
		}
	}
	return ""
}

// rememberKey is the key a remembered yes is filed under: every write alike (the jail already holds
// them to the folder), and a command by its program and first word.
func rememberKey(a Action) string {
	switch a.Kind {
	case Write:
		return "write"
	case Exec:
		if p := commandPrefix(a.Command); p != "" {
			return "exec:" + p
		}
	}
	return ""
}

// commandPrefix is the program and its subcommand, "npm test" for "npm test -- --watch", or empty
// for a compound command, which no prefix describes honestly.
func commandPrefix(command string) string {
	words, ok := splitWords(command)
	if !ok || len(words) == 0 {
		return ""
	}
	for _, w := range words {
		if isOperator(w) {
			return ""
		}
	}
	if len(words) > 1 && !strings.HasPrefix(words[1], "-") {
		return words[0] + " " + words[1]
	}
	return words[0]
}
