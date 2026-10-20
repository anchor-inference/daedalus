package quartz

import (
	"fmt"
	"image"
	"math"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

// The CGEventType values of the mouse events (kCGEventLeftMouseDown and its siblings). They are part
// of the system's binary interface and have not changed since Quartz Event Services appeared.
const (
	eventLeftDown     = 1
	eventLeftUp       = 2
	eventRightDown    = 3
	eventRightUp      = 4
	eventMouseMoved   = 5
	eventLeftDragged  = 6
	eventRightDragged = 7
	eventOtherDown    = 25
	eventOtherUp      = 26
	eventOtherDragged = 27
)

// The CGMouseButton values.
const (
	cgButtonLeft   = 0
	cgButtonRight  = 1
	cgButtonCenter = 2
)

// buttonEvent is the event type and CGMouseButton of pressing or releasing b.
func buttonEvent(b backend.Button, down bool) (int, int, error) {
	switch b {
	case backend.Left:
		if down {
			return eventLeftDown, cgButtonLeft, nil
		}
		return eventLeftUp, cgButtonLeft, nil
	case backend.Right:
		if down {
			return eventRightDown, cgButtonRight, nil
		}
		return eventRightUp, cgButtonRight, nil
	case backend.Middle:
		if down {
			return eventOtherDown, cgButtonCenter, nil
		}
		return eventOtherUp, cgButtonCenter, nil
	}
	return 0, 0, fmt.Errorf("no mouse button %s", b)
}

// heldButtons is which mouse buttons we hold down, indexed by backend.Button.
type heldButtons [4]bool

// moveEvent is the event type and button of moving the pointer with held down. A move while a
// button is down must be a drag event: macOS does not infer the drag from the button state, so a
// plain mouseMoved there leaves the application thinking the button came up, and a selection or a
// dragged file stops where the press was.
func moveEvent(held heldButtons) (int, int) {
	switch {
	case held[backend.Left]:
		return eventLeftDragged, cgButtonLeft
	case held[backend.Right]:
		return eventRightDragged, cgButtonRight
	case held[backend.Middle]:
		return eventOtherDragged, cgButtonCenter
	}
	return eventMouseMoved, cgButtonLeft
}

// clickSlop is how far, in points, a second press may land from the first and still make a double
// click. The daemon presses twice at the same point, so exact equality would do for it, but a point
// read back from the cursor can carry a fraction the press did not.
const clickSlop = 4.0

// clickCounter numbers presses the way the window server does, so the click state we put on an event
// matches what a person's double click carries. Unlike X11, where the toolkit counts clicks itself,
// a macOS application reads kCGMouseEventClickState from the event: two presses that both say 1 are
// two single clicks, and a word is never selected.
type clickCounter struct {
	button backend.Button
	x, y   float64
	at     time.Time
	count  int
}

// press records a press of b at (x, y) at now and returns its click count.
func (c *clickCounter) press(b backend.Button, x, y float64, now time.Time, interval time.Duration) int {
	if c.count > 0 && b == c.button && now.Sub(c.at) <= interval &&
		math.Abs(x-c.x) <= clickSlop && math.Abs(y-c.y) <= clickSlop {
		c.count++
	} else {
		c.count = 1
	}
	c.button, c.x, c.y, c.at = b, x, y, now
	return c.count
}

// release is the click count of releasing b: the same as its press, which is what a real release
// carries; 1 if b was not the button last pressed.
func (c *clickCounter) release(b backend.Button) int {
	if c.count > 0 && b == c.button {
		return c.count
	}
	return 1
}

// wheelAxes is the two wheel values of CGEventCreateScrollWheelEvent for a scroll of dx, dy notches.
// Quartz counts the wheel the way a person turns it: wheel1 positive is the wheel rolled away, which
// scrolls up, and wheel2 positive scrolls left. The backend's dy > 0 is down and dx > 0 is right, so
// both are negated. The operator's "natural scrolling" setting is applied by the system to hardware
// events and is not expected to flip events posted this way.
// [unverified on a real machine: posted events and natural scrolling]
func wheelAxes(dx, dy int) (wheel1, wheel2 int32) {
	return int32(-dy), int32(-dx)
}

// rgbaFromRows copies a w×h picture of RGBA rows, stride bytes apart, into an image. The capture is
// drawn into a premultiplied RGBA bitmap, and image.RGBA is premultiplied too, so the bytes copy as
// they are; only the stride may differ.
func rgbaFromRows(w, h, stride int, data []byte) (*image.RGBA, error) {
	if w <= 0 || h <= 0 || stride < w*4 || len(data) < stride*(h-1)+w*4 {
		return nil, fmt.Errorf("a capture of %d×%d with %d bytes a row does not fit %d bytes", w, h, stride, len(data))
	}
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		copy(img.Pix[y*img.Stride:y*img.Stride+w*4], data[y*stride:y*stride+w*4])
	}
	return img, nil
}

// allZero reports whether every byte of the picture is zero, alpha included. A real capture of a
// screen is opaque, so even a black one has alpha 255; transparent black everywhere is what the
// window server hands a program whose grant it does not honour.
func allZero(img *image.RGBA) bool {
	for _, b := range img.Pix {
		if b != 0 {
			return false
		}
	}
	return true
}
