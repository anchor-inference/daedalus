package textedit

import (
	"reflect"
	"strings"
	"testing"
)

func TestRenderWraps(t *testing.T) {
	cases := []struct {
		name     string
		value    string
		cursor   int
		width    int
		lines    []string
		row, col int
	}{
		{"empty is one row", "", 0, 10, []string{""}, 0, 0},
		{"fits", "hello", 5, 10, []string{"hello"}, 0, 5},
		{"breaks after a space", "hello world foo", 15, 10, []string{"hello ", "world foo"}, 1, 9},
		{"hard-breaks a long word", "abcdefghijkl", 3, 5, []string{"abcde", "fghij", "kl"}, 0, 3},
		{"cursor at the end of a full row moves to a new row", "abcde", 5, 5, []string{"abcde", ""}, 1, 0},
		{"cursor at a wrap point is on the next row", "abcdefgh", 5, 5, []string{"abcde", "fgh"}, 1, 0},
		{"newlines are rows", "a\nb\n\nc", 4, 10, []string{"a", "b", "", "c"}, 2, 0},
		{"trailing newline gives an empty row", "a\n", 2, 10, []string{"a", ""}, 1, 0},
		{"wide characters take two cells", "你好你好你好", 3, 5, []string{"你好", "你好", "你好"}, 1, 2},
		{"wide character does not split a cell", "a你好", 3, 4, []string{"a你", "好"}, 1, 2},
		{"tab is drawn as a space", "a\tb", 3, 10, []string{"a b"}, 0, 3},
		{"zero width is treated as one", "ab", 2, 0, []string{"a", "b", ""}, 2, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := New()
			e.SetValue(c.value)
			e.SetCursor(c.cursor)
			lines, row, col := e.Render(c.width)
			if !reflect.DeepEqual(lines, c.lines) {
				t.Fatalf("lines = %q, want %q", lines, c.lines)
			}
			if row != c.row || col != c.col {
				t.Fatalf("cursor = (%d,%d), want (%d,%d)", row, col, c.row, c.col)
			}
			if e.LinesFor(c.width) != len(c.lines) {
				t.Fatalf("LinesFor = %d", e.LinesFor(c.width))
			}
		})
	}
}

func TestNoRowIsWiderThanTheWidth(t *testing.T) {
	text := strings.Repeat("lorem ipsum 你好 dolor-sit-amet-consectetur ", 20)
	for width := 1; width < 40; width++ {
		e := New()
		e.SetValue(text)
		lines, _, col := e.Render(width)
		for _, line := range lines {
			w := 0
			for _, r := range line {
				w += runeWidth(r)
			}
			// A single wide character in a one-cell row is the only allowed overflow: it cannot be split.
			if w > width && !(width == 1 && w == 2) {
				t.Fatalf("width %d: row %q is %d cells", width, line, w)
			}
		}
		if col >= width && width > 1 {
			t.Fatalf("width %d: cursor column %d", width, col)
		}
	}
}

func TestVerticalMovesKeepTheGoalColumn(t *testing.T) {
	e := New()
	e.SetValue("abcdef\nab\nabcdef")
	e.Render(80)
	e.SetCursor(5) // row 0, column 5
	if do(e, "<down>") != Handled {
		t.Fatal("down did not move")
	}
	check(t, e, "abcdef\nab\nabcdef", 9) // end of "ab"
	do(e, "<down>")
	check(t, e, "abcdef\nab\nabcdef", 15) // column 5 again, not column 2
	if do(e, "<down>") != HistoryNext {
		t.Fatal("down on the last row should walk the history")
	}
	do(e, "<up>", "<up>")
	check(t, e, "abcdef\nab\nabcdef", 5)
	if do(e, "<up>") != HistoryPrev {
		t.Fatal("up on the first row should walk the history")
	}
}

func TestVerticalMovesFollowWrappedRows(t *testing.T) {
	e := New()
	e.SetValue("hello world foo")
	e.Render(10) // "hello " / "world foo"
	e.SetCursor(13)
	if do(e, "<up>") != Handled {
		t.Fatal("up inside a wrapped line should move")
	}
	// Column 7 on a row of six cells lands on its last character, not on the next row's first.
	check(t, e, "hello world foo", 5)
	if do(e, "<up>") != HistoryPrev {
		t.Fatal("up on the first wrapped row should walk the history")
	}
}

func TestUpFromTheVirtualRow(t *testing.T) {
	e := New()
	e.SetValue("abcde")
	lines, row, _ := e.Render(5)
	if len(lines) != 2 || row != 1 {
		t.Fatalf("setup: %q row %d", lines, row)
	}
	if do(e, "<down>") != HistoryNext {
		t.Fatal("down from the virtual row should walk the history")
	}
	if do(e, "<up>") != Handled {
		t.Fatal("up from the virtual row should move onto the full row")
	}
	check(t, e, "abcde", 0)
}

func TestVerticalMoveSkipsOutOfTokens(t *testing.T) {
	e := New()
	e.SetValue("0123456789\n")
	e.SetCursor(len(e.buf))
	e.InsertImage(1)
	e.Render(80)
	e.SetCursor(4)
	do(e, "<down>")
	if e.Cursor() != len([]rune("0123456789\n[Image #1]")) {
		t.Fatalf("cursor landed inside the token: %d", e.Cursor())
	}
}
