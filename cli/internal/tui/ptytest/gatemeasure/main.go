// Command gatemeasure runs the renderer gate: the gate program in a pty and in tmux, fullscreen and
// inline, and writes the casts, the screens and a summary into one directory.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/tui/ptytest"
)

func main() {
	program := flag.String("program", "", "path to the built gateprog")
	out := flag.String("out", ".", "directory for the artefacts")
	events := flag.Int("events", 200, "events to replay")
	delay := flag.Duration("delay", 15*time.Millisecond, "time between events")
	flag.Parse()
	if *program == "" {
		fmt.Fprintln(os.Stderr, "-program is required")
		os.Exit(2)
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		fail(err)
	}
	var summary strings.Builder
	for _, inline := range []bool{false, true} {
		mode := "fullscreen"
		if inline {
			mode = "inline"
		}
		fmt.Fprintf(&summary, "== %s\n", mode)
		pty(&summary, *program, *out, mode, inline, true, *events, *delay)
		pty(&summary, *program, *out, mode, inline, false, *events, *delay)
		if ptytest.TmuxAvailable() {
			tmux(&summary, *program, *out, mode, inline, *events, *delay)
		}
	}
	path := filepath.Join(*out, "summary.txt")
	_ = os.WriteFile(path, []byte(summary.String()), 0o644)
	fmt.Print(summary.String())
}

func args(inline bool, events int, delay time.Duration, quitAfter time.Duration) []string {
	a := []string{fmt.Sprintf("-events=%d", events), fmt.Sprintf("-delay=%s", delay), fmt.Sprintf("-quit-after=%s", quitAfter)}
	if inline {
		a = append(a, "-inline")
	}
	return a
}

// pty measures the byte stream at 120x40 with two resizes in the middle of the replay.
func pty(summary *strings.Builder, program, out, mode string, inline, sync bool, events int, delay time.Duration) {
	var opts []ptytest.Option
	if !sync {
		opts = append(opts, ptytest.WithoutSyncSupport())
		mode += "-nosync"
	}
	// Sixty lines of shell output first, so the run starts with a full screen and a scrollback,
	// as it does for a person, and losing either shows up in the history afterwards.
	script := "i=1; while [ $i -le 60 ]; do echo before-$i; i=$((i+1)); done; exec \"$0\" \"$@\""
	term, err := ptytest.Start(nil, exec.Command("sh", append([]string{"-c", script, program}, args(inline, events, delay, 600*time.Millisecond)...)...), 120, 40, opts...)
	if err != nil {
		fail(err)
	}
	defer term.Close()
	replay := time.Duration(events) * delay
	// A third of the way in, narrow; two thirds, widen; check each frame once it settles.
	time.Sleep(replay / 3)
	_ = term.Resize(80, 24)
	time.Sleep(250 * time.Millisecond)
	narrow := term.Screen()
	time.Sleep(replay / 3)
	_ = term.Resize(160, 50)
	time.Sleep(250 * time.Millisecond)
	wide := term.Screen()
	_ = term.Wait(replay + 10*time.Second)
	final := term.Screen()
	history := term.History()
	kept := 0
	for _, line := range history {
		if strings.HasPrefix(line, "before-") {
			kept++
		}
	}
	recording := term.Recording()
	_ = recording.WriteCast(filepath.Join(out, "pty-"+mode+".cast"))
	_ = os.WriteFile(filepath.Join(out, "pty-"+mode+"-80x24.txt"), []byte(narrow), 0o644)
	_ = os.WriteFile(filepath.Join(out, "pty-"+mode+"-160x50.txt"), []byte(wide), 0o644)
	_ = os.WriteFile(filepath.Join(out, "pty-"+mode+"-after-exit.txt"), []byte(final), 0o644)
	stats := ptytest.Analyze(recording)
	fmt.Fprintf(summary, "pty  %s\n", stats)
	fmt.Fprintf(summary, "pty  frame after resize to 80x24: %s\n", verdict(ptytest.GateFrameProblem(narrow)))
	fmt.Fprintf(summary, "pty  frame after resize to 160x50: %s\n", verdict(ptytest.GateFrameProblem(wide)))
	fmt.Fprintf(summary, "pty  history after exit: %d/60 earlier shell lines kept\n", kept)
	_ = os.WriteFile(filepath.Join(out, "pty-"+mode+"-history.txt"), []byte(strings.Join(history, "\n")), 0o644)
}

// tmux polls the pane as fast as tmux answers and counts frames that are not whole, resizes in the
// middle, and checks that shell output from before the program is still in the history after it.
func tmux(summary *strings.Builder, program, out, mode string, inline bool, events int, delay time.Duration) {
	session, err := ptytest.NewTmux(120, 40)
	if err != nil {
		fmt.Fprintf(summary, "tmux unavailable: %v\n", err)
		return
	}
	defer session.Kill()
	for i := 1; i <= 30; i++ {
		_ = session.Type(fmt.Sprintf("echo before-%02d", i), true)
	}
	time.Sleep(300 * time.Millisecond)
	// script(1) keeps a copy of exactly the bytes the program sent to tmux, so the stream tmux had
	// to render can be analysed as the pty's is.
	bytesLog := filepath.Join(out, "tmux-"+mode+".bytes")
	command := "script -q -E never -O " + bytesLog + " -c '" + program + " " + strings.Join(args(inline, events, delay, 800*time.Millisecond), " ") + "'; echo gate-exited"
	_ = session.Type(command, true)

	replay := time.Duration(events) * delay
	deadline := time.Now().Add(replay + 600*time.Millisecond)
	resizeAt := time.Now().Add(replay / 2)
	resized := false
	var resizedAt time.Time
	var recovery time.Duration
	transient := 0
	polls, bad, settled := 0, 0, false
	var firstBad, lastScreen string
	var badReasons = map[string]int{}
	for time.Now().Before(deadline) {
		screen, err := session.Capture()
		if err != nil {
			break
		}
		if !settled {
			// The first frames are the program starting up; count from the first whole frame.
			settled = ptytest.GateFrameProblem(screen) == ""
		} else {
			polls++
			problem := ptytest.GateFrameProblem(screen)
			if problem == "" && resized && recovery == 0 {
				recovery = time.Since(resizedAt)
			}
			if problem != "" && resized && recovery == 0 {
				// tmux crops the old frame to the new size before the program has redrawn: a
				// resize transient, counted apart from flicker during streaming.
				transient++
			} else if problem != "" {
				if strings.Contains("\n"+screen, "\ngate-exited\n") {
					break
				}
				bad++
				badReasons[problem]++
				if firstBad == "" {
					firstBad = problem + "\n" + screen
				}
			}
		}
		lastScreen = screen
		if !resized && time.Now().After(resizeAt) {
			_ = session.Resize(90, 30)
			resized = true
			resizedAt = time.Now()
		}
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(200 * time.Millisecond)
	afterResize, _ := session.Capture()
	time.Sleep(time.Second)
	history, _ := session.History()
	_ = os.WriteFile(filepath.Join(out, "tmux-"+mode+"-last-frame.txt"), []byte(lastScreen), 0o644)
	_ = os.WriteFile(filepath.Join(out, "tmux-"+mode+"-after-resize.txt"), []byte(afterResize), 0o644)
	_ = os.WriteFile(filepath.Join(out, "tmux-"+mode+"-history.txt"), []byte(history), 0o644)
	if firstBad != "" {
		_ = os.WriteFile(filepath.Join(out, "tmux-"+mode+"-first-bad-frame.txt"), []byte(firstBad), 0o644)
	}
	kept := 0
	for i := 1; i <= 30; i++ {
		if strings.Contains(history, fmt.Sprintf("before-%02d\n", i)) {
			kept++
		}
	}
	statusLines := strings.Count(history, "auto-accept on")
	if raw, err := os.ReadFile(bytesLog); err == nil {
		stats := ptytest.Analyze(ptytest.Recording{Cols: 120, Rows: 40, Chunks: []ptytest.Chunk{{Data: raw}}})
		fmt.Fprintf(summary, "tmux bytes: %s\n", stats)
	}
	fmt.Fprintf(summary, "tmux polls=%d incomplete_frames_while_streaming=%d reasons=%v resize_transient_frames=%d resize_recovered_in=%s\n",
		polls, bad, badReasons, transient, recovery.Round(time.Millisecond))
	fmt.Fprintf(summary, "tmux history after exit: %d/30 earlier shell lines kept, status line appears %d times, exit marker %v\n",
		kept, statusLines, strings.Contains("\n"+history, "\ngate-exited\n"))
}

func verdict(problem string) string {
	if problem == "" {
		return "whole"
	}
	return "BROKEN: " + problem
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
