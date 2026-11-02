package keys

import "fmt"

// Block is why a chord is refused. The daemon refuses these whatever the host asks: an agent that
// presses one has left the window it was given, or taken the machine away from the operator, and no
// screenshot afterwards can show the operator what happened.
type Block struct {
	// Why is the chord's effect in a few words, for the refusal's message.
	Why string
	// Leaves is a chord that only takes the keyboard away from the window in front (a task switcher,
	// a launcher). A lease over the whole desktop already allows that by clicking, so only a narrower
	// lease refuses it; every other chord is refused under any lease.
	Leaves bool
}

type blockRule struct {
	mods []Mod
	key  string
	Block
}

// The tables are per system because the same keys mean different things: Super+L locks Windows and
// most Linux desktops and is harmless on macOS, where Ctrl+Cmd+Q locks instead. A rule matches a chord
// with its key and at least its modifiers, in any order, so Shift+Alt+F4 is still Alt+F4. On macOS
// Super is Command and Alt is Option.
var (
	linuxRules = []blockRule{
		rule("alt+f4", "closes the window in front, and on the bare desktop offers to shut down", false),
		rule("ctrl+alt+backspace", "kills the display server and the whole session with it", false),
		rule("ctrl+alt+delete", "opens the log-out dialog or restarts the machine", false),
		rule("super+l", "locks the screen", false),
		rule("ctrl+alt+l", "locks the screen", false),
		// KDE's shortcuts that restart or shut down without asking.
		rule("ctrl+alt+shift+pageup", "restarts the machine without asking", false),
		rule("ctrl+alt+shift+pagedown", "shuts the machine down without asking", false),
		rule("alt+tab", "switches to another window", true),
		rule("super+tab", "switches to another window", true),
		rule("super", "opens the system's launcher", true),
		rule("super+d", "shows the desktop and hides every window", true),
		rule("alt+f1", "opens the system's launcher", true),
		rule("alt+f2", "opens a dialog that runs any command", true),
		rule("ctrl+alt+t", "opens a terminal", true),
	}
	windowsRules = []blockRule{
		rule("alt+f4", "closes the window in front, and on the bare desktop offers to shut down", false),
		rule("ctrl+alt+delete", "opens the system's secure screen (lock, sign out, Task Manager)", false),
		// The same screen inside a remote desktop session.
		rule("ctrl+alt+end", "opens the system's secure screen (lock, sign out, Task Manager)", false),
		rule("super+l", "locks the screen", false),
		rule("ctrl+shift+escape", "opens Task Manager, which ends any program", false),
		rule("super+x", "opens the menu that shuts down or signs out", false),
		rule("alt+tab", "switches to another window", true),
		rule("super+tab", "switches to another window", true),
		rule("alt+escape", "sends the window in front to the back", true),
		rule("super", "opens the Start menu", true),
		rule("super+d", "shows the desktop and hides every window", true),
		rule("super+m", "minimises every window", true),
		rule("super+r", "opens a dialog that runs any command", true),
		rule("super+e", "opens File Explorer", true),
	}
	darwinRules = []blockRule{
		rule("super+q", "quits the app in front", false),
		rule("ctrl+super+q", "locks the screen", false),
		rule("super+shift+q", "logs out", false),
		rule("super+alt+escape", "opens Force Quit, which ends any program", false),
		rule("super+shift+backspace", "empties the Trash", false),
		rule("super+tab", "switches to another app", true),
		rule("super+space", "opens Spotlight, which starts any app", true),
		rule("super+h", "hides the app in front", true),
		rule("super+m", "minimises the window in front", true),
		rule("ctrl+up", "opens Mission Control", true),
		rule("ctrl+left", "moves to another Space", true),
		rule("ctrl+right", "moves to another Space", true),
	}
)

func init() {
	// Ctrl+Alt+F1 to F12 switch the virtual terminal: the session is still there, but the screen and
	// the keyboard now belong to a text console or a greeter the daemon cannot see.
	for i := 1; i <= 12; i++ {
		linuxRules = append(linuxRules, rule(fmt.Sprintf("ctrl+alt+f%d", i), "switches to another virtual terminal, away from the session", false))
	}
}

func rule(chord, why string, leaves bool) blockRule {
	c, err := ParseChord(chord)
	if err != nil {
		panic("keys: a blocked chord that does not parse: " + chord)
	}
	return blockRule{mods: c.Mods, key: c.Key.String(), Block: Block{Why: why, Leaves: leaves}}
}

func rulesFor(os string) [][]blockRule {
	switch os {
	case "linux":
		return [][]blockRule{linuxRules}
	case "windows":
		return [][]blockRule{windowsRules}
	case "darwin":
		return [][]blockRule{darwinRules}
	}
	// The fake desktop, and any system without a table of its own, refuses every system's chords:
	// the fake stands in for all three in the tests, and an unknown system is safer refusing a chord
	// it might not mean than sending one that might lock it.
	return [][]blockRule{linuxRules, windowsRules, darwinRules}
}

// Blocked reports whether a chord is refused on the system os ("linux", "windows", "darwin"). A chord
// that matches both a rule that leaves and one that does not is reported as the one that does not.
func Blocked(os string, c Chord) (Block, bool) {
	var found Block
	ok := false
	for _, rules := range rulesFor(os) {
		for _, r := range rules {
			if r.key != c.Key.String() || !holds(c.Mods, r.mods) {
				continue
			}
			if !ok || found.Leaves && !r.Leaves {
				found, ok = r.Block, true
			}
		}
	}
	return found, ok
}

func holds(have, want []Mod) bool {
	for _, w := range want {
		found := false
		for _, h := range have {
			found = found || h == w
		}
		if !found {
			return false
		}
	}
	return true
}
