// Package geom is the arithmetic between a screenshot's pixels and the desktop: where a point the
// model read off a picture lands on which display, at which scale. It is pure, so every trap that
// computer-use agents fell into (fractional scales, displays left of or above the primary, a zero
// coordinate taken for a missing one, two rounding modes disagreeing by a pixel) is pinned by a table
// in its test rather than found on a real machine.
//
// Three spaces meet here. Desktop units are what the backend's input calls take: X11 root pixels,
// Windows physical pixels (the daemon is per-monitor DPI aware, so the whole virtual screen is one
// physical space), macOS global points. Captured pixels are what the backend's capture returns,
// PixelsPerUnit to a unit (2 on a Retina display, 1 elsewhere). Screenshot pixels are what the model
// sees, captured pixels scaled down to fit a long edge. A Frame binds the last to the first and is the
// only conversion the daemon makes.
package geom

import (
	"errors"
	"fmt"
	"math"
)

// Rect is a rectangle in desktop units. X and Y may be negative: a display left of or above the
// primary one starts there.
type Rect struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	W float64 `json:"w"`
	H float64 `json:"h"`
}

// Empty is a rectangle with no area.
func (r Rect) Empty() bool { return r.W <= 0 || r.H <= 0 }

// Contains reports whether the point is inside: the left and top edges are in, the right and bottom
// ones are not, so two displays side by side never both claim the column between them.
func (r Rect) Contains(x, y float64) bool {
	return x >= r.X && y >= r.Y && x < r.X+r.W && y < r.Y+r.H
}

// Intersect is the overlap of two rectangles, empty when they do not meet.
func (r Rect) Intersect(o Rect) Rect {
	x0, y0 := math.Max(r.X, o.X), math.Max(r.Y, o.Y)
	x1, y1 := math.Min(r.X+r.W, o.X+o.W), math.Min(r.Y+r.H, o.Y+o.H)
	if x1 <= x0 || y1 <= y0 {
		return Rect{}
	}
	return Rect{X: x0, Y: y0, W: x1 - x0, H: y1 - y0}
}

// Center is the middle of the rectangle.
func (r Rect) Center() (float64, float64) { return r.X + r.W/2, r.Y + r.H/2 }

// Union is the smallest rectangle holding every one given: the virtual screen of a set of displays.
func Union(rs ...Rect) Rect {
	var u Rect
	first := true
	for _, r := range rs {
		if r.Empty() {
			continue
		}
		if first {
			u, first = r, false
			continue
		}
		x0, y0 := math.Min(u.X, r.X), math.Min(u.Y, r.Y)
		x1, y1 := math.Max(u.X+u.W, r.X+r.W), math.Max(u.Y+u.H, r.Y+r.H)
		u = Rect{X: x0, Y: y0, W: x1 - x0, H: y1 - y0}
	}
	return u
}

// Display is one screen.
type Display struct {
	ID      string `json:"id"`
	Name    string `json:"name,omitempty"`
	Bounds  Rect   `json:"bounds"`
	Primary bool   `json:"primary"`
	// PixelsPerUnit is captured pixels per desktop unit: 2 on a Retina display, whose units are
	// points; 1 where the units are pixels already.
	PixelsPerUnit float64 `json:"pixels_per_unit"`
	// Scale is the scale factor the operating system shows for the display (Windows' 125 %, macOS's
	// backing scale, 1 on X11). It is information for a person; the arithmetic uses PixelsPerUnit.
	Scale float64 `json:"scale"`
}

// PixelSize is the display's size in captured pixels.
func (d Display) PixelSize() (int, int) {
	ppu := d.PixelsPerUnit
	if ppu <= 0 {
		ppu = 1
	}
	return int(math.Round(d.Bounds.W * ppu)), int(math.Round(d.Bounds.H * ppu))
}

// DisplayAt is the display holding the point.
func DisplayAt(ds []Display, x, y float64) (Display, bool) {
	for _, d := range ds {
		if d.Bounds.Contains(x, y) {
			return d, true
		}
	}
	return Display{}, false
}

// DisplayOf is the display holding the larger part of r, which is where a window is said to be: a
// window straddling two displays is captured from the one that shows most of it.
func DisplayOf(ds []Display, r Rect) (Display, bool) {
	best, area, found := Display{}, 0.0, false
	for _, d := range ds {
		o := d.Bounds.Intersect(r)
		if a := o.W * o.H; a > area {
			best, area, found = d, a, true
		}
	}
	return best, found
}

// Fit is the size a picture of w×h takes when its long edge must not pass maxEdge: scaled down with
// its aspect kept, never up, each side at least one pixel. maxEdge of 0 or less keeps the size.
func Fit(w, h, maxEdge int) (int, int) {
	if maxEdge <= 0 || (w <= maxEdge && h <= maxEdge) || w <= 0 || h <= 0 {
		return w, h
	}
	if w >= h {
		return maxEdge, max(1, int(math.Round(float64(h)*float64(maxEdge)/float64(w))))
	}
	return max(1, int(math.Round(float64(w)*float64(maxEdge)/float64(h)))), maxEdge
}

// Frame binds a screenshot to the part of the desktop it shows.
type Frame struct {
	// Region is what the picture shows, in desktop units.
	Region Rect `json:"region"`
	// Width and Height are the picture's size in screenshot pixels.
	Width  int `json:"width"`
	Height int `json:"height"`
}

// ErrOutsideFrame is a point that is not on the picture at all.
var ErrOutsideFrame = errors.New("the point is outside the screenshot")

// ToDesktop is where screenshot pixel (x, y) is on the desktop: the centre of the area the pixel
// stands for. Taking the centre, not the corner, keeps a point on the element it was read from at any
// downscale; and with the centre there is no rounding to disagree about, which is the bug that moved
// clicks by a pixel when one side rounded half up and the other half to even. (0, 0) is the top-left
// pixel, a real coordinate, never "missing".
func (f Frame) ToDesktop(x, y int) (float64, float64, error) {
	if f.Width <= 0 || f.Height <= 0 || f.Region.Empty() {
		return 0, 0, errors.New("the screenshot has no size")
	}
	if x < 0 || y < 0 || x >= f.Width || y >= f.Height {
		return 0, 0, fmt.Errorf("%w: (%d, %d) on a %d×%d picture", ErrOutsideFrame, x, y, f.Width, f.Height)
	}
	return f.Region.X + (float64(x)+0.5)*f.Region.W/float64(f.Width),
		f.Region.Y + (float64(y)+0.5)*f.Region.H/float64(f.Height), nil
}

// FromDesktop is the screenshot pixel showing desktop point (x, y), and whether the point is on the
// picture at all.
func (f Frame) FromDesktop(x, y float64) (int, int, bool) {
	if f.Width <= 0 || f.Height <= 0 || f.Region.Empty() {
		return 0, 0, false
	}
	px := int(math.Floor((x - f.Region.X) * float64(f.Width) / f.Region.W))
	py := int(math.Floor((y - f.Region.Y) * float64(f.Height) / f.Region.H))
	return px, py, px >= 0 && py >= 0 && px < f.Width && py < f.Height
}

// UnitsPerPixel is how many desktop units one screenshot pixel spans, across and down.
func (f Frame) UnitsPerPixel() (float64, float64) {
	if f.Width <= 0 || f.Height <= 0 {
		return 0, 0
	}
	return f.Region.W / float64(f.Width), f.Region.H / float64(f.Height)
}

// Snap is the device pixel holding a desktop point, for the backends whose input takes whole
// numbers (X11, Windows): the floor, so the pixel is the one the point is inside, also left of zero.
func Snap(v float64) int { return int(math.Floor(v)) }

// NormalizeVirtual is pixel (x, y) in the 0–65535 coordinates Windows' SendInput takes with
// MOUSEEVENTF_ABSOLUTE|MOUSEEVENTF_VIRTUALDESK, over the virtual screen virt. Without VIRTUALDESK the
// range covers the primary display only and every other display is unreachable; and the plain
// x*65535/width lands one pixel short for most x. Windows maps n back to floor(n*width/65536), so n is
// the smallest value whose image is x: a ceiling division.
func NormalizeVirtual(x, y int, virt Rect) (int32, int32) {
	norm := func(v int, origin, size float64) int32 {
		s := int64(size)
		if s <= 0 {
			return 0
		}
		off := int64(v) - int64(origin)
		off = min(max(off, 0), s-1)
		return int32((off*65536 + s - 1) / s)
	}
	return norm(x, virt.X, virt.W), norm(y, virt.Y, virt.H)
}
