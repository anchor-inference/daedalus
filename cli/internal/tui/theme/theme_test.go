package theme

import (
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"
)

func TestSpinnerBreathesForwardThenBack(t *testing.T) {
	var got []string
	for i := 0; i < 12; i++ {
		got = append(got, UnicodeGlyphs.SpinnerFrame(i))
	}
	want := "· ✢ ✳ ✶ ✻ ✽ ✻ ✶ ✳ ✢ · ✢"
	if strings.Join(got, " ") != want {
		t.Fatalf("frames %q, want %q", strings.Join(got, " "), want)
	}
}

// Every marker is one cell in both sets, except where a set spells a word: the layout is measured
// once and must not move when a terminal falls back to ASCII.
func TestMarkersAreOneCell(t *testing.T) {
	for name, g := range map[string]Glyphs{"unicode": UnicodeGlyphs, "ascii": ASCIIGlyphs} {
		for _, mark := range []string{g.Bullet, g.Result, g.Prompt, g.Pointer, g.Star, g.Separator, g.Rule, g.Corner} {
			if w := ansi.StringWidth(mark); w != 1 {
				t.Errorf("%s marker %q is %d cells", name, mark, w)
			}
		}
	}
}

func TestUnknownThemeFallsBackToTheBackground(t *testing.T) {
	if th := New(Options{Name: "from-the-future", Dark: false}); th.Name != "light" || th.Dark {
		t.Fatalf("got %s", th.Name)
	}
	if th := New(Options{Name: "", Dark: true}); th.Name != "dark" {
		t.Fatalf("got %s", th.Name)
	}
}

func TestPlainThemeDrawsNoColour(t *testing.T) {
	th := New(Options{Name: "dark", Plain: true})
	out := th.Fg(th.Palette.Accent).Render("x") + th.Bg(th.Palette.DiffAdd).Render("y") + th.Dim().Render("z")
	if strings.Contains(out, "\x1b[") {
		t.Fatalf("plain output carries escapes: %q", out)
	}
}

func TestSixteenColourThemeUsesTheTerminalForeground(t *testing.T) {
	th := New(Options{Name: "ansi", Dark: false})
	if th.Palette.Text != nil {
		t.Fatal("the ansi theme fixes a text colour, which vanishes on one of the two backgrounds")
	}
	if strings.Contains(th.Bold().Render("x"), "38;") {
		t.Fatal("bold text in the ansi theme carries a foreground colour")
	}
}
