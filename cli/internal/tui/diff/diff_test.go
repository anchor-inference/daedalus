package diff

import (
	"fmt"
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

func numbered(n int) string {
	var b strings.Builder
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&b, "line %d\n", i)
	}
	return b.String()
}

func ops(lines []Line) string {
	var b strings.Builder
	for _, line := range lines {
		switch line.Op {
		case Context:
			b.WriteByte('=')
		case Add:
			b.WriteByte('+')
		case Remove:
			b.WriteByte('-')
		case Elision:
			fmt.Fprintf(&b, "…%d", line.Elided)
		}
	}
	return b.String()
}

func TestLines(t *testing.T) {
	cases := []struct {
		name          string
		before, after string
		want          string
	}{
		{"identical", "a\nb\n", "a\nb\n", ""},
		{"trailing newline only", "a\nb", "a\nb\n", ""},
		{"insert", "a\nb\nc\n", "a\nb\nx\nc\n", "==+="},
		{"delete", "a\nb\nc\n", "a\nc\n", "=-="},
		{"change", "a\nb\nc\n", "a\nB\nc\n", "=-+="},
		{"new file", "", "a\nb\n", "++"},
		{"emptied", "a\nb\n", "", "--"},
		{"elision between hunks", strings.Replace(strings.Replace(numbered(20), "line 2\n", "two\n", 1), "line 18\n", "eighteen\n", 1), numbered(20), "=-+===…9===-+=="},
		{"elision at edges", numbered(20), strings.Replace(numbered(20), "line 10\n", "ten\n", 1), "…6===-+===…7"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := ops(Lines(c.before, c.after)); got != c.want {
				t.Fatalf("ops = %q, want %q", got, c.want)
			}
		})
	}
}

func TestNumbersFollowEachSide(t *testing.T) {
	lines := Lines("a\nb\nc\n", "a\nx\ny\nc\n")
	want := []Line{
		{Op: Context, Old: 1, New: 1, Text: "a"},
		{Op: Remove, Old: 2, Text: "b"},
		{Op: Add, New: 2, Text: "x"},
		{Op: Add, New: 3, Text: "y"},
		{Op: Context, Old: 3, New: 4, Text: "c"},
	}
	if fmt.Sprint(lines) != fmt.Sprint(want) {
		t.Fatalf("got %v\nwant %v", lines, want)
	}
	if added, removed := Count(lines); added != 2 || removed != 1 {
		t.Fatalf("count = +%d -%d", added, removed)
	}
}

func TestHugeChangeFallsBackToWholesale(t *testing.T) {
	var before, after strings.Builder
	for i := 0; i < 3000; i++ {
		fmt.Fprintf(&before, "old %d\n", i)
		fmt.Fprintf(&after, "new %d\n", i)
	}
	lines := Lines(before.String(), after.String())
	added, removed := Count(lines)
	if added != 3000 || removed != 3000 {
		t.Fatalf("count = +%d -%d", added, removed)
	}
	if lines[0].Op != Remove || lines[len(lines)-1].Op != Add {
		t.Fatalf("wholesale should remove the old block then add the new one")
	}
}

func TestFromEditNumbersFromOne(t *testing.T) {
	lines := FromEdit("foo()\n", "foo(bar)\n")
	if ops(lines) != "-+" || lines[0].Old != 1 || lines[1].New != 1 {
		t.Fatalf("got %v", lines)
	}
}

func darkTheme() *theme.Theme { return theme.New(theme.Options{Name: "dark"}) }

func TestRenderShape(t *testing.T) {
	lines := Lines("one\ntwo\nthree\n", "one\nTWO\nthree\n")
	rows := Render(darkTheme(), lines, 30, 0)
	plain := make([]string, len(rows))
	for i, row := range rows {
		plain[i] = ansi.Strip(row)
		if w := ansi.StringWidth(row); w > 30 {
			t.Fatalf("row %d is %d wide", i, w)
		}
	}
	want := []string{
		"1   one",
		"2 - two" + strings.Repeat(" ", 23),
		"2 + TWO" + strings.Repeat(" ", 23),
		"3   three",
	}
	if strings.Join(plain, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got\n%q\nwant\n%q", plain, want)
	}
}

func TestRenderTintsAndMarksWords(t *testing.T) {
	lines := Lines("x := compute(a, b)\n", "x := compute(a, c)\n")
	th := darkTheme()
	rows := Render(th, lines, 40, 0)
	if !strings.Contains(rows[0], "48;2;") || !strings.Contains(rows[1], "48;2;") {
		t.Fatalf("changed rows carry no background: %q", rows)
	}
	// The removed row holds three background runs at least: gutter, unchanged text, the changed word.
	if strings.Count(rows[0], "48;2;") < 3 {
		t.Fatalf("expected a word highlight inside the removed row: %q", rows[0])
	}
	if !strings.Contains(ansi.Strip(rows[1]), "x := compute(a, c)") {
		t.Fatalf("text lost: %q", ansi.Strip(rows[1]))
	}
}

func TestUnrelatedReplacementGetsNoWordMarks(t *testing.T) {
	marks := pairWords(Lines("alpha beta gamma\n", "completely different words here\n"))
	for _, m := range marks {
		if m != nil {
			t.Fatalf("unrelated lines should not be word-marked: %v", marks)
		}
	}
}

func TestRenderCutsLongLinesAndRows(t *testing.T) {
	th := darkTheme()
	long := strings.Repeat("abcdefghij", 10)
	rows := Render(th, Lines("", long+"\n"), 20, 0)
	if got := ansi.Strip(rows[0]); !strings.HasSuffix(got, "…") || ansi.StringWidth(got) != 20 {
		t.Fatalf("long line not cut to width: %q", got)
	}
	rows = Render(th, Lines("", numbered(10)), 40, 4)
	if len(rows) != 5 {
		t.Fatalf("want 4 rows and a tail, got %d", len(rows))
	}
	if got := strings.TrimSpace(ansi.Strip(rows[4])); got != "… +6 lines" {
		t.Fatalf("tail = %q", got)
	}
}

func TestRenderExpandsTabsAndElides(t *testing.T) {
	th := darkTheme()
	rows := Render(th, Lines("\tif x {\n", "\tif y {\n"), 40, 0)
	if !strings.Contains(ansi.Strip(rows[0]), "    if x {") {
		t.Fatalf("tab not expanded: %q", ansi.Strip(rows[0]))
	}
	before := strings.Replace(strings.Replace(numbered(20), "line 2\n", "two\n", 1), "line 18\n", "eighteen\n", 1)
	rows = Render(th, Lines(before, numbered(20)), 40, 0)
	found := false
	for _, row := range rows {
		if strings.TrimSpace(ansi.Strip(row)) == "…" {
			found = true
		}
	}
	if !found {
		t.Fatalf("no elision row in %q", rows)
	}
}

func TestRenderPlainAndASCII(t *testing.T) {
	th := theme.New(theme.Options{Name: "dark", Plain: true, ASCII: true})
	rows := Render(th, Lines("a\n", "b\n"), 20, 0)
	for _, row := range rows {
		if strings.Contains(row, "\x1b[") {
			t.Fatalf("plain theme drew colour: %q", row)
		}
	}
	if !strings.HasPrefix(rows[0], "1 - a") || !strings.HasPrefix(rows[1], "1 + b") {
		t.Fatalf("got %q", rows)
	}
	rows = Render(th, Lines("", numbered(5)), 20, 2)
	if strings.TrimSpace(rows[2]) != "... +3 lines" {
		t.Fatalf("ascii tail = %q", rows[2])
	}
}
