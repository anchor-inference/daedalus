package browser

import (
	"archive/zip"
	"bytes"
	"encoding/binary"
	"strings"
	"testing"
)

func TestScriptMatches(t *testing.T) {
	for _, c := range []struct {
		matches []string
		url     string
		runs    bool
	}{
		{nil, "https://anything.example/x", true},
		{[]string{"<all_urls>"}, "http://127.0.0.1:3000/", true},
		{[]string{"example.com"}, "https://example.com/", true},
		{[]string{"example.com"}, "http://shop.example.com:8080/cart?x=1", true},
		{[]string{"example.com"}, "https://example.com.evil.test/", false},
		{[]string{"example.com"}, "https://notexample.com/", false},
		{[]string{"https://example.com/*"}, "https://example.com/a/b", true},
		{[]string{"https://example.com/*"}, "http://example.com/a", false},
		{[]string{"*://localhost:*/*"}, "http://localhost:5173/app", true},
		{[]string{"*://localhost:*/*"}, "http://127.0.0.1:5173/app", false},
		{[]string{"https://a.test/*", "https://b.test/*"}, "https://b.test/", true},
		// A dot is a dot, not any character.
		{[]string{"https://a.test/x.js"}, "https://a.test/xajs", false},
	} {
		s := PageScript{Name: "s", JS: "1", Matches: c.matches}
		if err := s.compile(); err != nil {
			t.Fatalf("%v: %v", c.matches, err)
		}
		if got := s.Runs(c.url); got != c.runs {
			t.Errorf("%v on %s: %v, want %v", c.matches, c.url, got, c.runs)
		}
	}
	for _, bad := range []PageScript{{Name: "", JS: "1"}, {Name: "a b", JS: "1"}, {Name: "empty"},
		{Name: "big", JS: strings.Repeat("x", MaxScriptBytes+1)}} {
		if bad.compile() == nil {
			t.Errorf("accepted %q", bad.Name)
		}
	}
}

// TestScriptSourceKeepsTheAgentsCodeApart holds the wrapper to its promises: the code inside a try
// that names the script, the styles first, the frame check only where asked, and a source address
// that errors are told under.
func TestScriptSourceKeepsTheAgentsCodeApart(t *testing.T) {
	s := PageScript{Name: "paint", JS: "window.x = 1 // a comment on the last line", CSS: "h1{color:red}", Matches: []string{"example.com"}}
	if err := s.compile(); err != nil {
		t.Fatal(err)
	}
	src := s.source()
	for _, want := range []string{"(async () => {", "window !== window.top", "replaceSync(\"h1{color:red}\")",
		"window.x = 1 // a comment on the last line\n}", "page script \" + \"paint\" + \" threw:", "//# sourceURL=page-script/paint.js"} {
		if !strings.Contains(src, want) {
			t.Errorf("the source lacks %q:\n%s", want, src)
		}
	}
	if strings.Index(src, "replaceSync") > strings.Index(src, "window.x") {
		t.Error("the styles come after the code")
	}
	lines := strings.Split(src, "\n")
	if got := lines[scriptPrologue(s)]; got != s.JS {
		t.Errorf("the prologue is not %d lines: line %d is %q", scriptPrologue(s), scriptPrologue(s)+1, got)
	}
	s.AllFrames, s.CSS = true, ""
	src = s.source()
	if strings.Contains(src, "window.top") || strings.Contains(src, "CSSStyleSheet") {
		t.Errorf("all frames and no styles:\n%s", src)
	}
	if got := strings.Split(src, "\n")[scriptPrologue(s)]; got != s.JS {
		t.Errorf("the prologue without styles: %q", got)
	}
}

func TestExtensionIDIsChromiums(t *testing.T) {
	// The rpc suite holds it to the id a real Chromium gives; here, only its shape.
	if got := ExtensionID("/tmp/probe/ext"); len(got) != 32 || strings.Trim(got, "abcdefghijklmnop") != "" {
		t.Fatalf("not an extension id: %q", got)
	}
	if ExtensionID("/a") == ExtensionID("/b") {
		t.Fatal("two paths, one id")
	}
}

func zipOf(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = w.Write([]byte(body))
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestExtensionArchives(t *testing.T) {
	manifest := `{"manifest_version": 3, "name": "Probe Thing", "version": "1.2"}`
	files, man, err := readExtensionArchive(zipOf(t, map[string]string{"manifest.json": manifest, "js/a.js": "1"}))
	if err != nil || man.Name != "Probe Thing" || files["js/a.js"] == nil {
		t.Fatalf("a flat archive: %v %+v %v", err, man, files)
	}
	// An archive of the folder itself: the folder holding the manifest is the root.
	files, _, err = readExtensionArchive(zipOf(t, map[string]string{"dist/manifest.json": manifest, "dist/a.js": "1", "README": "x"}))
	if err != nil || files["a.js"] == nil || files["manifest.json"] == nil || files["README"] != nil {
		t.Fatalf("a folder's archive: %v %v", err, files)
	}
	// A crx is a zip behind its header.
	z := zipOf(t, map[string]string{"manifest.json": manifest})
	header := []byte("proto-header")
	crx := append([]byte("Cr24"), make([]byte, 8)...)
	binary.LittleEndian.PutUint32(crx[4:], 3)
	binary.LittleEndian.PutUint32(crx[8:], uint32(len(header)))
	crx = append(append(crx, header...), z...)
	if _, man, err = readExtensionArchive(crx); err != nil || man.Version != "1.2" {
		t.Fatalf("a crx: %v %+v", err, man)
	}
	for name, data := range map[string][]byte{
		"no manifest":  zipOf(t, map[string]string{"a.js": "1"}),
		"outside":      zipOf(t, map[string]string{"manifest.json": manifest, "../evil.js": "1"}),
		"not a zip":    []byte("hello"),
		"no version":   zipOf(t, map[string]string{"manifest.json": `{"name": "x"}`}),
		"cut-off crx":  crx[:14],
		"bad manifest": zipOf(t, map[string]string{"manifest.json": "{"}),
	} {
		if _, _, err := readExtensionArchive(data); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if s := slug("My Great Extension! (dev)"); s != "my-great-extension-dev" {
		t.Errorf("slug: %q", s)
	}
	if d := uniqueDir([]Extension{{Dir: "probe"}, {Dir: "probe-2"}}, "probe"); d != "probe-3" {
		t.Errorf("unique dir: %q", d)
	}
}
