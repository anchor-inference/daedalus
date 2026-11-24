//go:build unix

package rpc_test

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// readShot reads a kept picture to its end, as the host does.
func (h *harness) readShot(id string) []byte {
	h.t.Helper()
	var out []byte
	for {
		var chunk struct {
			Data string `json:"data_b64"`
			EOF  bool   `json:"eof"`
		}
		h.must("shot.read", map[string]any{"id": id, "offset": len(out), "max": 64 << 10}, &chunk)
		b, err := base64.StdEncoding.DecodeString(chunk.Data)
		if err != nil {
			h.t.Fatal(err)
		}
		out = append(out, b...)
		if chunk.EOF {
			return out
		}
	}
}

// darkRun is the length of the first run of dark pixels on row y, and where it starts.
func darkRun(img image.Image, y int) (start, n int) {
	b := img.Bounds()
	start = -1
	for x := b.Min.X; x < b.Max.X; x++ {
		r, g, bl, _ := img.At(x, y).RGBA()
		dark := (r>>8)+(g>>8)+(bl>>8) < 3*128
		switch {
		case dark && start < 0:
			start = x
		case !dark && start >= 0:
			return start, x - start
		}
	}
	return start, 0
}

// A picture of a part of the viewport at four times its CSS pixels is drawn at that resolution, not
// enlarged: the sign-in code of qr.html, one CSS pixel a module, comes back four pixels a module
// with edges that are black or white, kept in the daemon and read back with shot.read. Only the
// operator's picture may leave the secret fields showing. BROWSERD_SHOT_DIR keeps the pictures, the
// live view's frames beside the sharp one, for a decoder to compare.
func TestScreenshotScaledClipIsSharp(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/qr.html")
	tab := o.Tab.ID
	if s := h.snapshot(tab); !strings.Contains(s.Text, "Sign-in code") {
		t.Fatalf("the page is not drawn yet:\n%s", s.Text)
	}
	const modules, at = 45, 4
	clip := map[string]any{"x": 600, "y": 300, "w": modules, "h": modules}

	if err := h.call("page.screenshot", map[string]any{"tab_id": tab, "clip": clip, "unmasked": true}, nil); code(err) != protowire.CodeForbidden {
		t.Fatalf("an agent's unmasked picture: %v", err)
	}
	if err := h.call("page.screenshot", map[string]any{"tab_id": tab, "scale": 9}, nil); code(err) != protowire.CodeInvalidParams {
		t.Fatalf("a scale past the cap: %v", err)
	}
	var shot page.Screenshot
	h.must("page.screenshot", map[string]any{"tab_id": tab, "clip": clip, "scale": at, "format": "png", "keep": true, "unmasked": true,
		"origin": map[string]any{"actor": "operator"}}, &shot)
	if shot.Data != "" || shot.ShotID == "" || shot.Size == 0 || shot.Scale != at {
		t.Fatalf("a kept picture: %+v", shot)
	}
	data := h.readShot(shot.ShotID)
	if len(data) != shot.Size {
		t.Fatalf("read %d bytes of %d", len(data), shot.Size)
	}
	if err := h.call("shot.read", map[string]any{"id": shot.ShotID, "offset": 0}, nil); code(err) != protowire.CodeNotFound {
		t.Fatalf("a picture read to its end is forgotten: %v", err)
	}
	img, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if w, hh := img.Bounds().Dx(), img.Bounds().Dy(); w != modules*at || hh != modules*at || shot.Width != w {
		t.Fatalf("the picture is %dx%d (said %d), not %d square", w, hh, shot.Width, modules*at)
	}
	// The finder pattern's top row: four modules of quiet zone, then seven dark ones.
	start, run := darkRun(img, int((4+0.5)*at))
	if start < 4*at-1 || start > 4*at+1 || run < 7*at-2 || run > 7*at+2 {
		t.Fatalf("the finder pattern's row starts at %d and is %d dark pixels, not %d at %d", start, run, 7*at, 4*at)
	}
	grey := 0
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			r, _, _, _ := img.At(x, y).RGBA()
			if v := r >> 8; v > 40 && v < 215 {
				grey++
			}
		}
	}
	// An enlarged picture of one pixel a module is grey along every edge; one drawn at four times
	// greys only its antialiased rim.
	if share := float64(grey) / float64(b.Dx()*b.Dy()); share > 0.12 {
		t.Fatalf("%.0f%% of the picture is grey: it was enlarged, not drawn at its scale", share*100)
	}

	if dir := os.Getenv("BROWSERD_SHOT_DIR"); dir != "" {
		if err := os.WriteFile(filepath.Join(dir, "sharp-x4.png"), data, 0o644); err != nil {
			t.Fatal(err)
		}
		for _, box := range []int{640, 1280} {
			v := h.attach("g1", wire.Attach{Tier: "live", MaxW: box, MaxH: 4096, Quality: 60})
			// The first frames may be of the window before the screencast took the asked size.
			var f wire.Frame
			for deadline := time.Now().Add(10 * time.Second); f.Meta.W != min(box, 1280); {
				got, ok := v.frame(time.Until(deadline))
				if !ok {
					t.Fatalf("no live frame %d wide; the last was %dx%d", min(box, 1280), f.Meta.W, f.Meta.H)
				}
				f = got
				v.ack(f.FrameNo)
			}
			name := filepath.Join(dir, fmt.Sprintf("live-%d.jpg", box))
			if err := os.WriteFile(name, f.Image, 0o644); err != nil {
				t.Fatal(err)
			}
			t.Logf("live frame at a %d box: %dx%d, page %gx%g", box, f.Meta.W, f.Meta.H, f.Meta.VW, f.Meta.VH)
		}
	}

	// The agent's picture of the same page still hides the password.
	var masked page.Screenshot
	h.must("page.screenshot", map[string]any{"tab_id": tab, "scale": 2, "format": "png", "keep": true}, &masked)
	if len(masked.Masked) == 0 {
		t.Fatalf("an agent's scaled picture masked nothing: %+v", masked)
	}
	_ = h.readShot(masked.ShotID)
}
