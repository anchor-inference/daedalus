package keys

import (
	"reflect"
	"strings"
	"testing"
	"unicode/utf16"
)

func TestTheDialectsParseToOneChord(t *testing.T) {
	for in, want := range map[string]string{
		"Return":            "enter",
		"KP_Enter":          "enter",
		"ENTER":             "enter",
		"ctrl+shift+t":      "ctrl+shift+t",
		"shift+ctrl+t":      "ctrl+shift+t", // the order a chord presses its modifiers is ours
		"CTRL+ARROWLEFT":    "ctrl+left",
		"ctrl+Page_Down":    "ctrl+pagedown",
		"Meta+a":            "super+a",
		"cmd+space":         "super+space",
		"alt+F4":            "alt+f4",
		"Control_L+c":       "ctrl+c",
		"ctrl++":            "ctrl++",
		"ctrl+plus":         "ctrl++",
		"A":                 "shift+a", // a capital alone is Shift and the letter
		"ctrl+A":            "ctrl+a",  // but with a modifier the letter is the key
		"shift":             "shift",
		"super":             "super",
		"Escape":            "escape",
		"BackSpace":         "backspace",
		"ж":                 "ж",
		"KP_7":              "7",
		"ctrl+alt+Delete":   "ctrl+alt+delete",
		"option+cmd+escape": "alt+super+escape",
	} {
		c, err := ParseChord(in)
		if err != nil {
			t.Errorf("%q: %v", in, err)
			continue
		}
		if got := c.String(); got != want {
			t.Errorf("%q parsed as %q, want %q", in, got, want)
		}
	}
}

func TestNonsenseKeysAreRefused(t *testing.T) {
	for _, in := range []string{"", "ctrl+", "a+b", "ctrl+nosuchkey", "hyperdrive", "ctrl+\x01"} {
		if c, err := ParseChord(in); err == nil {
			t.Errorf("%q parsed as %q", in, c)
		}
	}
	if _, err := ParseSequence("   "); err == nil {
		t.Error("an empty sequence parsed")
	}
	seq, err := ParseSequence("ctrl+a Delete")
	if err != nil || len(seq) != 2 || seq[1].Key.Name != Delete {
		t.Errorf("sequence %v %v", seq, err)
	}
}

func TestPrintableKeysAreTheOnesTheAuditHides(t *testing.T) {
	for in, want := range map[string]bool{"a": true, "A": true, "space": true, "ctrl+a": false, "enter": false, "ж": true, "super+l": false} {
		c, _ := ParseChord(in)
		if c.Printable() != want {
			t.Errorf("%q printable=%v", in, !want)
		}
	}
}

func TestLineBreaksAndTabsBecomeKeys(t *testing.T) {
	got := Split("one\r\ntwo\nthree\rfour\tfive")
	want := []Segment{{Text: "one"}, {Key: Enter}, {Text: "two"}, {Key: Enter}, {Text: "three"}, {Key: Enter},
		{Text: "four"}, {Key: Tab}, {Text: "five"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
	if got := Split("\n"); !reflect.DeepEqual(got, []Segment{{Key: Enter}}) {
		t.Fatalf("a lone newline: %+v", got)
	}
}

func TestChunksNeverPassTheLimitNorSplitAPair(t *testing.T) {
	text := strings.Repeat("ab😀", 9) + "Привет, мир" + strings.Repeat("😀", 15)
	chunks := ChunkUTF16(text, 20)
	if strings.Join(chunks, "") != text {
		t.Fatal("the chunks do not add up to the text")
	}
	for _, c := range chunks {
		units := utf16.Encode([]rune(c))
		if len(units) > 20 {
			t.Errorf("chunk %q is %d units", c, len(units))
		}
		if len(units) > 0 && utf16.IsSurrogate(rune(units[len(units)-1])) && units[len(units)-1] < 0xDC00 {
			t.Errorf("chunk %q ends on a high surrogate", c)
		}
	}
	// 19 units then an emoji: the emoji starts the next chunk rather than straddling the two.
	c := ChunkUTF16(strings.Repeat("x", 19)+"😀", 20)
	if len(c) != 2 || c[1] != "😀" {
		t.Errorf("%q", c)
	}
}

func TestSurrogatePairsGoDownTogether(t *testing.T) {
	got := UnicodeStrokes("aé😀")
	want := []Stroke{
		{Unit: 'a'}, {Unit: 'a', Up: true},
		{Unit: 0xE9}, {Unit: 0xE9, Up: true},
		{Unit: 0xD83D}, {Unit: 0xDE00}, {Unit: 0xD83D, Up: true}, {Unit: 0xDE00, Up: true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
}
