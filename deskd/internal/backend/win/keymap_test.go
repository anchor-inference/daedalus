package win

import (
	"slices"
	"testing"

	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

func TestNamedKey(t *testing.T) {
	cases := []struct {
		key      keys.Key
		vk       uint16
		extended bool
	}{
		{keys.Key{Name: keys.Enter}, vkReturn, false},
		{keys.Key{Name: keys.Left}, vkLeft, true},
		{keys.Key{Name: keys.PageDown}, vkNext, true},
		{keys.Key{Name: keys.Delete}, vkDelete, true},
		{keys.Key{Name: keys.PrintScr}, vkSnapshot, true},
		{keys.Key{Name: "f1"}, 0x70, false},
		{keys.Key{Name: "f24"}, 0x87, false},
		{keys.Key{Name: "ctrl"}, vkControl, false},
		{keys.Key{Name: "super"}, vkLWin, true},
	}
	for _, c := range cases {
		got, ok := namedKey(c.key)
		if !ok || got.vk != c.vk || got.extended != c.extended {
			t.Errorf("%v: got %+v %v, want vk %#x extended %v", c.key, got, ok, c.vk, c.extended)
		}
	}
	if _, ok := namedKey(keys.Key{Rune: 'a'}); ok {
		t.Error("a character key must not be in the named table")
	}
}

// Every name keys.ParseChord can produce must reach a virtual key, or a chord the parser accepts
// would fail only on Windows.
func TestNamedKeyCoversParser(t *testing.T) {
	for _, s := range []string{"enter", "tab", "escape", "backspace", "delete", "insert", "home", "end",
		"pageup", "pagedown", "up", "down", "left", "right", "space", "capslock", "printscreen", "menu",
		"ctrl", "alt", "shift", "super", "f13"} {
		c, err := keys.ParseChord(s)
		if err != nil {
			t.Fatalf("%s: %v", s, err)
		}
		if _, ok := namedKey(c.Key); !ok {
			t.Errorf("%s parses to %+v, which has no virtual key", s, c.Key)
		}
	}
}

func TestWheelData(t *testing.T) {
	if got := wheelData(2, true); !slices.Equal(got, []int32{-120, -120}) {
		t.Errorf("two notches down: %v", got)
	}
	if got := wheelData(-1, true); !slices.Equal(got, []int32{120}) {
		t.Errorf("one notch up: %v", got)
	}
	if got := wheelData(3, false); !slices.Equal(got, []int32{120, 120, 120}) {
		t.Errorf("three notches right: %v", got)
	}
	if got := wheelData(0, true); len(got) != 0 {
		t.Errorf("no notches: %v", got)
	}
}

func TestBGRAToRGBA(t *testing.T) {
	src := []byte{1, 2, 3, 0, 10, 20, 30, 0}
	dst := make([]byte, len(src))
	bgraToRGBA(dst, src)
	if want := []byte{3, 2, 1, 255, 30, 20, 10, 255}; !slices.Equal(dst, want) {
		t.Errorf("got %v, want %v", dst, want)
	}
}

func TestAppName(t *testing.T) {
	for in, want := range map[string]string{
		`C:\Program Files\App\Editor.EXE`: "Editor",
		`C:\Windows\explorer.exe`:         "explorer",
		`tool`:                            "tool",
		`C:\dir\.exe`:                     ".exe",
		``:                                "",
	} {
		if got := appName(in); got != want {
			t.Errorf("appName(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMoreTrusted(t *testing.T) {
	const medium, high = 0x2000, 0x3000
	if !moreTrusted(high, medium) || moreTrusted(medium, medium) || moreTrusted(0x1000, medium) {
		t.Error("only a higher integrity level outranks ours")
	}
}
