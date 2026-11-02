package main

import (
	"context"
	"io"

	"github.com/ascorblack/daedalus/cli/internal/app"
	"github.com/ascorblack/daedalus/cli/internal/config"
	"github.com/ascorblack/daedalus/cli/internal/tui"
)

func startRequest(resume string, cont bool, prompt string) tui.StartRequest {
	return tui.StartRequest{Resume: resume, Continue: cont, Prompt: prompt}
}

// runScreen shows a session: the full interface on a terminal, line mode with --plain or when stdin
// or stdout is not one (a pipe, an editor's shell), since the interface needs cursor control.
func runScreen(ctx context.Context, b *app.Backend, e *env, req tui.StartRequest, plain bool, in io.Reader, out io.Writer) error {
	if plain {
		p := &app.Plain{Backend: b, In: in, Out: out}
		return p.Run(ctx, req)
	}
	// The interface's prefs and prompt history live beside the configuration.
	dir, _ := config.Dir()
	options := tui.Options{Cwd: e.cwd, AgentName: "Daedalus", Version: version, Start: req, Store: &tui.FileStore{Dir: dir}}
	if e.guardChosen {
		options.Guard = e.guard.String()
	}
	return tui.Run(ctx, b, options)
}
