package keys

import (
	"fmt"
	"testing"
)

func blocked(t *testing.T, os, chord string) (Block, bool) {
	t.Helper()
	c, err := ParseChord(chord)
	if err != nil {
		t.Fatalf("%q: %v", chord, err)
	}
	return Blocked(os, c)
}

func TestEverySystemRefusesTheChordsThatEndOrLeaveItsSession(t *testing.T) {
	type want struct {
		os, chord string
		leaves    bool
	}
	cases := []want{
		{"linux", "alt+F4", false},
		{"linux", "shift+alt+f4", false}, // more modifiers than the rule is still the rule
		{"linux", "ctrl+alt+BackSpace", false},
		{"linux", "ctrl+alt+Delete", false},
		{"linux", "ctrl+alt+del", false},
		{"linux", "alt+ctrl+del", false}, // in any order
		{"linux", "super+l", false},
		{"linux", "Meta+L", false},
		{"linux", "ctrl+alt+l", false},
		{"linux", "ctrl+alt+shift+Page_Down", false},
		{"linux", "alt+tab", true},
		{"linux", "alt+shift+tab", true},
		{"linux", "super", true},
		{"linux", "ctrl+super", true},
		{"linux", "super+d", true},
		{"linux", "alt+f2", true},
		{"linux", "ctrl+alt+t", true},
		{"windows", "alt+f4", false},
		{"windows", "ctrl+alt+delete", false},
		{"windows", "ctrl+alt+end", false},
		{"windows", "win+l", false},
		{"windows", "ctrl+shift+esc", false},
		{"windows", "win+x", false},
		{"windows", "alt+tab", true},
		{"windows", "win", true},
		{"windows", "win+r", true},
		{"windows", "win+d", true},
		{"darwin", "cmd+q", false},
		{"darwin", "cmd+shift+q", false},
		{"darwin", "cmd+option+esc", false},
		{"darwin", "cmd+ctrl+q", false},
		{"darwin", "ctrl+cmd+q", false},
		{"darwin", "cmd+shift+BackSpace", false},
		{"darwin", "cmd+tab", true},
		{"darwin", "cmd+space", true},
		{"darwin", "ctrl+up", true},
	}
	for i := 1; i <= 12; i++ {
		cases = append(cases, want{"linux", fmt.Sprintf("ctrl+alt+F%d", i), false})
	}
	for _, c := range cases {
		b, ok := blocked(t, c.os, c.chord)
		if !ok {
			t.Errorf("%s %q is not refused", c.os, c.chord)
			continue
		}
		if b.Leaves != c.leaves || b.Why == "" {
			t.Errorf("%s %q: %+v", c.os, c.chord, b)
		}
		// The fake desktop stands in for every system, so it refuses each one's chords.
		if _, ok := blocked(t, "fake", c.chord); !ok {
			t.Errorf("fake %q is not refused", c.chord)
		}
	}
}

func TestOrdinaryChordsAndOtherSystemsShortcutsPass(t *testing.T) {
	for _, c := range []struct{ os, chord string }{
		{"linux", "ctrl+s"},
		{"linux", "ctrl+q"}, // an application's own shortcut, not the session's
		{"linux", "ctrl+t"},
		{"linux", "ctrl+tab"},
		{"linux", "ctrl+f4"}, // closes a tab, not the window
		{"linux", "ctrl+alt+f13"},
		{"linux", "ctrl+left"},
		{"linux", "enter"},
		{"linux", "alt+f"},
		{"windows", "ctrl+c"},
		{"windows", "ctrl+esc"},
		{"windows", "shift+esc"},
		{"windows", "ctrl+r"},
		{"darwin", "cmd+c"},
		{"darwin", "cmd+w"},
		{"darwin", "cmd+shift+w"},
		{"darwin", "super+l"},     // nothing on macOS
		{"windows", "ctrl+cmd+q"}, // macOS's lock, nothing on Windows
		{"darwin", "alt+f4"},
	} {
		if b, ok := blocked(t, c.os, c.chord); ok {
			t.Errorf("%s %q is refused: %+v", c.os, c.chord, b)
		}
	}
}

func TestAChordThatBothLeavesAndEndsIsReportedAsEnding(t *testing.T) {
	// Cmd+Option+Esc on the fake matches macOS's Force Quit (ends) and Windows' Alt+Esc (leaves):
	// Force Quit is what a lease over the whole desktop must still refuse.
	b, ok := blocked(t, "fake", "cmd+option+esc")
	if !ok || b.Leaves {
		t.Fatalf("%+v %v", b, ok)
	}
}
