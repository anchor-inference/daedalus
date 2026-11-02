package ptytest

import (
	"fmt"
	"strings"
)

// GateFrameProblem says what is wrong with one frame of the gate program (gateprog), or "" when the
// frame is whole: the status line on the last row exactly once, the input box's three rows right
// above it, and some transcript above that.
//
// A frame caught between an erase and the redraw that follows it fails this check, which is what
// makes a fast poll of the screen a flicker detector: on a terminal that paints between reads, that
// is the frame a person would have seen flash.
func GateFrameProblem(screen string) string {
	lines := strings.Split(strings.TrimRight(screen, "\n"), "\n")
	for len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) < 5 {
		return fmt.Sprintf("only %d non-blank rows", len(lines))
	}
	if n := strings.Count(screen, "auto-accept on"); n != 1 {
		return fmt.Sprintf("status line appears %d times", n)
	}
	last := len(lines) - 1
	if !strings.Contains(lines[last], "auto-accept on") {
		return fmt.Sprintf("last row is not the status line: %q", lines[last])
	}
	if !strings.HasPrefix(strings.TrimSpace(lines[last-1]), "╰") {
		return fmt.Sprintf("row above the status line is not the box's bottom: %q", lines[last-1])
	}
	if input := strings.TrimSpace(lines[last-2]); !strings.HasPrefix(input, "│>") && !strings.HasPrefix(input, "│ >") {
		return fmt.Sprintf("input row missing: %q", lines[last-2])
	}
	if !strings.HasPrefix(strings.TrimSpace(lines[last-3]), "╭") {
		return fmt.Sprintf("box top missing: %q", lines[last-3])
	}
	if !strings.Contains(screen, "●") && !strings.Contains(screen, "> Measure") {
		return "no transcript on screen"
	}
	return ""
}
