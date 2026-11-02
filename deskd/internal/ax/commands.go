package ax

import (
	"fmt"
	"strconv"
)

// The command lines below are the whole of what the daemon asks of the tool. None carries --headed
// or --force, and no pointer, key, window, clipboard or app command is built anywhere, so the tool's
// real-input paths are unreachable: where an element offers no semantic action the tool refuses
// rather than falling back to input. Every value that came from the host follows "--", where the
// tool's parser takes it as a value even when it starts with a dash.

// The element actions, by the contract's names.
const (
	ActClick    = "click"
	ActSetValue = "set_value"
	ActClear    = "clear"
	ActType     = "type"
	ActSelect   = "select"
	ActToggle   = "toggle"
	ActCheck    = "check"
	ActUncheck  = "uncheck"
	ActExpand   = "expand"
	ActCollapse = "collapse"
	ActScroll   = "scroll"
	ActScrollTo = "scroll_to"
	ActFocus    = "focus"
)

// toolCommand is each action's command in the tool, and whether it takes text.
var toolCommand = map[string]struct {
	name string
	text bool
}{
	ActClick: {"click", false}, ActSetValue: {"set-value", true}, ActClear: {"clear", false},
	ActType: {"type", true}, ActSelect: {"select", true}, ActToggle: {"toggle", false},
	ActCheck: {"check", false}, ActUncheck: {"uncheck", false}, ActExpand: {"expand", false},
	ActCollapse: {"collapse", false}, ActScroll: {"scroll", false}, ActScrollTo: {"scroll-to", false},
	ActFocus: {"focus", false},
}

// TakesText reports whether an action needs text, and whether the action exists.
func TakesText(action string) (text, ok bool) {
	c, ok := toolCommand[action]
	return c.text, ok
}

// The properties get reads; the boolean ones are the tool's "is".
var (
	getProps = map[string]bool{"text": true, "value": true, "title": true, "bounds": true, "role": true, "states": true}
	isProps  = map[string]bool{"visible": true, "enabled": true, "checked": true, "focused": true, "expanded": true, "selected": true}
)

// IsBoolProperty reports whether a property is one of the booleans, and whether it exists.
func IsBoolProperty(p string) (boolean, ok bool) {
	if getProps[p] {
		return false, true
	}
	return isProps[p], isProps[p]
}

// The surfaces a snapshot may read: overlays of the app's own windows. Shell surfaces (task bar,
// tray, start menu) belong to the system, not to any granted window, and are not offered.
var surfaces = map[string]bool{"window": true, "menu": true, "sheet": true, "popover": true, "alert": true}

// ValidSurface reports whether a snapshot may read the surface.
func ValidSurface(s string) bool { return surfaces[s] }

// SnapshotArgs is one snapshot's.
type SnapshotArgs struct {
	Window      string // the tool's window id; empty with Root
	Root        string
	Surface     string
	Skeleton    bool
	Interactive bool
	Bounds      bool
	TimeoutMs   int
}

// Args is the command line.
func (a SnapshotArgs) Args() []string {
	args := []string{"snapshot", "--timeout-ms", strconv.Itoa(a.TimeoutMs)}
	if a.Interactive {
		args = append(args, "-i", "--compact")
	}
	if a.Skeleton {
		args = append(args, "--skeleton")
	}
	if a.Bounds {
		args = append(args, "--include-bounds")
	}
	if a.Root != "" {
		return append(args, "--root", a.Root)
	}
	if a.Surface != "" && a.Surface != "window" {
		args = append(args, "--surface", a.Surface)
	}
	return append(args, "--window-id", a.Window)
}

// FindArgs is one find's.
type FindArgs struct {
	Window, Root            string
	Role, Name, Value, Text string
	Exact                   bool
	Limit                   int
	TimeoutMs               int
}

// Args is the command line. The tool's flags take their value as the next argument; a value that
// starts with a dash is joined to its flag with "=", which the parser never reads as a flag.
func (a FindArgs) Args() []string {
	args := []string{"find", "--timeout-ms", strconv.Itoa(a.TimeoutMs), "--limit", strconv.Itoa(a.Limit)}
	if a.Root != "" {
		args = append(args, "--root", a.Root)
	} else {
		args = append(args, "--window-id", a.Window)
	}
	for _, f := range []struct{ flag, v string }{{"--role", a.Role}, {"--name", a.Name}, {"--value", a.Value}, {"--text", a.Text}} {
		if f.v != "" {
			args = append(args, f.flag+"="+f.v)
		}
	}
	if a.Exact {
		args = append(args, "--exact")
	}
	return args
}

// GetArgs is the command line of a property read.
func GetArgs(ref, property string) []string {
	if isProps[property] {
		return []string{"is", "--property", property, "--", ref}
	}
	return []string{"get", "--property", property, "--", ref}
}

// ActArgs is the command line of an element action.
func ActArgs(action, ref, text, direction string, amount, timeoutMs int) ([]string, error) {
	c, ok := toolCommand[action]
	if !ok {
		return nil, fmt.Errorf("no action %q", action)
	}
	args := []string{c.name, "--timeout-ms", strconv.Itoa(timeoutMs)}
	if action == ActScroll {
		args = append(args, "--direction", direction, "--amount", strconv.Itoa(amount))
	}
	args = append(args, "--", ref)
	if c.text {
		args = append(args, text)
	}
	return args, nil
}

// ListWindowsArgs is the command line of the window list the Linux mapping reads.
func ListWindowsArgs() []string { return []string{"list-windows"} }
