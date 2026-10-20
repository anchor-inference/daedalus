package x11

import (
	"image/color"
	"testing"

	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

func TestZPixmapsOfEveryLayoutConvert(t *testing.T) {
	// Two pixels, red then blue, in the 32-bit little-endian layout of nearly every server.
	f := pixelFormat{bytesPerPixel: 4, lsbFirst: true, red: 0xff0000, green: 0x00ff00, blue: 0x0000ff}
	img, err := toRGBA([]byte{0, 0, 255, 0, 255, 0, 0, 0}, 2, 1, 32, f)
	if err != nil {
		t.Fatal(err)
	}
	if img.RGBAAt(0, 0) != (color.RGBA{255, 0, 0, 255}) || img.RGBAAt(1, 0) != (color.RGBA{0, 0, 255, 255}) {
		t.Errorf("little endian: %v %v", img.RGBAAt(0, 0), img.RGBAAt(1, 0))
	}
	// The same, big-endian.
	f.lsbFirst = false
	img, _ = toRGBA([]byte{0, 255, 0, 0, 0, 0, 0, 255}, 2, 1, 32, f)
	if img.RGBAAt(0, 0) != (color.RGBA{255, 0, 0, 255}) || img.RGBAAt(1, 0) != (color.RGBA{0, 0, 255, 255}) {
		t.Errorf("big endian: %v %v", img.RGBAAt(0, 0), img.RGBAAt(1, 0))
	}
	// 16-bit 5-6-5 with rows padded to 32 bits: a 3-pixel row is 6 bytes plus 2 of padding.
	f = pixelFormat{bytesPerPixel: 2, lsbFirst: true, red: 0xf800, green: 0x07e0, blue: 0x001f}
	data := []byte{0x00, 0xf8, 0xe0, 0x07, 0x1f, 0x00, 0xee, 0xee, 0xff, 0xff, 0, 0, 0, 0, 0xee, 0xee}
	img, err = toRGBA(data, 3, 2, 32, f)
	if err != nil {
		t.Fatal(err)
	}
	if img.RGBAAt(0, 0) != (color.RGBA{255, 0, 0, 255}) || img.RGBAAt(1, 0) != (color.RGBA{0, 255, 0, 255}) ||
		img.RGBAAt(2, 0) != (color.RGBA{0, 0, 255, 255}) || img.RGBAAt(0, 1) != (color.RGBA{255, 255, 255, 255}) {
		t.Errorf("565: %v %v %v %v", img.RGBAAt(0, 0), img.RGBAAt(1, 0), img.RGBAAt(2, 0), img.RGBAAt(0, 1))
	}
	if _, err := toRGBA([]byte{1, 2}, 2, 2, 32, pixelFormat{bytesPerPixel: 4}); err == nil {
		t.Error("a short image converted")
	}
}

func TestKeysymsOfKeysAndCharacters(t *testing.T) {
	for k, want := range map[keys.Key]uint32{
		{Name: keys.Enter}: 0xff0d, {Name: "f1"}: 0xffbe, {Name: "f12"}: 0xffc9, {Name: "ctrl"}: 0xffe3,
		{Rune: 'a'}: 0x61, {Rune: 'é'}: 0xe9, {Rune: 'ж'}: 0x01000436, {Rune: '😀'}: 0x0101f600,
	} {
		if got, ok := keysymOf(k); !ok || got != want {
			t.Errorf("%v: %#x", k, got)
		}
	}
	for ks, want := range map[uint32]rune{0x61: 'a', 0x01000436: 'ж', 0xff0d: 0} {
		if got := keysymRune(ks); got != want {
			t.Errorf("%#x → %q", ks, got)
		}
	}
}

func TestTheKeymapIsSearchedAsX11ReadsIt(t *testing.T) {
	// Keycodes 10–13: "a A", "1 !", "b" listed lower case only, and an empty one.
	m := keymap{min: 10, max: 13, per: 2, syms: []uint32{0x61, 0x41, 0x31, 0x21, 0x62, 0, 0, 0}}
	for ks, want := range map[uint32][2]int{0x61: {10, 0}, 0x41: {10, 1}, 0x21: {11, 1}, 0x42: {12, 1}} {
		code, col, ok := m.find(ks)
		if !ok || int(code) != want[0] || col != want[1] {
			t.Errorf("%#x: %d/%d %v", ks, code, col, ok)
		}
	}
	if _, _, ok := m.find(0x01000436); ok {
		t.Error("a character not on the layout was found")
	}
	if sp := m.spare(); len(sp) != 1 || sp[0] != 13 {
		t.Errorf("spare %v", sp)
	}
}
