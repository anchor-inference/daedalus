package textedit

import "testing"

// normal returns an editor in vim normal mode holding value with the cursor at offset.
func normal(value string, cursor int) *Editor {
	e := New()
	e.SetVim(true)
	e.SetValue(value)
	e.vim.mode = vimNormal
	e.SetCursor(cursor)
	e.Render(80)
	return e
}

func TestVimModes(t *testing.T) {
	e := New()
	if e.VimMode() != "" {
		t.Fatal("vim off has no mode name")
	}
	e.SetVim(true)
	if e.VimMode() != "INSERT" {
		t.Fatalf("vim starts in %q", e.VimMode())
	}
	do(e, "abc")
	if do(e, "<esc>") != Handled || e.VimMode() != "NORMAL" {
		t.Fatal("esc should enter normal mode")
	}
	check(t, e, "abc", 2) // stepped back onto the last character typed
	if do(e, "<esc>") != Unhandled {
		t.Fatal("esc in normal mode with nothing pending belongs to the caller")
	}
	do(e, "d", "<esc>")
	if e.vim.op != "" {
		t.Fatal("esc should cancel a pending operator")
	}
	if do(e, "<enter>") != Submit {
		t.Fatal("enter in normal mode submits")
	}
	if do(e, "<ctrl+c>") != Unhandled {
		t.Fatal("ctrl+c is the caller's in normal mode")
	}
	do(e, "q") // an unbound letter is swallowed, not typed
	check(t, e, "abc", 2)
}

func TestVimCommands(t *testing.T) {
	cases := []struct {
		name   string
		value  string
		cursor int
		keys   []string
		want   string
		at     int
		mode   string
	}{
		{"h and l", "abcdef", 0, []string{"l", "l", "l", "h"}, "abcdef", 2, "NORMAL"},
		{"l stops on the last character", "abc", 0, []string{"9", "l"}, "abc", 2, "NORMAL"},
		{"w", "foo bar.baz qux", 0, []string{"w", "w"}, "foo bar.baz qux", 7, "NORMAL"},
		{"count w", "a b c d e", 0, []string{"3", "w"}, "a b c d e", 6, "NORMAL"},
		{"W skips punctuation", "foo bar.baz qux", 4, []string{"W"}, "foo bar.baz qux", 12, "NORMAL"},
		{"b", "foo bar baz", 10, []string{"b", "b"}, "foo bar baz", 4, "NORMAL"},
		{"e", "foo bar baz", 0, []string{"e", "e"}, "foo bar baz", 6, "NORMAL"},
		{"0 ^ $", "  indented", 5, []string{"0"}, "  indented", 0, "NORMAL"},
		{"^", "  indented", 7, []string{"^"}, "  indented", 2, "NORMAL"},
		{"$", "abc\ndef", 0, []string{"$"}, "abc\ndef", 2, "NORMAL"},
		{"gg", "a\nb\nc", 4, []string{"g", "g"}, "a\nb\nc", 0, "NORMAL"},
		{"G", "a\nb\nccc", 0, []string{"G"}, "a\nb\nccc", 4, "NORMAL"},
		{"2G", "a\nb\nc", 0, []string{"2", "G"}, "a\nb\nc", 2, "NORMAL"},
		{"10l uses a zero in the count", "abcdefghijklmnop", 0, []string{"1", "0", "l"}, "abcdefghijklmnop", 10, "NORMAL"},
		{"x", "abcdef", 1, []string{"x"}, "acdef", 1, "NORMAL"},
		{"3x", "abcdef", 1, []string{"3", "x"}, "aef", 1, "NORMAL"},
		{"x stops at the line end", "ab\ncd", 1, []string{"5", "x"}, "a\ncd", 0, "NORMAL"},
		{"X", "abcdef", 3, []string{"X"}, "abdef", 2, "NORMAL"},
		{"D", "hello world", 5, []string{"D"}, "hello", 4, "NORMAL"},
		{"C", "hello world", 5, []string{"C", "!"}, "hello!", 6, "INSERT"},
		{"dw", "foo bar baz", 0, []string{"d", "w"}, "bar baz", 0, "NORMAL"},
		{"2dw", "foo bar baz", 0, []string{"2", "d", "w"}, "baz", 0, "NORMAL"},
		{"d2w", "foo bar baz", 0, []string{"d", "2", "w"}, "baz", 0, "NORMAL"},
		{"dw on the last word stays on its line", "foo bar\nnext", 4, []string{"d", "w"}, "foo \nnext", 3, "NORMAL"},
		{"de", "foo bar", 0, []string{"d", "e"}, " bar", 0, "NORMAL"},
		{"db", "foo bar", 4, []string{"d", "b"}, "bar", 0, "NORMAL"},
		{"d$", "foo bar", 2, []string{"d", "$"}, "fo", 1, "NORMAL"},
		{"d0", "foo bar", 4, []string{"d", "0"}, "bar", 0, "NORMAL"},
		{"cw changes to the word's end", "foo bar", 0, []string{"c", "w", "baz"}, "baz bar", 3, "INSERT"},
		{"ce", "foo bar", 4, []string{"c", "e", "qux"}, "foo qux", 7, "INSERT"},
		{"dd", "one\ntwo\nthree", 4, []string{"d", "d"}, "one\nthree", 4, "NORMAL"},
		{"2dd", "one\ntwo\nthree", 0, []string{"2", "d", "d"}, "three", 0, "NORMAL"},
		{"dd on the last line", "one\ntwo", 5, []string{"d", "d"}, "one", 0, "NORMAL"},
		{"dd on the only line", "only", 1, []string{"d", "d"}, "", 0, "NORMAL"},
		{"dj", "a\nb\nc", 0, []string{"d", "j"}, "c", 0, "NORMAL"},
		{"dk", "a\nb\nc", 2, []string{"d", "k"}, "c", 0, "NORMAL"},
		{"cc", "one\n  two\nthree", 5, []string{"c", "c", "new"}, "one\nnew\nthree", 7, "INSERT"},
		{"yy p", "one\ntwo", 0, []string{"y", "y", "p"}, "one\none\ntwo", 4, "NORMAL"},
		{"yy P", "one\ntwo", 4, []string{"y", "y", "P"}, "one\ntwo\ntwo", 4, "NORMAL"},
		{"yw p", "foo bar", 0, []string{"y", "w", "$", "p"}, "foo barfoo ", 10, "NORMAL"},
		{"x then p swaps", "ab", 0, []string{"x", "p"}, "ba", 1, "NORMAL"},
		{"x then P", "abc", 1, []string{"x", "P"}, "abc", 1, "NORMAL"},
		{"3p", "ab", 0, []string{"x", "3", "p"}, "baaa", 3, "NORMAL"},
		{"i", "abc", 1, []string{"i", "X"}, "aXbc", 2, "INSERT"},
		{"a", "abc", 1, []string{"a", "X"}, "abXc", 3, "INSERT"},
		{"a at the end", "abc", 2, []string{"a", "X"}, "abcX", 4, "INSERT"},
		{"I", "  abc", 4, []string{"I", "X"}, "  Xabc", 3, "INSERT"},
		{"A", "abc\ndef", 0, []string{"A", "X"}, "abcX\ndef", 4, "INSERT"},
		{"o", "abc\ndef", 1, []string{"o", "X"}, "abc\nX\ndef", 5, "INSERT"},
		{"O", "abc\ndef", 5, []string{"O", "X"}, "abc\nX\ndef", 5, "INSERT"},
		{"r", "abc", 1, []string{"r", "X"}, "aXc", 1, "NORMAL"},
		{"3r", "abcd", 0, []string{"3", "r", "z"}, "zzzd", 2, "NORMAL"},
		{"r past the line end does nothing", "ab", 1, []string{"3", "r", "z"}, "ab", 1, "NORMAL"},
		{"~", "abC", 0, []string{"~", "~", "~"}, "ABc", 2, "NORMAL"},
		{"3~", "abc", 0, []string{"3", "~"}, "ABC", 2, "NORMAL"},
		{"arrows work in normal mode", "abc", 0, []string{"<right>", "<right>", "<left>"}, "abc", 1, "NORMAL"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := normal(c.value, c.cursor)
			do(e, c.keys...)
			check(t, e, c.want, c.at)
			if e.VimMode() != c.mode {
				t.Fatalf("mode = %s, want %s", e.VimMode(), c.mode)
			}
		})
	}
}

func TestVimUndoTakesAChangeAndItsTypingTogether(t *testing.T) {
	e := normal("foo bar", 0)
	do(e, "c", "w", "baz", "<esc>")
	check(t, e, "baz bar", 2)
	do(e, "u")
	check(t, e, "foo bar", 0)

	e = normal("abc", 2)
	do(e, "o", "line", "<esc>", "u")
	check(t, e, "abc", 2)

	e = normal("abc", 0)
	do(e, "x", "x", "u")
	check(t, e, "bc", 0)
	do(e, "2", "u")
	check(t, e, "abc", 0)
}

func TestVimHistoryEdges(t *testing.T) {
	e := normal("one line", 3)
	if do(e, "k") != HistoryPrev {
		t.Fatal("k on the first row walks the history")
	}
	if do(e, "j") != HistoryNext {
		t.Fatal("j on the last row walks the history")
	}
	e = normal("a\nb\nc", 0)
	if do(e, "j") != Handled {
		t.Fatal("j inside the text moves")
	}
	check(t, e, "a\nb\nc", 2)
	if do(e, "5", "j") != Handled {
		t.Fatal("a counted j that reaches the end still moves")
	}
	check(t, e, "a\nb\nc", 4)
}

func TestVimInsertModeKeepsReadlineKeys(t *testing.T) {
	e := New()
	e.SetVim(true)
	do(e, "hello world", "<ctrl+w>", "<ctrl+a>", ">")
	check(t, e, ">hello ", 1)
}

func TestVimResetReturnsToInsert(t *testing.T) {
	e := normal("abc", 0)
	e.Reset()
	if e.VimMode() != "INSERT" {
		t.Fatalf("after reset the mode is %s", e.VimMode())
	}
	e.SetVim(false)
	if e.Vim() || e.VimMode() != "" {
		t.Fatal("vim did not turn off")
	}
}
