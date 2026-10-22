package scan

import (
	"bytes"
	"math/rand"
	"reflect"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/ptyd/internal/scan/scantest"
)

// scanAll runs the whole input through one scanner in the given chunks and returns the joined
// output and the marks with offsets into it.
func scanAll(chunks ...[]byte) ([]byte, []Mark) {
	s := New()
	var out []byte
	var marks []Mark
	for _, c := range chunks {
		o, ms := s.Scan(c)
		for _, m := range ms {
			m.Offset += len(out)
			marks = append(marks, m)
		}
		out = append(out, o...)
	}
	return out, marks
}

// corpus is one of every sequence the scanner treats specially, plus text around them.
var corpus = []string{
	"plain text, ünïcödé and 日本語\r\n",
	"\x1b]0;my title\x07",
	"\x1b]2;other title\x1b\\",
	"\xc2\x9d2;c1 title\xc2\x9c",
	"\x1b]7;file://host/home/someone/my%20dir\x07",
	"\x1b]133;A\x07$ \x1b]133;B\x07ls\r\n\x1b]133;C\x07out\r\n\x1b]133;D;2;aid=7\x07",
	"\x1b]633;E;echo a\\x3bb\\\\c;nonce1\x07",
	"\x1b]9;build finished\x07",
	"\x1b]9;4;1;45\x07",
	"\x1b]9;9;/tmp\x07",
	"\x1b]777;notify;Title here;the body\x1b\\",
	"\x07",
	"\x1b[?1049h\x1b[?2004;1000h\x1b[?1049l",
	"\x1b[c\x1b[>c\x1b[>q\x1b[6n\x1b[?6n\x1b[5n",
	"\x1b[?2004$p\x1b[4$p\x1b[?u\x1b[>5u\x1b[<2u\x1b[=3;2u",
	"\x1bP$qm\x1b\\",
	"\x1b]11;?\x07",
	"\x1b[1;31mred\x1b[0m\x1b[38;2;255;128;0mrgb\x1b[m",
	"\x1bc\x1b[!p\x1b=\x1b>",
	"\x1b(B\x1b)0\x1b#8",
	"\x1b[99999999b\x1b[0000000000012A",
	"\x1b[1\n2m",
	"\xc2\x9b99999b",
	"\x1b]0;unterminated then ESC\x1b[m",
	"\x1b[1;2\x18abc\x1b]0;x\x1a",
	"\x1b_Gkitty=1\x1b\\\x1b^pm\x1b\\\x1bXsos\x1b\\",
	"\xc2\xa0nbsp\xc2",
	"\xc2x\x1b",
}

func TestSplitAnywhereGivesTheSameResult(t *testing.T) {
	whole := []byte(strings.Join(corpus, ""))
	wantOut, wantMarks := scanAll(whole)
	for i := 0; i <= len(whole); i++ {
		out, marks := scanAll(whole[:i], whole[i:])
		if !bytes.Equal(out, wantOut) {
			t.Fatalf("split at %d: output differs\n got %q\nwant %q", i, out, wantOut)
		}
		if !reflect.DeepEqual(marks, wantMarks) {
			t.Fatalf("split at %d: marks differ\n got %+v\nwant %+v", i, marks, wantMarks)
		}
	}
	// And byte by byte.
	chunks := make([][]byte, len(whole))
	for i := range whole {
		chunks[i] = whole[i : i+1]
	}
	out, marks := scanAll(chunks...)
	if !bytes.Equal(out, wantOut) || !reflect.DeepEqual(marks, wantMarks) {
		t.Fatal("byte-by-byte scan differs")
	}
}

func TestOrdinarySequencesPassUnchanged(t *testing.T) {
	for _, c := range []string{
		"hello\r\n", "\x1b[1;31mred\x1b[0m", "\x1b]0;title\x07", "\x1b]2;t\x1b\\", "\x1b[?1049h",
		"\x1b(B", "\x1b[38;2;255;128;0m", "\x1b[10000b", "\x1bP$qm\x1b\\", "\x1b]8;id=1;https://x.test/\x1b\\link\x1b]8;;\x1b\\",
		"日本語 ✅ \xf0\x9f\x91\xa8‍\xf0\x9f\x91\xa9", "\x1b[3;5H\x1b[2J\x1b[K",
	} {
		if out, _ := scanAll([]byte(c)); string(out) != c {
			t.Errorf("%q became %q", c, out)
		}
	}
}

func TestClamps(t *testing.T) {
	cases := []struct{ in, want string }{
		{"x\x1b[1000000000b", "x\x1b[10000b"},
		{"\x1b[10001L", "\x1b[10000L"},
		{"\x1b[" + strings.Repeat("9", 5000) + "C", "\x1b[10000C"},
		{"\x1b[0000000000012A", "\x1b[12A"},
		{"\x1b[4000000000;4000000000H", "\x1b[10000;10000H"},
		{"\xc2\x9b99999b", "\xc2\x9b10000b"},
		// A line feed inside the sequence is executed first; the digits around it stay one parameter.
		{"\x1b[99\n9999b", "\n\x1b[10000b"},
	}
	for _, c := range cases {
		if out, _ := scanAll([]byte(c.in)); string(out) != c.want {
			t.Errorf("%q: got %q, want %q", c.in[:min(len(c.in), 30)], out, c.want)
		}
	}

	// Sequences interrupted by one that is then dropped must not leave half of themselves for the
	// digits that follow to continue. Each of these once passed an unclamped parameter through.
	for _, in := range []string{
		"\xc2\x9b100\xc2\x9b\xdaA01b",
		"\x1b\xc2\x9b\xdaA[99999999b",
		"\x1b[100\x1b[\xdaA01b",
		"\xc2\xc2\x9b\xdaA\x9b10001b",
		"\x1b\x1b[\xf3A[10001b",
		"\x1b\xc2\xc2\x9b\xdaA\x9b10001b",
		"\x1b]0;" + strings.Repeat("a", MaxString-10) + "\x1b[\xdaA" + strings.Repeat("b", MaxString) + "\x07",
	} {
		if out, _ := scanAll([]byte(in)); scantest.Verify(out) != nil {
			t.Errorf("%q: %v", in[:min(len(in), 30)], scantest.Verify(out))
		}
	}

	// At most MaxFields parameters survive.
	sgr := "\x1b[" + strings.Repeat("1;", 20000) + "1m"
	out, _ := scanAll([]byte(sgr))
	if n := bytes.Count(out, []byte(";")) + 1; n != MaxFields || !bytes.HasSuffix(out, []byte("m")) {
		t.Errorf("20000-parameter SGR kept %d fields: %q", n, out[:min(len(out), 80)])
	}

	// A string over MaxString is dropped whole, and what follows it survives.
	for _, intro := range []string{"\x1b]0;", "\x1bP", "\x1b_", "\x1b^", "\x1bX", "\xc2\x9d0;"} {
		in := intro + strings.Repeat("a", MaxString+10) + "\x1b\\after"
		out, marks := scanAll([]byte(in))
		if string(out) != "after" || len(marks) != 0 {
			t.Errorf("%q: oversized string produced %q (%d marks)", intro, out[:min(len(out), 40)], len(marks))
		}
	}
	// One of exactly MaxString bytes is kept.
	in := "\x1b]52;c;" + strings.Repeat("a", MaxString-5) + "\x07"
	if out, _ := scanAll([]byte(in)); string(out) != in {
		t.Errorf("a string of exactly the limit was altered (%d -> %d bytes)", len(in), len(out))
	}

	// A long title is cut to MaxTitle at a character boundary.
	title := strings.Repeat("я", MaxTitle) // two bytes each
	out, marks := scanAll([]byte("\x1b]2;" + title + "\x07"))
	if len(marks) != 1 || len(marks[0].Text) > MaxTitle || len(marks[0].Text) < MaxTitle-1 {
		t.Fatalf("title mark %d bytes", len(marks[0].Text))
	}
	if !bytes.HasPrefix(out, []byte("\x1b]2;я")) || !bytes.HasSuffix(out, []byte("я\x07")) {
		t.Errorf("title not cut at a character boundary")
	}
}

func TestMarks(t *testing.T) {
	type want struct {
		in string
		m  Mark
	}
	exit2 := Mark{Kind: KindPrompt, Letter: 'D', HasExit: true, Exit: 2, Params: map[string]string{"aid": "7"}}
	cases := []want{
		{"\x1b]0;hello\x07", Mark{Kind: KindTitle, Text: "hello"}},
		{"\x1b]2;hi\x1b\\", Mark{Kind: KindTitle, Text: "hi"}},
		{"\x1b]7;file://box/home/someone/my%20dir\x07", Mark{Kind: KindCwd, Text: "/home/someone/my dir"}},
		{"\x1b]7;kitty-shell-cwd://box/tmp\x07", Mark{Kind: KindCwd, Text: "/tmp"}},
		{"\x1b]133;A\x07", Mark{Kind: KindPrompt, Letter: 'A'}},
		{"\x1b]133;D;2;aid=7\x07", exit2},
		{"\x1b]633;E;echo a\\x3bb\\\\c;nonce1\x07", Mark{Kind: KindVscode, Letter: 'E', Text: `echo a;b\c`}},
		{"\x1b]633;P;Cwd=/x\\x3by\x07", Mark{Kind: KindVscode, Letter: 'P', Params: map[string]string{"Cwd": "/x;y"}}},
		{"\x1b]9;done\x07", Mark{Kind: KindNotify, Text: "done"}},
		{"\x1b]777;notify;T;B;C\x07", Mark{Kind: KindNotify, Title: "T", Text: "B;C"}},
		{"\x1b]9;4;1;45\x07", Mark{Kind: KindProgress, State: 1, Value: 45}},
		{"\x1b]9;4;0\x07", Mark{Kind: KindProgress, State: 0}},
		{"\x07", Mark{Kind: KindBell}},
		{"\x1b[?2004h", Mark{Kind: KindMode, Mode: 2004, Set: true, Private: true}},
		{"\x1b[?1l", Mark{Kind: KindMode, Mode: 1, Set: false, Private: true}},
		{"\x1b=", Mark{Kind: KindMode, Mode: 66, Set: true, Private: true}},
		{"\x1b[>5u", Mark{Kind: KindKitty, Op: '>', Flags: 5}},
		{"\x1b[<u", Mark{Kind: KindKitty, Op: '<', Value: 1}},
		{"\x1b[=3;2u", Mark{Kind: KindKitty, Op: '=', Flags: 3, Value: 2}},
		{"\x1bc", Mark{Kind: KindReset, Letter: 'c'}},
		{"\x1b[!p", Mark{Kind: KindReset, Letter: 'p'}},
		{"\x1b[c", Mark{Kind: KindQuery, Query: QueryDA1, Raw: []byte("\x1b[c")}},
		{"\x1b[0c", Mark{Kind: KindQuery, Query: QueryDA1, Raw: []byte("\x1b[0c")}},
		{"\x1b[>c", Mark{Kind: KindQuery, Query: QueryDA2, Raw: []byte("\x1b[>c")}},
		{"\x1b[>q", Mark{Kind: KindQuery, Query: QueryXTVERSION, Raw: []byte("\x1b[>q")}},
		{"\x1b[6n", Mark{Kind: KindQuery, Query: QueryDSR, Mode: 6, Raw: []byte("\x1b[6n")}},
		{"\x1b[?6n", Mark{Kind: KindQuery, Query: QueryDSR, Mode: 6, Private: true, Raw: []byte("\x1b[?6n")}},
		{"\x1b[?2004$p", Mark{Kind: KindQuery, Query: QueryDECRQM, Mode: 2004, Private: true, Raw: []byte("\x1b[?2004$p")}},
		{"\x1b[?u", Mark{Kind: KindQuery, Query: QueryKitty, Raw: []byte("\x1b[?u")}},
		{"\x1bP$qm\x1b\\", Mark{Kind: KindQuery, Query: QueryDECRQSS, Text: "m", Raw: []byte("\x1bP$qm\x1b\\")}},
		{"\x1b]11;?\x07", Mark{Kind: KindQuery, Query: QueryOSCColor, Mode: 11, Value: 1, Text: "?", Raw: []byte("11;?")}},
		{"\x1b]10;?;?\x07", Mark{Kind: KindQuery, Query: QueryOSCColor, Mode: 10, Value: 2, Text: "?;?", Raw: []byte("10;?;?")}},
		{"\x1b]4;1;?;2;?\x07", Mark{Kind: KindQuery, Query: QueryOSCColor, Mode: 4, Value: 2, Text: "1;?;2;?", Raw: []byte("4;1;?;2;?")}},
		{"\x1b[14t", Mark{Kind: KindQuery, Query: QueryXTWINOPS, Mode: 14, Raw: []byte("\x1b[14t")}},
		{"\x1b[18;0t", Mark{Kind: KindQuery, Query: QueryXTWINOPS, Mode: 18, Raw: []byte("\x1b[18;0t")}},
		{"\x1b[?996n", Mark{Kind: KindQuery, Query: QueryDSR, Mode: 996, Private: true, Raw: []byte("\x1b[?996n")}},
	}
	for _, c := range cases {
		out, marks := scanAll([]byte(c.in))
		c.m.Offset = len(out)
		if len(marks) != 1 || !reflect.DeepEqual(marks[0], c.m) {
			t.Errorf("%q: marks %+v, want %+v", c.in, marks, c.m)
		}
	}

	// ConEmu subcommands and OSC 1 are not reported; neither is a BEL that ends an OSC.
	for _, in := range []string{"\x1b]9;9;/tmp\x07", "\x1b]1;icon\x07", "\x1b]52;c;aGk=\x07",
		"\x1b]10;#fff;?\x07", "\x1b]4;1;#000\x07", "\x1b[22;0t", "\x1b[8;24;80t"} {
		if _, marks := scanAll([]byte(in)); len(marks) != 0 {
			t.Errorf("%q produced %+v", in, marks)
		}
	}

	// Several modes in one sequence give one mark each, all at the sequence's end.
	out, marks := scanAll([]byte("ab\x1b[?1049;2004hcd"))
	if len(marks) != 2 || marks[0].Mode != 1049 || marks[1].Mode != 2004 || marks[0].Offset != 2+len("\x1b[?1049;2004h") {
		t.Errorf("multi-mode: %+v in %q", marks, out)
	}
}

func TestStrip(t *testing.T) {
	cases := []struct{ in, want string }{
		{"\x1b[1;31mred\x1b[0m plain\r\n", "red plain\n"},
		{"\x1b]0;title\x07after", "after"},
		{"line\rover\r\n", "over\n"},
		{"get 10%\rget 50%\rget 100%\ndone\n", "get 100%\ndone\n"},
		{"a\nspin |\rspin /\r", "a\nspin /\n"},
		{"a\tb\x08c\x07d", "a\tbcd"},
		{"\x1b[?2004hprompt$ ", "prompt$ "},
		{"trailing\r", "trailing\n"},
		{"\xc2\x9b1mX", "X"},
		{"ünï", "ünï"},
	}
	for _, c := range cases {
		if got := string(Strip([]byte(c.in))); got != c.want {
			t.Errorf("Strip(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// adversarial builds the kinds of input that broke emulators, at sizes a test can afford.
func adversarial() map[string][]byte {
	rep := func(s string, n int) []byte { return []byte(strings.Repeat(s, n)) }
	return map[string][]byte{
		"rep 1e9":             []byte("x\x1b[1000000000b"),
		"il dl su sd 1e8":     rep("\x1b[100000000L\x1b[100000000M\x1b[100000000S\x1b[100000000T", 10),
		"cht cbt 1e9":         []byte("\x1b[1000000000I\x1b[1000000000Z"),
		"unterminated osc":    append([]byte("\x1b]0;"), rep("a", 3<<20)...),
		"title push":          append(append([]byte("\x1b]2;"), rep("t", 1<<20)...), append([]byte("\x07"), rep("\x1b[22;0t", 10000)...)...),
		"kitty push":          rep("\x1b[>1u", 100000),
		"invalid utf8":        rep("\xff\xfe\xc2\xc3\x80", 200000),
		"unique links":        []byte(strings.Repeat("\x1b]8;;https://x.test/\x1b\\l\x1b]8;;\x1b\\", 20000)),
		"endless dcs":         append([]byte("\x1bPq"), rep("#0;2;0;0;0", 300000)...),
		"endless apc":         append([]byte("\x1b_G"), rep("A", 3<<20)...),
		"sync never closed":   append([]byte("\x1b[?2026h"), rep("text ", 500000)...),
		"20k-param sgr":       append(append([]byte("\x1b["), rep("1;", 20000)...), 'm'),
		"5000-digit param":    append(append([]byte("\x1b["), rep("7", 5000)...), 'C'),
		"cup 4e9":             []byte("\x1b[4000000000;4000000000H"),
		"xtwinops 9999":       []byte("\x1b[8;9999;9999t\x1b[4;9999;9999t"),
		"c1 csi":              rep("\xc2\x9b999999999b", 1000),
		"controls inside csi": rep("\x1b[99\r9999\n9999b", 1000),
	}
}

func TestAdversarialInputIsClamped(t *testing.T) {
	for name, in := range adversarial() {
		s := New()
		var out []byte
		// Feed in read-sized chunks, as the terminal's reader does.
		for i := 0; i < len(in); i += 32 << 10 {
			o, _ := s.Scan(in[i:min(len(in), i+32<<10)])
			out = append(out, o...)
			if cap(s.pend) > 2*MaxString {
				t.Errorf("%s: the scanner holds %d bytes", name, cap(s.pend))
			}
		}
		if err := scantest.Verify(out); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestRandomChunkingAgainstCorpus(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	var all []byte
	for i := 0; i < 400; i++ {
		all = append(all, corpus[rng.Intn(len(corpus))]...)
		if rng.Intn(4) == 0 {
			noise := make([]byte, rng.Intn(40))
			rng.Read(noise)
			all = append(all, noise...)
		}
	}
	wantOut, wantMarks := scanAll(all)
	for round := 0; round < 200; round++ {
		var chunks [][]byte
		for i := 0; i < len(all); {
			n := 1 + rng.Intn(64)
			chunks = append(chunks, all[i:min(len(all), i+n)])
			i += n
		}
		out, marks := scanAll(chunks...)
		if !bytes.Equal(out, wantOut) || !reflect.DeepEqual(marks, wantMarks) {
			t.Fatalf("round %d: chunked scan differs", round)
		}
	}
	if err := scantest.Verify(wantOut); err != nil {
		t.Fatal(err)
	}
}

func FuzzScan(f *testing.F) {
	for _, c := range corpus {
		f.Add([]byte(c), uint8(3))
	}
	for _, v := range adversarial() {
		f.Add(v[:min(len(v), 4096)], uint8(7))
	}
	f.Fuzz(func(t *testing.T, data []byte, split uint8) {
		if len(data) > 1<<20 {
			return
		}
		wantOut, wantMarks := scanAll(data)
		if err := scantest.Verify(wantOut); err != nil {
			t.Fatal(err)
		}
		k := int(split) % (len(data) + 1)
		out, marks := scanAll(data[:k], data[k:])
		if !bytes.Equal(out, wantOut) || !reflect.DeepEqual(marks, wantMarks) {
			t.Fatalf("split at %d differs", k)
		}
		_ = Strip(data)
	})
}

func BenchmarkScanText(b *testing.B) {
	line := []byte("\x1b[32mok\x1b[0m  some ordinary build output, with a path /usr/lib/x.so and ünïcödé\r\n")
	data := bytes.Repeat(line, (1<<20)/len(line))
	s := New()
	b.SetBytes(int64(len(data)))
	for i := 0; i < b.N; i++ {
		for j := 0; j < len(data); j += 32 << 10 {
			s.Scan(data[j:min(len(data), j+32<<10)])
		}
	}
}
