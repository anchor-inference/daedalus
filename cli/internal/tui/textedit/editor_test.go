package textedit

import (
	"reflect"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
)

var namedKeys = map[string]rune{
	"enter":     tea.KeyEnter,
	"esc":       tea.KeyEscape,
	"backspace": tea.KeyBackspace,
	"delete":    tea.KeyDelete,
	"left":      tea.KeyLeft,
	"right":     tea.KeyRight,
	"up":        tea.KeyUp,
	"down":      tea.KeyDown,
	"home":      tea.KeyHome,
	"end":       tea.KeyEnd,
	"tab":       tea.KeyTab,
	"pgup":      tea.KeyPgUp,
}

// press builds the key a terminal would report for a spec like "ctrl+a", "shift+enter" or "x".
func press(spec string) tea.KeyPressMsg {
	var mod tea.KeyMod
	for {
		switch {
		case strings.HasPrefix(spec, "ctrl+") && len(spec) > len("ctrl+"):
			mod |= tea.ModCtrl
			spec = spec[len("ctrl+"):]
			continue
		case strings.HasPrefix(spec, "alt+") && len(spec) > len("alt+"):
			mod |= tea.ModAlt
			spec = spec[len("alt+"):]
			continue
		case strings.HasPrefix(spec, "shift+") && len(spec) > len("shift+"):
			mod |= tea.ModShift
			spec = spec[len("shift+"):]
			continue
		}
		break
	}
	if code, ok := namedKeys[spec]; ok {
		return tea.KeyPressMsg{Code: code, Mod: mod}
	}
	r := []rune(spec)[0]
	k := tea.KeyPressMsg{Code: r, Mod: mod}
	if mod&(tea.ModCtrl|tea.ModAlt) == 0 {
		k.Text = spec
	}
	return k
}

// do feeds keys to the editor: "<spec>" is one named key, anything else is typed rune by rune.
// It returns the outcome of the last key.
func do(e *Editor, inputs ...string) Outcome {
	var out Outcome
	for _, in := range inputs {
		if strings.HasPrefix(in, "<") && strings.HasSuffix(in, ">") && len(in) > 2 {
			out = e.HandleKey(press(in[1 : len(in)-1]))
			continue
		}
		for _, r := range in {
			out = e.HandleKey(press(string(r)))
		}
	}
	return out
}

func check(t *testing.T, e *Editor, value string, cursor int) {
	t.Helper()
	if got := e.Value(); got != value {
		t.Fatalf("value = %q, want %q", got, value)
	}
	if got := e.Cursor(); got != cursor {
		t.Fatalf("cursor = %d, want %d (value %q)", got, cursor, value)
	}
}

func TestReadlineEditing(t *testing.T) {
	cases := []struct {
		name   string
		keys   []string
		value  string
		cursor int
	}{
		{"typing", []string{"hello"}, "hello", 5},
		{"wide characters count as one rune each", []string{"你好", "<left>", "x"}, "你x好", 2},
		{"cyrillic", []string{"привет", "<ctrl+a>", "<ctrl+f>", "<delete>"}, "пивет", 1},
		{"ctrl+a and ctrl+e", []string{"abc", "<ctrl+a>", "x", "<ctrl+e>", "y"}, "xabcy", 5},
		{"home and end", []string{"abc", "<home>", "1", "<end>", "2"}, "1abc2", 5},
		{"ctrl+b ctrl+f", []string{"abc", "<ctrl+b>", "<ctrl+b>", "<ctrl+f>", "X"}, "abXc", 3},
		{"backspace", []string{"abc", "<backspace>"}, "ab", 2},
		{"ctrl+h is backspace", []string{"abc", "<ctrl+h>"}, "ab", 2},
		{"delete", []string{"abc", "<home>", "<delete>"}, "bc", 0},
		{"backspace at start does nothing", []string{"a", "<home>", "<backspace>"}, "a", 0},
		{"ctrl+d deletes forward", []string{"abc", "<home>", "<ctrl+d>"}, "bc", 0},
		{"word moves", []string{"foo bar.baz", "<alt+b>", "<alt+b>", "X"}, "foo Xbar.baz", 5},
		{"word forward", []string{"foo bar baz", "<home>", "<alt+f>", "<ctrl+right>", "X"}, "foo barX baz", 8},
		{"ctrl+k kills to end of line", []string{"hello world", "<ctrl+a>", "<alt+f>", "<ctrl+k>"}, "hello", 5},
		{"ctrl+k at line end joins", []string{"ab", "<ctrl+j>", "cd", "<up>", "<ctrl+e>", "<ctrl+k>"}, "abcd", 2},
		{"ctrl+u kills to line start", []string{"hello world", "<alt+b>", "<ctrl+u>"}, "world", 0},
		{"ctrl+w kills a whitespace word", []string{"git commit -m", "<ctrl+w>"}, "git commit ", 11},
		{"alt+backspace kills a word", []string{"path/to/file", "<alt+backspace>"}, "path/to/", 8},
		{"alt+d kills forward", []string{"one two", "<home>", "<alt+d>"}, " two", 0},
		{"consecutive kills join for one yank", []string{"one two three", "<ctrl+w>", "<ctrl+w>", "<ctrl+y>"}, "one two three", 13},
		{"undo", []string{"abc", "<ctrl+_>"}, "", 0},
		{"ctrl+slash undoes too", []string{"abc", "<ctrl+/>"}, "", 0},
		{"shift+enter breaks the line", []string{"a", "<shift+enter>", "b"}, "a\nb", 3},
		{"alt+enter breaks the line", []string{"a", "<alt+enter>", "b"}, "a\nb", 3},
		{"ctrl+j breaks the line", []string{"a", "<ctrl+j>", "b"}, "a\nb", 3},
		{"backslash enter breaks the line", []string{"a\\", "<enter>", "b"}, "a\nb", 3},
		{"alt+letter types nothing", []string{"a", "<alt+x>"}, "a", 1},
		{"space", []string{"a b"}, "a b", 3},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := New()
			do(e, c.keys...)
			check(t, e, c.value, c.cursor)
		})
	}
}

func TestOutcomes(t *testing.T) {
	cases := []struct {
		name string
		pre  []string
		key  string
		want Outcome
	}{
		{"enter submits", []string{"hi"}, "<enter>", Submit},
		{"backslash enter does not", []string{"hi\\"}, "<enter>", Handled},
		{"ctrl+d on empty is the caller's", nil, "<ctrl+d>", Unhandled},
		{"ctrl+d with text is handled", []string{"a"}, "<ctrl+d>", Handled},
		{"tab", nil, "<tab>", Unhandled},
		{"esc without vim", []string{"a"}, "<esc>", Unhandled},
		{"ctrl+c", nil, "<ctrl+c>", Unhandled},
		{"ctrl+r", nil, "<ctrl+r>", Unhandled},
		{"ctrl+g", nil, "<ctrl+g>", Unhandled},
		{"ctrl+o", nil, "<ctrl+o>", Unhandled},
		{"ctrl+v", nil, "<ctrl+v>", Unhandled},
		{"ctrl+l", nil, "<ctrl+l>", Unhandled},
		{"shift+tab", nil, "<shift+tab>", Unhandled},
		{"pgup", nil, "<pgup>", Unhandled},
		{"up on one line walks the history", []string{"abc"}, "<up>", HistoryPrev},
		{"down on one line walks the history", []string{"abc"}, "<down>", HistoryNext},
		{"up on the second line moves", []string{"a", "<ctrl+j>", "b"}, "<up>", Handled},
		{"down on the first line moves", []string{"a", "<ctrl+j>", "b", "<up>"}, "<down>", Handled},
		{"down on the last line walks", []string{"a", "<ctrl+j>", "b"}, "<down>", HistoryNext},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := New()
			do(e, c.pre...)
			if got := do(e, c.key); got != c.want {
				t.Fatalf("outcome = %d, want %d", got, c.want)
			}
		})
	}
}

func TestYankPopCyclesTheRing(t *testing.T) {
	e := New()
	do(e, "alpha", "<ctrl+u>", "beta", "<ctrl+u>")
	check(t, e, "", 0)
	do(e, "<ctrl+y>")
	check(t, e, "beta", 4)
	do(e, "<alt+y>")
	check(t, e, "alpha", 5)
	do(e, "<alt+y>")
	check(t, e, "beta", 4)
	// One undo takes the yank and its pops back together.
	do(e, "<ctrl+_>")
	check(t, e, "", 0)
}

func TestYankPopNeedsAYank(t *testing.T) {
	e := New()
	do(e, "alpha", "<ctrl+u>", "x", "<alt+y>")
	check(t, e, "x", 1)
}

func TestUndoCoalescesTyping(t *testing.T) {
	e := New()
	do(e, "hello world")
	do(e, "<ctrl+_>")
	check(t, e, "", 0)

	e = New()
	do(e, "abc", "<left>", "d")
	check(t, e, "abdc", 3)
	do(e, "<ctrl+_>")
	check(t, e, "abc", 2)
	do(e, "<ctrl+_>")
	check(t, e, "", 0)

	e = New()
	do(e, "abc def", "<ctrl+w>")
	check(t, e, "abc ", 4)
	do(e, "<ctrl+_>")
	check(t, e, "abc def", 7)

	e = New()
	do(e, "ab")
	e.Paste("PASTE")
	do(e, "cd")
	check(t, e, "abPASTEcd", 9)
	e.Undo()
	check(t, e, "abPASTE", 7)
	e.Undo()
	check(t, e, "ab", 2)
}

func TestPastePlaceholders(t *testing.T) {
	e := New()
	e.Paste("short")
	check(t, e, "short", 5)

	e = New()
	do(e, "see ")
	e.Paste("one\r\ntwo\r\nthree\r\nfour")
	want := "see [Pasted text #1 +3 lines]"
	check(t, e, want, len([]rune(want)))
	if got := e.Expanded(); got != "see one\ntwo\nthree\nfour" {
		t.Fatalf("expanded = %q", got)
	}
	// Two newlines stay inline: the threshold is more than two.
	e2 := New()
	e2.Paste("a\nb\nc")
	check(t, e2, "a\nb\nc", 5)

	long := strings.Repeat("x", 801)
	e.Paste(long)
	if !strings.HasSuffix(e.Value(), "[Pasted text #2 +0 lines]") {
		t.Fatalf("long paste = %q", e.Value())
	}
	if got := e.Expanded(); got != "see one\ntwo\nthree\nfour"+long {
		t.Fatalf("expanded after two pastes is wrong")
	}
}

func TestPlaceholdersAreAtomic(t *testing.T) {
	e := New()
	do(e, "a ")
	e.Paste("1\n2\n3\n4")
	do(e, " b")
	value := "a [Pasted text #1 +3 lines] b"
	check(t, e, value, len([]rune(value)))

	do(e, "<left>", "<left>")
	tokenEnd := len([]rune("a [Pasted text #1 +3 lines]"))
	check(t, e, value, tokenEnd)
	do(e, "<left>")
	check(t, e, value, 2)
	do(e, "<right>")
	check(t, e, value, tokenEnd)
	do(e, "<backspace>")
	check(t, e, "a  b", 2)

	e = New()
	do(e, "x")
	e.InsertImage(1)
	do(e, "y")
	check(t, e, "x[Image #1]y", 12)
	do(e, "<home>", "<right>", "<delete>")
	check(t, e, "xy", 1)

	e.SetCursor(0)
	e.InsertImage(3)
	e.SetCursor(5)
	if e.Cursor() != len([]rune("[Image #3]")) {
		t.Fatalf("SetCursor inside a token = %d", e.Cursor())
	}
}

func TestImagesAreThoseStillReferenced(t *testing.T) {
	e := New()
	e.InsertImage(1)
	do(e, " and ")
	e.InsertImage(2)
	if got := e.Images(); !reflect.DeepEqual(got, []int{1, 2}) {
		t.Fatalf("images = %v", got)
	}
	do(e, "<backspace>")
	if got := e.Images(); !reflect.DeepEqual(got, []int{1}) {
		t.Fatalf("images after deleting = %v", got)
	}
	if got := e.Expanded(); got != "[Image #1] and " {
		t.Fatalf("expanded keeps image tokens: %q", got)
	}
}

func TestResetForgetsPastes(t *testing.T) {
	e := New()
	e.Paste("1\n2\n3\n4")
	e.Reset()
	if !e.Empty() {
		t.Fatal("reset left text")
	}
	e.Paste("5\n6\n7\n8")
	if e.Value() != "[Pasted text #1 +3 lines]" {
		t.Fatalf("paste numbering did not restart: %q", e.Value())
	}
}

func TestSetValueClearsUndo(t *testing.T) {
	e := New()
	do(e, "draft")
	e.SetValue("from history")
	check(t, e, "from history", 12)
	e.Undo()
	check(t, e, "from history", 12)
}

func TestWordBeforeCursorAndReplaceRange(t *testing.T) {
	e := New()
	do(e, "look at @src/ma")
	word, start := e.WordBeforeCursor()
	if word != "@src/ma" || start != 8 {
		t.Fatalf("word = %q at %d", word, start)
	}
	e.ReplaceRange(start, e.Cursor(), "@src/main.go ")
	check(t, e, "look at @src/main.go ", 21)
	e.Undo()
	check(t, e, "look at @src/ma", 15)

	e = New()
	do(e, "/com")
	word, start = e.WordBeforeCursor()
	if word != "/com" || start != 0 {
		t.Fatalf("word = %q at %d", word, start)
	}
	e = New()
	do(e, "a ")
	if word, start = e.WordBeforeCursor(); word != "" || start != 2 {
		t.Fatalf("after a space: %q at %d", word, start)
	}
}

func TestInsertText(t *testing.T) {
	e := New()
	e.InsertText("abc")
	e.InsertText("def")
	check(t, e, "abcdef", 6)
	e.Undo()
	check(t, e, "", 0)
}
