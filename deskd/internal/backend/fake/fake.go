// Package fake is a desktop that exists only in memory: displays and windows from a scene, a capture
// that paints each window in its own colour, and input that is recorded instead of sent. The daemon's
// protocol tests run against it, and so can the host's: `deskd serve --backend fake --scene <file>`
// serves it on the real socket, and the operator's `fake.simulate` stands in for the operator's
// hand, a locked screen or a withdrawn permission.
package fake

import (
	"context"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"math"
	"os"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// Scene is the desktop the fake shows.
type Scene struct {
	Displays []geom.Display `json:"displays"`
	// Windows are front to back.
	Windows []SceneWindow `json:"windows"`
	// Focused is the id of the window with the keyboard.
	Focused string `json:"focused,omitempty"`
}

// SceneWindow is one window and the colour it is painted.
type SceneWindow struct {
	backend.Window
	Color [3]uint8 `json:"color"`
}

// DefaultScene is a Retina laptop display with a 1080p display to its left, an editor and a terminal
// on them, and a window of the app the daemon must never touch (the protected list of the tests names
// it).
func DefaultScene() Scene {
	return Scene{
		Displays: []geom.Display{
			{ID: "1", Name: "Built-in", Bounds: geom.Rect{X: 0, Y: 0, W: 1440, H: 900}, Primary: true, PixelsPerUnit: 2, Scale: 2},
			{ID: "2", Name: "External", Bounds: geom.Rect{X: -1920, Y: 0, W: 1920, H: 1080}, PixelsPerUnit: 1, Scale: 1},
		},
		Windows: []SceneWindow{
			{Window: backend.Window{ID: "10", Title: "notes.txt", Bounds: geom.Rect{X: 100, Y: 100, W: 800, H: 600},
				App: backend.App{ID: "com.example.editor", Name: "Editor", PID: 501, BundleID: "com.example.editor", ExePath: "/Applications/Editor.app/Contents/MacOS/Editor"}},
				Color: [3]uint8{255, 0, 0}},
			{Window: backend.Window{ID: "11", Title: "Daedalus", Bounds: geom.Rect{X: 950, Y: 100, W: 400, H: 700},
				App: backend.App{ID: "com.example.daedalus", Name: "Daedalus", PID: 502, BundleID: "com.example.daedalus", ExePath: "/Applications/Daedalus.app/Contents/MacOS/Daedalus"}},
				Color: [3]uint8{0, 0, 255}},
			{Window: backend.Window{ID: "12", Title: "shell", Bounds: geom.Rect{X: -1800, Y: 200, W: 1000, H: 700},
				App: backend.App{ID: "com.example.terminal", Name: "Terminal", PID: 503, BundleID: "com.example.terminal"}},
				Color: [3]uint8{0, 200, 0}},
			{Window: backend.Window{ID: "13", Title: "Administrator: setup", Bounds: geom.Rect{X: -700, Y: 300, W: 500, H: 400},
				App: backend.App{ID: "pid:504", Name: "setup", PID: 504, ExePath: `C:\Tools\setup.exe`, Elevated: true}},
				Color: [3]uint8{128, 128, 128}},
		},
		Focused: "10",
	}
}

// LoadScene reads a scene from a JSON file.
func LoadScene(path string) (Scene, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Scene{}, err
	}
	var s Scene
	if err := json.Unmarshal(b, &s); err != nil {
		return Scene{}, fmt.Errorf("scene %s: %w", path, err)
	}
	if len(s.Displays) == 0 {
		return Scene{}, fmt.Errorf("scene %s has no displays", path)
	}
	return s, nil
}

// Input is one recorded synthetic event.
type Input struct {
	Kind   string  `json:"kind"` // move, button, scroll, key, type
	X      float64 `json:"x,omitempty"`
	Y      float64 `json:"y,omitempty"`
	Button string  `json:"button,omitempty"`
	Down   bool    `json:"down,omitempty"`
	DX     int     `json:"dx,omitempty"`
	DY     int     `json:"dy,omitempty"`
	Key    string  `json:"key,omitempty"`
	Text   string  `json:"text,omitempty"`
	// Window is the window under the pointer (a button or scroll) or with the keyboard (a key or
	// text) when the event was sent: where it would have landed.
	Window string `json:"window,omitempty"`
}

// Fake is the in-memory desktop.
type Fake struct {
	mu        sync.Mutex
	scene     Scene
	cursorX   float64
	cursorY   float64
	inputs    []Input
	perms     backend.Permissions
	secure    backend.Secure
	lastInput time.Time
	// InputDelay makes every input call take this long, so a test can stop or interrupt an action
	// half way through.
	InputDelay time.Duration
}

// New returns a fake showing scene, with every permission granted.
func New(scene Scene) *Fake {
	return &Fake{scene: scene, perms: backend.Permissions{Screen: backend.Granted, Accessibility: backend.Granted,
		InputMonitoring: backend.NotApplicable}}
}

func (f *Fake) Platform() backend.Platform {
	return backend.Platform{OS: "fake", SessionType: backend.SessionFake, Units: backend.UnitsPoints}
}

func (f *Fake) Displays(ctx context.Context) ([]geom.Display, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]geom.Display(nil), f.scene.Displays...), nil
}

func (f *Fake) Permissions(ctx context.Context) backend.Permissions {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.perms
}

// RequestPermission grants what is asked, as an operator answering the system's prompt would.
func (f *Fake) RequestPermission(ctx context.Context, which string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch which {
	case backend.PermScreen:
		f.perms.Screen = backend.Granted
	case backend.PermAccessibility:
		f.perms.Accessibility = backend.Granted
	case backend.PermInputMonitoring:
		f.perms.InputMonitoring = backend.Granted
	default:
		return fmt.Errorf("no permission %q", which)
	}
	return nil
}

// Capture paints the desktop's grey, then each window in its colour from back to front.
func (f *Fake) Capture(ctx context.Context, d geom.Display, region geom.Rect) (*image.RGBA, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.perms.Screen != backend.Granted {
		return nil, &backend.PermissionError{Which: backend.PermScreen}
	}
	ppu := d.PixelsPerUnit
	if ppu <= 0 {
		ppu = 1
	}
	w, h := int(math.Round(region.W*ppu)), int(math.Round(region.H*ppu))
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	paint := func(r geom.Rect, c color.RGBA) {
		o := r.Intersect(region)
		if o.Empty() {
			return
		}
		x0, y0 := int(math.Round((o.X-region.X)*ppu)), int(math.Round((o.Y-region.Y)*ppu))
		x1, y1 := int(math.Round((o.X+o.W-region.X)*ppu)), int(math.Round((o.Y+o.H-region.Y)*ppu))
		for y := y0; y < y1 && y < h; y++ {
			for x := x0; x < x1 && x < w; x++ {
				img.SetRGBA(x, y, c)
			}
		}
	}
	paint(region, color.RGBA{60, 60, 60, 255})
	for i := len(f.scene.Windows) - 1; i >= 0; i-- {
		sw := f.scene.Windows[i]
		if sw.Minimized {
			continue
		}
		paint(sw.Bounds, color.RGBA{sw.Color[0], sw.Color[1], sw.Color[2], 255})
	}
	return img, nil
}

func (f *Fake) windowsLocked() []backend.Window {
	out := make([]backend.Window, 0, len(f.scene.Windows))
	for _, sw := range f.scene.Windows {
		w := sw.Window
		w.Focused = w.ID == f.scene.Focused
		out = append(out, w)
	}
	return out
}

func (f *Fake) Windows(ctx context.Context) ([]backend.Window, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.windowsLocked(), nil
}

func (f *Fake) windowAtLocked(x, y float64) (backend.Window, bool) {
	for _, w := range f.windowsLocked() {
		if !w.Minimized && w.Bounds.Contains(x, y) {
			return w, true
		}
	}
	return backend.Window{}, false
}

func (f *Fake) WindowAt(ctx context.Context, x, y float64) (backend.Window, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w, ok := f.windowAtLocked(x, y)
	return w, ok, nil
}

func (f *Fake) foregroundLocked() (backend.Window, bool) {
	for _, w := range f.windowsLocked() {
		if w.Focused {
			return w, true
		}
	}
	return backend.Window{}, false
}

func (f *Fake) Foreground(ctx context.Context) (backend.Window, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w, ok := f.foregroundLocked()
	return w, ok, nil
}

// Activate brings the window to the front and gives it the keyboard.
func (f *Fake) Activate(ctx context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, sw := range f.scene.Windows {
		if sw.ID == id {
			ws := append([]SceneWindow{sw}, append(append([]SceneWindow(nil), f.scene.Windows[:i]...), f.scene.Windows[i+1:]...)...)
			f.scene.Windows = ws
			f.scene.Focused = id
			return nil
		}
	}
	return fmt.Errorf("no window %s", id)
}

func (f *Fake) Cursor(ctx context.Context) (float64, float64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cursorX, f.cursorY, nil
}

// record keeps an input and, like a real system, counts it as the latest input.
func (f *Fake) record(ctx context.Context, in Input) error {
	if f.InputDelay > 0 {
		select {
		case <-time.After(f.InputDelay):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.perms.Accessibility != backend.Granted {
		return &backend.PermissionError{Which: backend.PermAccessibility}
	}
	switch in.Kind {
	case "move":
		f.cursorX, f.cursorY = in.X, in.Y
	case "button", "scroll":
		if w, ok := f.windowAtLocked(f.cursorX, f.cursorY); ok {
			in.Window = w.ID
		}
		in.X, in.Y = f.cursorX, f.cursorY
	case "key", "type":
		if w, ok := f.foregroundLocked(); ok {
			in.Window = w.ID
		}
	}
	f.inputs = append(f.inputs, in)
	f.lastInput = time.Now()
	return nil
}

func (f *Fake) Move(ctx context.Context, x, y float64) error {
	return f.record(ctx, Input{Kind: "move", X: x, Y: y})
}

func (f *Fake) Button(ctx context.Context, b backend.Button, down bool) error {
	return f.record(ctx, Input{Kind: "button", Button: b.String(), Down: down})
}

func (f *Fake) Scroll(ctx context.Context, dx, dy int) error {
	return f.record(ctx, Input{Kind: "scroll", DX: dx, DY: dy})
}

func (f *Fake) Key(ctx context.Context, k keys.Key, down bool) error {
	return f.record(ctx, Input{Kind: "key", Key: k.String(), Down: down})
}

func (f *Fake) Type(ctx context.Context, text string) error {
	return f.record(ctx, Input{Kind: "type", Text: text})
}

func (f *Fake) Secure(ctx context.Context) (backend.Secure, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.secure, nil
}

func (f *Fake) LastInput(ctx context.Context) (time.Time, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lastInput, !f.lastInput.IsZero()
}

func (f *Fake) Close() error { return nil }

// Inputs returns the recorded events and forgets them.
func (f *Fake) Inputs() []Input {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := f.inputs
	f.inputs = nil
	return out
}

// Simulation is what fake.simulate can change.
type Simulation struct {
	// HumanInput is the operator touching the mouse: the pointer moves by (dx, dy) if given, and the
	// system's last input becomes now.
	HumanInput bool    `json:"human_input,omitempty"`
	DX         float64 `json:"dx,omitempty"`
	DY         float64 `json:"dy,omitempty"`
	// Secure sets the locked state; nil leaves it.
	Secure *backend.Secure `json:"secure,omitempty"`
	// Permissions replaces the grants; nil leaves them.
	Permissions *backend.Permissions `json:"permissions,omitempty"`
	// MoveWindow moves a window by (dx, dy).
	MoveWindow string `json:"move_window,omitempty"`
	// Displays replaces the displays; nil leaves them.
	Displays []geom.Display `json:"displays,omitempty"`
	// Focus gives a window the keyboard.
	Focus string `json:"focus,omitempty"`
}

// Simulate applies a simulation.
func (f *Fake) Simulate(s Simulation) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if s.HumanInput {
		f.cursorX += s.DX
		f.cursorY += s.DY
		f.lastInput = time.Now()
	}
	if s.Secure != nil {
		f.secure = *s.Secure
	}
	if s.Permissions != nil {
		f.perms = *s.Permissions
	}
	if s.Displays != nil {
		f.scene.Displays = s.Displays
	}
	if s.MoveWindow != "" {
		found := false
		for i := range f.scene.Windows {
			if f.scene.Windows[i].ID == s.MoveWindow {
				f.scene.Windows[i].Bounds.X += s.DX
				f.scene.Windows[i].Bounds.Y += s.DY
				found = true
			}
		}
		if !found {
			return fmt.Errorf("no window %s", s.MoveWindow)
		}
	}
	if s.Focus != "" {
		f.scene.Focused = s.Focus
	}
	return nil
}
