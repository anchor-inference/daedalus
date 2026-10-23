package quartz

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

func TestSettingsLinksOpenEachPrivacyList(t *testing.T) {
	for which, anchor := range map[string]string{
		backend.PermScreen:          "Privacy_ScreenCapture",
		backend.PermAccessibility:   "Privacy_Accessibility",
		backend.PermInputMonitoring: "Privacy_ListenEvent",
	} {
		want := "x-apple.systempreferences:com.apple.preference.security?" + anchor
		if got := SettingsURL(which); got != want {
			t.Errorf("SettingsURL(%q) = %q, want %q", which, got, want)
		}
		if d := permissionDetail(which); !strings.Contains(d, want) || !strings.Contains(d, "remove") {
			t.Errorf("the detail for %q names neither its link nor the stale grant: %q", which, d)
		}
	}
	if got := SettingsURL("camera"); got != "x-apple.systempreferences:com.apple.preference.security" {
		t.Errorf("an unknown permission opens %q", got)
	}
	if permissionOf(true) != backend.Granted || permissionOf(false) != backend.Denied {
		t.Error("a probe's answer maps to the wrong state")
	}
}

func chord(t *testing.T, s string) keys.Chord {
	t.Helper()
	c, err := keys.ParseChord(s)
	if err != nil {
		t.Fatalf("ParseChord(%q): %v", s, err)
	}
	return c
}

func TestKeysMapToUSVirtualKeyCodes(t *testing.T) {
	for _, tc := range []struct {
		key     string
		code    uint16
		implied uint64
		mod     keys.Mod
	}{
		{"a", 0x00, 0, ""},
		{"z", 0x06, 0, ""},
		{"0", 0x1D, 0, ""},
		{"5", 0x17, 0, ""}, // the codes of 5 and 6 are out of order on the real layout too
		{"6", 0x16, 0, ""},
		{"Return", 0x24, 0, ""},
		{"Tab", 0x30, 0, ""},
		{"BackSpace", 0x33, 0, ""},
		{"Delete", 0x75, 0, ""},
		{"Escape", 0x35, 0, ""},
		{"space", 0x31, 0, ""},
		{"Up", 0x7E, 0, ""},
		{"Left", 0x7B, 0, ""},
		{"Page_Down", 0x79, 0, ""},
		{"F1", 0x7A, 0, ""},
		{"F12", 0x6F, 0, ""},
		{"F20", 0x5A, 0, ""},
		{"`", 0x32, 0, ""},
		{"slash", 0x2C, 0, ""},
		{"!", 0x12, flagShift, ""},
		{"?", 0x2C, flagShift, ""},
		{"~", 0x32, flagShift, ""},
		{"ctrl", 0x3B, 0, keys.Ctrl},
		{"alt", 0x3A, 0, keys.Alt},
		{"shift", 0x38, 0, keys.Shift},
		{"cmd", 0x37, 0, keys.Super},
	} {
		c := chord(t, tc.key)
		got, err := lookupKey(c.Key)
		if err != nil {
			t.Errorf("%q: %v", tc.key, err)
			continue
		}
		if got.code != tc.code || got.implied != tc.implied || got.mod != tc.mod {
			t.Errorf("%q = code %#x implied %#x mod %q, want %#x %#x %q",
				tc.key, got.code, got.implied, got.mod, tc.code, tc.implied, tc.mod)
		}
	}
	// A capital passed straight in, not through ParseChord, still needs Shift.
	if got, err := lookupKey(keys.Key{Rune: 'Q'}); err != nil || got.code != 0x0C || got.implied != flagShift {
		t.Errorf("Q = %+v, %v", got, err)
	}
}

func TestKeysWithoutAMacKeyAreRefused(t *testing.T) {
	for _, k := range []keys.Key{{Name: "f21"}, {Name: keys.PrintScr}, {Rune: 'é'}, {Rune: 'ж'}} {
		_, err := lookupKey(k)
		if err == nil {
			t.Errorf("%v has a key", k)
			continue
		}
		if k.Rune != 0 && !strings.Contains(err.Error(), "type it") {
			t.Errorf("the refusal of %v does not point at typing: %v", k, err)
		}
	}
}

func TestEveryLetterAndDigitHasADistinctCode(t *testing.T) {
	seen := map[uint16]rune{}
	for _, r := range "abcdefghijklmnopqrstuvwxyz0123456789-=[];',./`\\" {
		c, ok := runeCodes[r]
		if !ok {
			t.Errorf("%q has no code", r)
			continue
		}
		if o, dup := seen[c]; dup {
			t.Errorf("%q and %q share code %#x", r, o, c)
		}
		seen[c] = r
	}
	for shifted, base := range shiftedRunes {
		if _, ok := runeCodes[base]; !ok {
			t.Errorf("%q shifts a key %q that is not on the table", shifted, base)
		}
	}
}

func TestButtonsAndDragsUseTheirOwnEventTypes(t *testing.T) {
	for _, tc := range []struct {
		b          backend.Button
		down       bool
		typ, cgBtn int
	}{
		{backend.Left, true, 1, 0}, {backend.Left, false, 2, 0},
		{backend.Right, true, 3, 1}, {backend.Right, false, 4, 1},
		{backend.Middle, true, 25, 2}, {backend.Middle, false, 26, 2},
	} {
		typ, btn, err := buttonEvent(tc.b, tc.down)
		if err != nil || typ != tc.typ || btn != tc.cgBtn {
			t.Errorf("buttonEvent(%s, %v) = %d, %d, %v", tc.b, tc.down, typ, btn, err)
		}
	}
	if _, _, err := buttonEvent(backend.Button(9), true); err == nil {
		t.Error("an unknown button was accepted")
	}
	var held heldButtons
	if typ, _ := moveEvent(held); typ != eventMouseMoved {
		t.Errorf("a move with no button is %d", typ)
	}
	held[backend.Right] = true
	if typ, btn := moveEvent(held); typ != eventRightDragged || btn != cgButtonRight {
		t.Errorf("a move holding right is %d, %d", typ, btn)
	}
	held[backend.Left] = true
	if typ, _ := moveEvent(held); typ != eventLeftDragged {
		t.Errorf("a move holding left and right is %d", typ)
	}
	held = heldButtons{}
	held[backend.Middle] = true
	if typ, btn := moveEvent(held); typ != eventOtherDragged || btn != cgButtonCenter {
		t.Errorf("a move holding middle is %d, %d", typ, btn)
	}
}

func TestClicksCountLikeTheWindowServer(t *testing.T) {
	var c clickCounter
	t0 := time.Unix(1000, 0)
	iv := 500 * time.Millisecond
	steps := []struct {
		b    backend.Button
		x, y float64
		dt   time.Duration
		want int
	}{
		{backend.Left, 10, 10, 0, 1},
		{backend.Left, 10, 10, 200 * time.Millisecond, 2}, // a double click
		{backend.Left, 11, 9, 200 * time.Millisecond, 3},  // a triple, within the slop
		{backend.Left, 11, 9, 600 * time.Millisecond, 1},  // too late
		{backend.Left, 30, 9, 100 * time.Millisecond, 1},  // too far
		{backend.Right, 30, 9, 100 * time.Millisecond, 1}, // another button
		{backend.Right, 30, 9, 100 * time.Millisecond, 2},
	}
	now := t0
	for i, s := range steps {
		now = now.Add(s.dt)
		if got := c.press(s.b, s.x, s.y, now, iv); got != s.want {
			t.Errorf("press %d = %d, want %d", i, got, s.want)
		}
	}
	if got := c.release(backend.Right); got != 2 {
		t.Errorf("the release of a double click says %d", got)
	}
	if got := c.release(backend.Left); got != 1 {
		t.Errorf("the release of a button not last pressed says %d", got)
	}
}

func TestWheelSignsFollowQuartz(t *testing.T) {
	// Down is a negative wheel1, right a negative wheel2.
	if w1, w2 := wheelAxes(0, 3); w1 != -3 || w2 != 0 {
		t.Errorf("three notches down = %d, %d", w1, w2)
	}
	if w1, w2 := wheelAxes(2, -1); w1 != 1 || w2 != -2 {
		t.Errorf("two right and one up = %d, %d", w1, w2)
	}
}

func TestCaptureRowsDropTheirPadding(t *testing.T) {
	// Two pixels a row, padded to 12 bytes.
	data := []byte{
		1, 2, 3, 255, 4, 5, 6, 255, 9, 9, 9, 9,
		7, 8, 9, 255, 10, 11, 12, 255,
	}
	img, err := rgbaFromRows(2, 2, 12, data)
	if err != nil {
		t.Fatal(err)
	}
	if c := img.RGBAAt(1, 1); c.R != 10 || c.G != 11 || c.B != 12 || c.A != 255 {
		t.Errorf("pixel (1, 1) = %v", c)
	}
	if c := img.RGBAAt(0, 1); c.R != 7 {
		t.Errorf("the second row starts at the padding: %v", c)
	}
	if allZero(img) {
		t.Error("an opaque picture counts as empty")
	}
	if _, err := rgbaFromRows(2, 2, 12, data[:19]); err == nil {
		t.Error("a short buffer was accepted")
	}
	if _, err := rgbaFromRows(2, 2, 4, data); err == nil {
		t.Error("a stride shorter than a row was accepted")
	}
	blank, _ := rgbaFromRows(2, 1, 8, make([]byte, 8))
	if !allZero(blank) {
		t.Error("a transparent picture does not count as empty")
	}
}

// The daemon lives inside Daedalus.app and takes the application's Info.plist for its own, so without
// a background activation policy set before any other AppKit call it showed in the Dock as a second
// Daedalus that bounced for as long as it ran. The cgo file builds only on a Mac; this reads it, so
// that the guard holds on every machine the tests run on.
func TestTheDaemonStaysOutOfTheDock(t *testing.T) {
	body, err := os.ReadFile("quartz_darwin.go")
	if err != nil {
		t.Fatal(err)
	}
	source := string(body)
	if !strings.Contains(source, "[NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited]") {
		t.Fatal("dk_stay_out_of_dock no longer sets the prohibited activation policy")
	}
	start := strings.Index(source, "func init() {")
	if start < 0 {
		t.Fatal("the init that keeps the daemon out of the Dock is gone")
	}
	end := strings.Index(source[start:], "\n}\n")
	initBody := source[start : start+end]
	if !strings.Contains(initBody, "runtime.LockOSThread()") || !strings.Contains(initBody, "C.dk_stay_out_of_dock()") {
		t.Fatalf("init must lock the main thread and leave the Dock before anything else runs:\n%s", initBody)
	}
}
