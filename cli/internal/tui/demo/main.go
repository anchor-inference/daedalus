// Command demo runs the terminal interface against the scripted fake backend, so its screens can be
// looked at, recorded and tested without a server.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/tui"
)

func main() {
	delay := flag.Duration("delay", 40*time.Millisecond, "pause between streamed events")
	inline := flag.Bool("inline", false, "print into the scrollback instead of fullscreen")
	themeName := flag.String("theme", "", "dark, light or ansi")
	resume := flag.Bool("resume", false, "open with a resumed session's history")
	prompt := flag.String("prompt", "", "send this prompt at start")
	cwd := flag.String("cwd", "", "working folder to show")
	realShell := flag.Bool("real-shell", false, "run ! commands for real")
	frozen := flag.String("clock", "", "fix the clock at this RFC 3339 time, for reproducible captures")
	dark := flag.String("background", "", "dark or light; empty asks the terminal")
	flag.Parse()

	folder := *cwd
	if folder == "" {
		folder, _ = os.Getwd()
	}
	fake := tui.NewFake(folder)
	fake.Delay = *delay
	fake.RealShell = *realShell
	options := tui.Options{Cwd: folder, Theme: *themeName, Store: &tui.MemoryStore{}, Version: "demo"}
	if *inline {
		options.Inline = inline
	}
	if *frozen != "" {
		at, err := time.Parse(time.RFC3339, *frozen)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		start := time.Now()
		options.Now = func() time.Time { return at.Add(time.Since(start)) }
		fake.Now = options.Now
	}
	switch *dark {
	case "dark", "light":
		d := *dark == "dark"
		options.Dark = &d
	}
	if *resume {
		options.Start.Resume = "s-91c2"
	}
	options.Start.Prompt = *prompt
	if err := tui.Run(context.Background(), fake, options); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
