package markdown

import (
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/theme"
)

// These mirror the host's tests of the Telegram flattening (tests/unit/test_telegram_render.py):
// the terminal and the chat must read a rich answer the same way.
func TestFlattenEvidenceTags(t *testing.T) {
	cases := map[string]string{
		`fixed in <file path="daedalus/tools/web.py" lines="20-40"/>.`:                      "fixed in daedalus/tools/web.py:20-40.",
		`see <file path="notes.md" />`:                                                      "see notes.md",
		`<run id="v12" label="14 passed"/> now`:                                             "14 passed (run v12) now",
		`<run id="job-3-1"/>`:                                                               "run job-3-1",
		"a <b>bold</b> tag and <file/> stay out of it":                                      "a <b>bold</b> tag and <file/> stay out of it",
		"before\n\n![screen](daedalus-media:11111111-1111-1111-1111-111111111111)\n\nafter": "before\n\nafter",
	}
	for in, want := range cases {
		if got := Flatten(in); got != want {
			t.Errorf("Flatten(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestFlattenCardsAndCitations(t *testing.T) {
	answer := "Pick:\n\n:::card\ntitle: 1. Pantum P2500NW\nurl: https://kaspi.kz/p/1\nimage: https://img.example/p.jpg\nbadge: My pick\n" +
		"meta: ≈ 45 000 ₸ · mono laser\n- 22 pages a minute\nlinks: [Kaspi](https://kaspi.kz/p/1) · [DNS](https://dns.example/p)\n" +
		"note: check delivery\n:::\n:::entity\ntitle: Anchor Inference\nurl: https://example.org\nmeta: AI systems · Astana\n:::\n\n" +
		"Order it.[^](https://kaspi.kz/p/1) [^DNS](https://www.dns-shop.kz/x) Quoted: `[^](https://a.example)`"
	flat := Flatten(answer)
	for _, absent := range []string{":::", "image:", "title:"} {
		if strings.Contains(flat, absent) {
			t.Errorf("flattened answer still holds %q:\n%s", absent, flat)
		}
	}
	for _, present := range []string{
		"**[1. Pantum P2500NW](https://kaspi.kz/p/1)** · _My pick_\n≈ 45 000 ₸ · mono laser\n- 22 pages a minute\n",
		"[Kaspi](https://kaspi.kz/p/1) · [DNS](https://dns.example/p)\n_check delivery_",
		"### [Anchor Inference](https://example.org)\nAI systems · Astana",
		"Order it. ([kaspi.kz](https://kaspi.kz/p/1), [DNS](https://www.dns-shop.kz/x))",
		"`[^](https://a.example)`",
	} {
		if !strings.Contains(flat, present) {
			t.Errorf("flattened answer lacks %q:\n%s", present, flat)
		}
	}
	quoted := "```\n:::card\ntitle: x\n:::\n```"
	if got := Flatten(quoted); got != quoted {
		t.Errorf("a quoted card was flattened: %q", got)
	}
}

func TestFlattenFlow(t *testing.T) {
	answer := "The plan:\n\n:::flow 💡 Pilot project\n💬 Customer — writes in the chat\n\n🤖 ==AI assistant== — answers\n" +
		"↓ when it cannot answer\n✅ Solved -> 🎧 Operator | 📊 Report\nv\nDone\nnote: Two weeks\n:::\nAfter."
	want := "The plan:\n\n**💡 Pilot project**\n💬 Customer — writes in the chat\n↓\n🤖 **AI assistant** — answers\n" +
		"↓ when it cannot answer\n✅ Solved → 🎧 Operator · 📊 Report\n↓\nDone\n_Two weeks_\n\nAfter."
	if got := Flatten(answer); got != want {
		t.Fatalf("got\n%q\nwant\n%q", got, want)
	}
	for _, kept := range []string{"```mermaid\nflowchart TD\n  A --> B\n```", "```\n:::flow\nA -> B\n:::\n```"} {
		if got := Flatten(kept); got != kept {
			t.Errorf("code was flattened: %q", got)
		}
	}
}

func TestStablePrefix(t *testing.T) {
	cases := []struct {
		name, text, stable string
	}{
		{"one open paragraph", "Hello wor", ""},
		{"finished paragraph", "First.\n\nSecond", "First.\n\n"},
		{"blank line not yet ended", "First.\n", ""},
		{"inside a fence", "Intro.\n\n```go\nfunc a() {\n\n\treturn\n", "Intro.\n\n"},
		{"after a closed fence", "```\na\n\nb\n```\n\nNext", "```\na\n\nb\n```\n\n"},
		{"a longer fence closes only on its own length", "````\n```\n\nstill code\n", ""},
		{"tilde fence", "~~~\nx\n\ny\n~~~\n\nz", "~~~\nx\n\ny\n~~~\n\n"},
		{"inside a card", "Pick:\n\n:::card\ntitle: x\n\nbody\n", "Pick:\n\n"},
		{"after a card", ":::card\ntitle: x\n\nbody\n:::\n\nNext", ":::card\ntitle: x\n\nbody\n:::\n\n"},
		{"several blocks", "a\n\nb\n\nc", "a\n\nb\n\n"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			stable, open := StablePrefix(c.text)
			if stable != c.stable || stable+open != c.text {
				t.Fatalf("StablePrefix(%q) = %q + %q, want stable %q", c.text, stable, open, c.stable)
			}
		})
	}
}

func darkRenderer() *Renderer { return New(theme.New(theme.Options{Name: "dark"})) }

func TestRenderShape(t *testing.T) {
	r := darkRenderer()
	src := "# Title\n\nSome **bold** text that is long enough to wrap at a narrow width for sure.\n\n- one\n- two\n\n```go\nfunc main() {\n\tprintln(1)\n}\n```\n"
	out := r.Render(src, 30)
	lines := strings.Split(out, "\n")
	if strings.TrimSpace(ansi.Strip(lines[0])) == "" || strings.TrimSpace(ansi.Strip(lines[len(lines)-1])) == "" {
		t.Fatalf("leading or trailing blank line:\n%s", out)
	}
	for _, line := range lines {
		plain := ansi.Strip(line)
		if ansi.StringWidth(line) > 30 {
			t.Errorf("line wider than 30: %q", plain)
		}
		if plain != strings.TrimRight(plain, " ") {
			t.Errorf("trailing spaces left: %q", plain)
		}
		if strings.Contains(line, "\t") {
			t.Errorf("tab left in output: %q", plain)
		}
	}
	plain := ansi.Strip(out)
	if strings.Contains(plain, "# Title") || !strings.HasPrefix(plain, "Title") {
		t.Errorf("heading marker shown or margin added:\n%s", plain)
	}
	if !strings.Contains(plain, "- one") || !strings.Contains(plain, "func main() {") || !strings.Contains(plain, "    println(1)") {
		t.Errorf("structure lost:\n%s", plain)
	}
	if strings.Contains(plain, "\n\n\n") {
		t.Errorf("double blank line:\n%s", plain)
	}
}

func TestRenderFlattensRichSyntax(t *testing.T) {
	out := ansi.Strip(darkRenderer().Render("See <file path=\"a/b.go\" lines=\"3-4\"/>.\n\n:::card\ntitle: Thing\nmeta: cheap\n:::\n", 60))
	if strings.Contains(out, "<file") || strings.Contains(out, ":::") || !strings.Contains(out, "a/b.go:3-4") || !strings.Contains(out, "Thing") {
		t.Fatalf("rich syntax not flattened:\n%s", out)
	}
}

func TestRenderPlainHasNoColour(t *testing.T) {
	r := New(theme.New(theme.Options{Name: "dark", Plain: true}))
	out := r.Render("Text with `code` and\n\n```go\nx := 1\n```\n", 40)
	for _, colour := range []string{"38;2;", "38;5;", "48;"} {
		if strings.Contains(out, colour) {
			t.Fatalf("plain theme drew colour %q: %q", colour, out)
		}
	}
	if !strings.Contains(ansi.Strip(out), "x := 1") {
		t.Fatalf("code lost: %q", out)
	}
}

func TestStreamRendersFinishedBlocksOnce(t *testing.T) {
	r := darkRenderer()
	s := NewStream(r)
	for _, piece := range []string{"First para", "graph.\n\nSecond ", "paragraph.\n\nThird"} {
		s.Append(piece)
		s.Render(40)
	}
	// Three appends, three frames: each frame renders the open tail once, and each of the two
	// finished paragraphs exactly once in total.
	before := r.renders.Load()
	lines := s.Render(40)
	if got := r.renders.Load() - before; got != 1 {
		t.Fatalf("a frame re-rendered %d blocks, want only the open tail", got)
	}
	want := "First paragraph.\n\nSecond paragraph.\n\nThird"
	if got := ansi.Strip(strings.Join(lines, "\n")); got != want {
		t.Fatalf("got %q want %q", got, want)
	}
	if s.Text() != "First paragraph.\n\nSecond paragraph.\n\nThird" {
		t.Fatalf("text = %q", s.Text())
	}
	// A new width re-renders the finished blocks once more, and only then.
	before = r.renders.Load()
	s.Render(20)
	if got := r.renders.Load() - before; got != 3 {
		t.Fatalf("width change rendered %d times, want 3", got)
	}
	s.Reset()
	if len(s.Render(40)) != 0 || s.Text() != "" {
		t.Fatalf("reset left content")
	}
}

func TestStreamMatchesWholeRender(t *testing.T) {
	r := darkRenderer()
	src := "Intro line.\n\n```py\nprint(1)\n\nprint(2)\n```\n\n1. a\n2. b\n\nEnd."
	s := NewStream(r)
	for _, ch := range strings.SplitAfter(src, " ") {
		s.Append(ch)
	}
	got := ansi.Strip(strings.Join(s.Render(50), "\n"))
	want := ansi.Strip(r.Render(src, 50))
	if got != want {
		t.Fatalf("stream differs from whole render:\n%q\n%q", got, want)
	}
}
