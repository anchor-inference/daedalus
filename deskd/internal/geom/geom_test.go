package geom

import (
	"errors"
	"math"
	"testing"
)

func TestFitScalesDownOnlyAndKeepsTheAspect(t *testing.T) {
	for _, tc := range []struct {
		w, h, edge, ww, wh int
	}{
		{2880, 1800, 1568, 1568, 980}, // a 16:10 Retina display
		{1800, 2880, 1568, 980, 1568}, // the same, rotated
		{1280, 800, 1568, 1280, 800},  // already small enough: never scaled up
		{3840, 2160, 1280, 1280, 720},
		{5000, 3, 1000, 1000, 1}, // a sliver keeps one pixel
		{1920, 1080, 0, 1920, 1080},
		{1366, 768, 1366, 1366, 768}, // exactly the edge
	} {
		gw, gh := Fit(tc.w, tc.h, tc.edge)
		if gw != tc.ww || gh != tc.wh {
			t.Errorf("Fit(%d, %d, %d) = %d×%d, want %d×%d", tc.w, tc.h, tc.edge, gw, gh, tc.ww, tc.wh)
		}
	}
}

// Each row is a picture of a region, a pixel the model names on it, and where that must land.
func TestScreenshotPixelsLandWhereTheyWereRead(t *testing.T) {
	for _, tc := range []struct {
		name   string
		frame  Frame
		x, y   int
		dx, dy float64
	}{
		{"unscaled pixel space: the pixel is the pixel", Frame{Rect{0, 0, 1920, 1080}, 1920, 1080}, 100, 200, 100.5, 200.5},
		{"zero is a coordinate, not a missing one", Frame{Rect{0, 0, 1920, 1080}, 1920, 1080}, 0, 0, 0.5, 0.5},
		{"the last pixel", Frame{Rect{0, 0, 1920, 1080}, 1920, 1080}, 1919, 1079, 1919.5, 1079.5},
		{"halved: one pixel spans two units", Frame{Rect{0, 0, 2560, 1600}, 1280, 800}, 640, 400, 1281, 801},
		{"Retina points, downscaled from 2880 pixels to 1568", Frame{Rect{0, 0, 1440, 900}, 1568, 980}, 784, 490, 720.459, 450.459},
		{"a display left of the primary", Frame{Rect{-1920, 0, 1920, 1080}, 1280, 720}, 0, 0, -1919.25, 0.75},
		{"a display above and left, fractional size", Frame{Rect{-1536, -864, 1536, 864}, 1536, 864}, 1535, 863, -0.5, -0.5},
		{"a window on a 150 % display, in physical pixels", Frame{Rect{300, 200, 900, 600}, 600, 400}, 10, 10, 315.75, 215.75},
		{"125 %: thirds of a unit do not round the wrong way", Frame{Rect{0, 0, 2400, 1350}, 1920, 1080}, 3, 3, 4.375, 4.375},
	} {
		dx, dy, err := tc.frame.ToDesktop(tc.x, tc.y)
		if err != nil {
			t.Errorf("%s: %v", tc.name, err)
			continue
		}
		if math.Abs(dx-tc.dx) > 0.001 || math.Abs(dy-tc.dy) > 0.001 {
			t.Errorf("%s: (%d, %d) → (%.3f, %.3f), want (%.3f, %.3f)", tc.name, tc.x, tc.y, dx, dy, tc.dx, tc.dy)
		}
		// And back: the desktop point is shown by the very pixel it came from.
		bx, by, in := tc.frame.FromDesktop(dx, dy)
		if !in || bx != tc.x || by != tc.y {
			t.Errorf("%s: back to (%d, %d) inside=%v, want (%d, %d)", tc.name, bx, by, in, tc.x, tc.y)
		}
	}
}

func TestPointsOffThePictureAreRefused(t *testing.T) {
	f := Frame{Rect{-1920, 0, 1920, 1080}, 1280, 720}
	for _, p := range [][2]int{{-1, 0}, {0, -1}, {1280, 0}, {0, 720}, {5000, 5000}} {
		if _, _, err := f.ToDesktop(p[0], p[1]); !errors.Is(err, ErrOutsideFrame) {
			t.Errorf("(%d, %d): %v", p[0], p[1], err)
		}
	}
	if _, _, err := (Frame{}).ToDesktop(0, 0); err == nil {
		t.Error("a frame with no size converted a point")
	}
	if _, _, in := f.FromDesktop(0.5, 10); in {
		t.Error("a point on the primary display was found on the display left of it")
	}
}

// Every pixel of a downscaled picture of a display left of the primary maps to a distinct point inside
// the region, and back to itself.
func TestEveryPixelRoundTrips(t *testing.T) {
	for _, f := range []Frame{
		{Rect{-1440, -100, 1440, 900}, 1568, 980},
		{Rect{0, 0, 2560, 1440}, 1366, 768},
		{Rect{1920, 0, 1707.5, 960}, 1280, 720},
	} {
		for x := 0; x < f.Width; x += 7 {
			for y := 0; y < f.Height; y += 11 {
				dx, dy, err := f.ToDesktop(x, y)
				if err != nil || !f.Region.Contains(dx, dy) {
					t.Fatalf("%+v: (%d, %d) → (%v, %v) %v", f, x, y, dx, dy, err)
				}
				if bx, by, in := f.FromDesktop(dx, dy); !in || bx != x || by != y {
					t.Fatalf("%+v: (%d, %d) came back as (%d, %d)", f, x, y, bx, by)
				}
			}
		}
	}
}

func TestSnapIsTheFloorAlsoLeftOfZero(t *testing.T) {
	for v, want := range map[float64]int{0.5: 0, 1919.5: 1919, -0.5: -1, -1919.25: -1920, 3: 3} {
		if got := Snap(v); got != want {
			t.Errorf("Snap(%v) = %d, want %d", v, got, want)
		}
	}
}

func TestDisplaysAreFoundByPointAndByMostOfAWindow(t *testing.T) {
	ds := []Display{
		{ID: "left", Bounds: Rect{-1920, 0, 1920, 1080}, PixelsPerUnit: 1},
		{ID: "main", Bounds: Rect{0, 0, 1440, 900}, PixelsPerUnit: 2, Primary: true},
	}
	if d, ok := DisplayAt(ds, -0.5, 10); !ok || d.ID != "left" {
		t.Errorf("left of the seam: %v %v", d.ID, ok)
	}
	if d, ok := DisplayAt(ds, 0, 10); !ok || d.ID != "main" {
		t.Errorf("on the seam: %v %v", d.ID, ok)
	}
	if _, ok := DisplayAt(ds, 0, 1000); ok {
		t.Error("a point below the shorter display was found on it")
	}
	if d, ok := DisplayOf(ds, Rect{-300, 100, 1000, 500}); !ok || d.ID != "main" {
		t.Errorf("a window mostly on main: %v", d.ID)
	}
	if w, h := ds[1].PixelSize(); w != 2880 || h != 1800 {
		t.Errorf("Retina pixel size %d×%d", w, h)
	}
	if u := Union(ds[0].Bounds, ds[1].Bounds); u != (Rect{-1920, 0, 3360, 1080}) {
		t.Errorf("virtual screen %+v", u)
	}
}

// Windows maps a normalised n back to floor(n*width/65536); every pixel must come back as itself, on
// every display of a virtual screen that starts left of zero.
func TestVirtualDesktopNormalisationReachesEveryPixel(t *testing.T) {
	for _, virt := range []Rect{{-1920, -200, 4480, 1640}, {0, 0, 1920, 1080}, {0, 0, 7680, 4320}} {
		for x := int(virt.X); x < int(virt.X+virt.W); x++ {
			nx, _ := NormalizeVirtual(x, int(virt.Y), virt)
			back := int(virt.X) + int(int64(nx)*int64(virt.W)/65536)
			if back != x || nx < 0 || nx > 65535 {
				t.Fatalf("virtual %+v: x %d → %d → %d", virt, x, nx, back)
			}
		}
		for y := int(virt.Y); y < int(virt.Y+virt.H); y++ {
			_, ny := NormalizeVirtual(int(virt.X), y, virt)
			if back := int(virt.Y) + int(int64(ny)*int64(virt.H)/65536); back != y {
				t.Fatalf("virtual %+v: y %d → %d → %d", virt, y, ny, back)
			}
		}
	}
}
