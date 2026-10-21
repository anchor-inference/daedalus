package desk

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"image"
	"strconv"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/shot"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Shot is what the daemon remembers of a screenshot it handed out: enough to turn a pixel of it into
// a desktop point later, and to tell when the desktop no longer looks the way the picture showed it.
type Shot struct {
	ID         string
	Frame      geom.Frame
	Display    string
	Window     string
	WindowRect geom.Rect
	Lease      string
	DisplayGen int
	At         time.Time
}

// ScreenshotParams is screenshot's.
type ScreenshotParams struct {
	Session string `json:"session"`
	// Display or Window chooses what is captured; neither means the lease's own window or display,
	// or the primary display.
	Display string `json:"display,omitempty"`
	Window  string `json:"window,omitempty"`
	// MaxEdge is the longest side of the picture in pixels; the daemon's max_edge when 0.
	MaxEdge int    `json:"max_edge,omitempty"`
	Format  string `json:"format,omitempty"`
	Quality int    `json:"quality,omitempty"`
}

// ScreenshotResult is screenshot's answer.
type ScreenshotResult struct {
	ID     string `json:"screenshot_id"`
	Format string `json:"format"`
	// Data is the picture, base64.
	Data   string `json:"data"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
	// Display is the display the picture is of; Window the window, when one was asked for.
	Display string `json:"display"`
	Window  string `json:"window,omitempty"`
	// Region is what the picture shows, in desktop units.
	Region geom.Rect `json:"region"`
	// UnitsPerPixel is how many desktop units one picture pixel spans, across and down.
	UnitsPerPixel [2]float64 `json:"units_per_pixel"`
	// PixelsPerUnit and Scale are the display's (geom.Display).
	PixelsPerUnit float64 `json:"pixels_per_unit"`
	Scale         float64 `json:"scale"`
	Units         string  `json:"units"`
	// Cursor is the pointer in picture pixels, when it is on the picture.
	Cursor *[2]int `json:"cursor,omitempty"`
}

// Screenshot captures for the session holding the lease.
func (d *Desk) Screenshot(ctx context.Context, p ScreenshotParams) (ScreenshotResult, error) {
	res, err := d.screenshot(ctx, p)
	r := audit.Record{Role: "host", Event: "screenshot", Session: p.Session}
	if err != nil {
		r.Event, r.Code, r.Reason = "refused", codeOf(err), "screenshot"
	} else {
		r.Screenshot, r.Bytes = res.ID, len(res.Data)
	}
	d.write(r)
	return res, err
}

func (d *Desk) screenshot(ctx context.Context, p ScreenshotParams) (ScreenshotResult, error) {
	format := p.Format
	if format == "" {
		format = shot.JPEG
	}
	if format != shot.JPEG && format != shot.PNG {
		return ScreenshotResult{}, wire.Errorf(wire.CodeInvalidParams, "format must be jpeg or png")
	}
	quality := p.Quality
	if quality == 0 {
		quality = DefaultJPEGQuality
	}
	if quality < 1 || quality > 100 {
		return ScreenshotResult{}, wire.Errorf(wire.CodeInvalidParams, "quality must be 1–100")
	}
	maxEdge := p.MaxEdge
	if maxEdge == 0 {
		maxEdge = d.cfg.MaxEdge
	}
	if maxEdge < 64 || maxEdge > 8192 {
		return ScreenshotResult{}, wire.Errorf(wire.CodeInvalidParams, "max_edge must be 64–8192")
	}
	if p.Display != "" && p.Window != "" {
		return ScreenshotResult{}, wire.Errorf(wire.CodeInvalidParams, "give display or window, not both")
	}
	d.mu.Lock()
	l, err := d.holderLocked(p.Session)
	var lease Lease
	if l != nil {
		lease = *l
	}
	d.mu.Unlock()
	if err != nil {
		return ScreenshotResult{}, err
	}
	if err := d.checkSecure(ctx); err != nil {
		return ScreenshotResult{}, err
	}
	ds, err := d.refreshDisplays(ctx)
	if err != nil {
		return ScreenshotResult{}, fromBackend(err)
	}
	target, window := p.Display, p.Window
	if target == "" && window == "" {
		switch lease.Scope.Kind {
		case ScopeWindow:
			window = lease.Scope.Window
		case ScopeDisplay:
			target = lease.Scope.Display
		}
	}
	var disp geom.Display
	var region geom.Rect
	var win backend.Window
	if window != "" {
		win, err = d.findWindow(ctx, window)
		if err != nil {
			return ScreenshotResult{}, err
		}
		if d.Protected(win.App) {
			return ScreenshotResult{}, refuse(CodeProtected, "that window belongs to an app the daemon never touches", map[string]any{"app": win.App.ID})
		}
		if err := d.windowInScope(lease, win); err != nil {
			return ScreenshotResult{}, err
		}
		var ok bool
		if disp, ok = geom.DisplayOf(ds, win.Bounds); !ok {
			return ScreenshotResult{}, wire.Errorf(wire.CodeNotFound, "window %q is on no display", window)
		}
		region = win.Bounds.Intersect(disp.Bounds)
	} else {
		if target == "" {
			target = ds[0].ID
			for _, x := range ds {
				if x.Primary {
					target = x.ID
					break
				}
			}
		}
		found := false
		for _, x := range ds {
			if x.ID == target {
				disp, found = x, true
			}
		}
		if !found {
			return ScreenshotResult{}, wire.Errorf(wire.CodeNotFound, "no display %q", target)
		}
		switch lease.Scope.Kind {
		case ScopeWindow:
			return ScreenshotResult{}, refuse(CodeOutOfScope, "the lease covers one window: capture that window", map[string]any{"scope": lease.Scope})
		case ScopeDisplay:
			if disp.ID != lease.Scope.Display {
				return ScreenshotResult{}, refuse(CodeOutOfScope, "the lease covers another display", map[string]any{"scope": lease.Scope})
			}
		}
		region = disp.Bounds
	}
	if region.Empty() {
		return ScreenshotResult{}, wire.Errorf(wire.CodeNotFound, "nothing of that is on screen")
	}
	img, err := d.be.Capture(ctx, disp, region)
	if err != nil {
		return ScreenshotResult{}, fromBackend(err)
	}
	b := img.Bounds()
	w, h := geom.Fit(b.Dx(), b.Dy(), maxEdge)
	scaled := shot.Downscale(img, w, h)
	data, err := d.encode(scaled, format, quality)
	if err != nil {
		return ScreenshotResult{}, err
	}
	frame := geom.Frame{Region: region, Width: w, Height: h}

	d.mu.Lock()
	if d.lease == nil || d.lease.ID != lease.ID {
		// Stopped or revoked while capturing: the picture is not handed out.
		d.mu.Unlock()
		return ScreenshotResult{}, refuse(CodeNoLease, "the lease ended during the capture", map[string]any{"reason": d.ended.reason})
	}
	d.shotSerial++
	s := &Shot{ID: "s" + d.shotPrefix + "-" + strconv.FormatUint(d.shotSerial, 10), Frame: frame, Display: disp.ID,
		Window: win.ID, WindowRect: win.Bounds, Lease: lease.ID, DisplayGen: d.displayGen, At: d.now()}
	d.shots[s.ID] = s
	d.shotOrder = append(d.shotOrder, s.ID)
	for len(d.shotOrder) > keptScreenshots {
		delete(d.shots, d.shotOrder[0])
		d.shotOrder = d.shotOrder[1:]
	}
	d.mu.Unlock()

	upx, upy := frame.UnitsPerPixel()
	res := ScreenshotResult{ID: s.ID, Format: format, Data: data, Width: w, Height: h, Display: disp.ID, Window: win.ID,
		Region: region, UnitsPerPixel: [2]float64{upx, upy}, PixelsPerUnit: disp.PixelsPerUnit, Scale: disp.Scale,
		Units: d.be.Platform().Units}
	if cx, cy, err := d.be.Cursor(ctx); err == nil {
		if px, py, in := frame.FromDesktop(cx, cy); in {
			res.Cursor = &[2]int{px, py}
		}
	}
	return res, nil
}

// encode writes the picture and keeps it under a frame. A JPEG too large steps its quality down
// rather than failing; a PNG cannot, so it is refused with what to ask for instead.
func (d *Desk) encode(img *image.RGBA, format string, quality int) (string, error) {
	for {
		raw, err := shot.Encode(img, format, quality)
		if err != nil {
			return "", err
		}
		if n := base64.StdEncoding.EncodedLen(len(raw)); n <= maxScreenshotData {
			return base64.StdEncoding.EncodeToString(raw), nil
		}
		if format != shot.JPEG || quality <= 30 {
			return "", wire.Errorf(wire.CodeLimit, "the %s picture is %d bytes, more than a reply carries: ask for jpeg or a smaller max_edge", format, len(raw))
		}
		quality -= 20
	}
}

// clearShotsLocked forgets every screenshot: a new or ended lease starts with none.
func (d *Desk) clearShotsLocked() {
	d.shots = map[string]*Shot{}
	d.shotOrder = nil
}

// resolveShot finds a screenshot id that is still good for the lease, or says why it is not.
func (d *Desk) resolveShot(ctx context.Context, id string, lease Lease) (*Shot, error) {
	d.mu.Lock()
	s := d.shots[id]
	gen := d.displayGen
	d.mu.Unlock()
	stale := func(reason, msg string) error {
		return refuse(CodeStale, msg+": take a new screenshot", map[string]any{"screenshot_id": id, "reason": reason})
	}
	switch {
	case s == nil:
		return nil, stale("unknown", "no such screenshot")
	case s.Lease != lease.ID:
		return nil, stale("other_lease", "the screenshot was taken under another lease")
	case d.now().Sub(s.At) > time.Duration(d.cfg.ScreenshotTTLs)*time.Second:
		return nil, stale("old", "the screenshot is too old")
	}
	if _, err := d.refreshDisplays(ctx); err == nil {
		d.mu.Lock()
		gen = d.displayGen
		d.mu.Unlock()
	}
	if s.DisplayGen != gen {
		return nil, stale("displays_changed", "the displays changed since the screenshot")
	}
	if s.Window != "" {
		w, err := d.findWindow(ctx, s.Window)
		if err != nil {
			return nil, stale("window_gone", "the window of the screenshot is gone")
		}
		if w.Bounds != s.WindowRect {
			return nil, stale("window_moved", "the window moved or changed size since the screenshot")
		}
	}
	return s, nil
}

// checkSecure refuses on a locked screen or a secure desktop.
func (d *Desk) checkSecure(ctx context.Context) error {
	sec, err := d.be.Secure(ctx)
	if err != nil {
		d.log.Debug("secure state", "error", err.Error())
		return nil
	}
	if sec.Locked {
		return refuse(CodeSecure, fmt.Sprintf("the desktop is not one the agent may see or drive (%s)", sec.Reason),
			map[string]any{"reason": sec.Reason})
	}
	return nil
}

// codeOf is an error's contract code, for the audit.
func codeOf(err error) int {
	var we *wire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return wire.CodeInternal
}
