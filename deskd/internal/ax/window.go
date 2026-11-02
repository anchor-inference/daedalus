package ax

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

// How the daemon's window ids meet the tool's. On Windows and macOS both name the system's own
// window (a handle, a window number), so one is a rewrite of the other. On Linux the tool names a
// window by its AT-SPI address, which says nothing of the X11 window: the two meet only through the
// process and the place on screen.
const (
	MapDirect  = "direct"
	MapOverlap = "overlap"
)

// MappingFor is how windows are matched on a system (runtime.GOOS, or the fake's "fake").
func MappingFor(system string) string {
	switch system {
	case "windows", "darwin":
		return MapDirect
	}
	return MapOverlap
}

// DirectID is the tool's id of one of the daemon's windows on Windows ("0x<hex>" is the handle the
// tool writes in decimal) and macOS (the window number, in decimal on both sides).
func DirectID(system, id string) (string, error) {
	switch system {
	case "windows":
		hex, ok := strings.CutPrefix(strings.ToLower(id), "0x")
		if !ok {
			return "", fmt.Errorf("window id %q is not a handle", id)
		}
		n, err := strconv.ParseUint(hex, 16, 64)
		if err != nil {
			return "", fmt.Errorf("window id %q is not a handle", id)
		}
		return "w-" + strconv.FormatUint(n, 10), nil
	case "darwin":
		n, err := strconv.ParseUint(id, 10, 64)
		if err != nil {
			return "", fmt.Errorf("window id %q is not a window number", id)
		}
		return "w-" + strconv.FormatUint(n, 10), nil
	}
	return "", fmt.Errorf("no direct mapping on %s", system)
}

// FromDirectID is the daemon's id of a window the tool named, on Windows and macOS.
func FromDirectID(system, toolID string) (string, bool) {
	digits, ok := strings.CutPrefix(toolID, "w-")
	if !ok {
		return "", false
	}
	n, err := strconv.ParseUint(digits, 10, 64)
	if err != nil {
		return "", false
	}
	switch system {
	case "windows":
		return "0x" + strconv.FormatUint(n, 16), true
	case "darwin":
		return strconv.FormatUint(n, 10), true
	}
	return "", false
}

// The thresholds of the overlap match. The X11 window's bounds include the frame the window manager
// draws (a title bar of 30 or so pixels), the AT-SPI frame's do not, so a true pair overlaps by a
// little less than all: 800×600 against 800×570 is 0.95. A dialog of the same process lying over
// its main window overlaps that window far less, and two windows of one process stacked exactly on
// each other (a tabbed editor's views) cannot be told apart and are refused as ambiguous.
const (
	minOverlap       = 0.6
	ambiguousOverlap = 0.02
)

// MatchResult is the outcome of an overlap match.
type MatchResult struct {
	ID         string
	Ambiguous  bool
	Candidates int
}

// MatchByOverlap finds, among the tool's windows, the one that is the daemon's window of process pid
// at bounds: the same process, and the largest overlap above minOverlap. A window the tool reports
// without bounds cannot be placed and is never chosen.
func MatchByOverlap(windows []ToolWindow, pid int, bounds geom.Rect) MatchResult {
	best, second := -1.0, -1.0
	var bestID string
	n := 0
	for _, w := range windows {
		if w.PID != pid || pid == 0 || w.Bounds == nil {
			continue
		}
		o := overlap(w.Bounds.Rect(), bounds)
		if o < minOverlap {
			continue
		}
		n++
		if o > best {
			second, best, bestID = best, o, w.ID
		} else if o > second {
			second = o
		}
	}
	if best < 0 {
		return MatchResult{}
	}
	if second >= 0 && best-second < ambiguousOverlap {
		return MatchResult{Ambiguous: true, Candidates: n}
	}
	return MatchResult{ID: bestID, Candidates: n}
}

// overlap is the intersection over the union of two rectangles.
func overlap(a, b geom.Rect) float64 {
	i := a.Intersect(b)
	if i.Empty() {
		return 0
	}
	inter := i.W * i.H
	union := a.W*a.H + b.W*b.H - inter
	if union <= 0 {
		return 0
	}
	return inter / union
}
