//go:build unix

package rpc_test

import (
	"os"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

// preset is one of the app's picture presets as its ATTACH says it (miniapp/src/browser/geometry.ts).
type preset struct {
	name     string
	maxW     int
	maxH     int
	quality  int
	fps      int
	adaptive *bool
}

var off = false

var presets = []preset{
	{"auto", 2560, 1600, 0, 0, nil},
	{"smooth", 1280, 800, 50, 15, nil},
	{"balanced", 1920, 1200, 75, 8, nil},
	{"sharp", 4096, 4096, 92, 4, &off},
}

// watch attaches a live view with the preset to the group's tab, acknowledges every frame at once, and
// counts frames and bytes over the span: what the operator's link carries for that choice.
func watch(t *testing.T, h *harness, group string, p preset, span time.Duration) (frames int, bytes int, w, ht int) {
	t.Helper()
	v := h.attach(group, wire.Attach{Tier: "live", MaxW: p.maxW, MaxH: p.maxH, Quality: p.quality, FPS: p.fps, Adaptive: p.adaptive})
	// The first frame may be the newest the cast kept; the span starts after it.
	if f, ok := v.frame(10 * time.Second); ok {
		v.ack(f.FrameNo)
	} else {
		t.Fatalf("%s: no first frame", p.name)
	}
	end := time.Now().Add(span)
	for time.Now().Before(end) {
		f, ok := v.frame(time.Until(end))
		if !ok {
			break
		}
		frames++
		bytes += len(f.Image)
		w, ht = f.Meta.W, f.Meta.H
		v.ack(f.FrameNo)
	}
	h.must("view.detach", map[string]any{"channel": v.channel}, nil)
	return frames, bytes, w, ht
}

// TestQualityPresetsTradeFramesForDetail holds the presets to what they promise on a moving page of
// text: Sharp sends few frames, each larger and at the page's full size; Smooth many smaller ones.
// With BROWSERD_MEASURE=1 every preset is measured for longer and the table is logged.
func TestQualityPresetsTradeFramesForDetail(t *testing.T) {
	launchOnly(t, "the measure is of the daemon's own screencast")
	h := start(t, nil)
	h.open("g1", "p1", "/reading")
	span := 3 * time.Second
	chosen := []preset{presets[1], presets[3]}
	if os.Getenv("BROWSERD_MEASURE") != "" {
		span, chosen = 10*time.Second, presets
	}
	type result struct {
		frames, bytes, w, h int
	}
	got := map[string]result{}
	for _, p := range chosen {
		n, b, w, ht := watch(t, h, "g1", p, span)
		got[p.name] = result{n, b, w, ht}
		per := 0
		if n > 0 {
			per = b / n
		}
		t.Logf("%-8s %5.1f frames/s  %7d bytes/frame  %6.0f KB/s  %dx%d", p.name, float64(n)/span.Seconds(), per, float64(b)/span.Seconds()/1024, w, ht)
	}
	smooth, sharp := got["smooth"], got["sharp"]
	if sharp.frames == 0 || smooth.frames == 0 {
		t.Fatalf("no frames: %+v", got)
	}
	if fps := float64(sharp.frames) / span.Seconds(); fps > 5.5 {
		t.Errorf("Sharp sent %.1f frames a second, past its 4", fps)
	}
	if sharp.bytes/sharp.frames <= smooth.bytes/smooth.frames {
		t.Errorf("a Sharp frame (%d bytes) is no larger than a Smooth one (%d)", sharp.bytes/sharp.frames, smooth.bytes/smooth.frames)
	}
	if sharp.w != 1280 || sharp.h != 800 {
		t.Errorf("Sharp is not the page's full size: %dx%d", sharp.w, sharp.h)
	}
	if smooth.frames <= sharp.frames {
		t.Errorf("Smooth (%d frames) is not more fluid than Sharp (%d)", smooth.frames, sharp.frames)
	}
}
