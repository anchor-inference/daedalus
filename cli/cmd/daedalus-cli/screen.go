package main

import (
	"context"
	"io"

	"github.com/ascorblack/daedalus/cli/internal/app"
	"github.com/ascorblack/daedalus/cli/internal/tui"
)

func startRequest(resume string, cont bool, prompt string) tui.StartRequest {
	return tui.StartRequest{Resume: resume, Continue: cont, Prompt: prompt}
}

// runScreen shows a session. The full interface (internal/tui, Bubble Tea) takes over here once it
// is part of the build: `tui.Run(ctx, b, tui.Options{Cwd: e.cwd, AgentName: "Daedalus", Version:
// version, Start: req})`. Until then, and always with --plain or without a terminal, the session is
// shown in line mode.
func runScreen(ctx context.Context, b *app.Backend, e *env, req tui.StartRequest, plain bool, in io.Reader, out io.Writer) error {
	_ = plain
	p := &app.Plain{Backend: b, In: in, Out: out}
	return p.Run(ctx, req)
}
