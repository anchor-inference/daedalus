package desk

import (
	"context"
	"image"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/shot"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The preview's sizes: small enough that a grid of them loads at once in the operator's picker, and
// capped so the operator's role cannot use preview as an unlimited screenshot.
const (
	DefaultPreviewEdge = 480
	MaxPreviewEdge     = 960
	minPreviewEdge     = 64
	previewQuality     = 70
)

// PreviewParams is preview's.
type PreviewParams struct {
	Display string `json:"display,omitempty"`
	Window  string `json:"window,omitempty"`
	MaxEdge int    `json:"max_edge,omitempty"`
}

// PreviewResult is preview's answer: a JPEG, base64.
type PreviewResult struct {
	Image   string `json:"image"`
	Width   int    `json:"width"`
	Height  int    `json:"height"`
	Display string `json:"display"`
	Window  string `json:"window,omitempty"`
}

// Preview is a thumbnail of a display or a window for the operator choosing what to grant. It needs
// no lease, because it is the operator looking at their own screen, and it hands out no screenshot
// id, so nothing the agent does can be aimed with it. A protected window is refused as everywhere
// else: Daedalus's own windows are never pictured on their own. A locked screen is refused too, so
// the picker never shows the lock screen as if it were a display.
//
// It is not audited: the audit records what was done to the desktop and by whom, and an operator's
// glance at a thumbnail is neither; a picker refreshing its thumbnails would only bury the record.
func (d *Desk) Preview(ctx context.Context, p PreviewParams) (PreviewResult, error) {
	maxEdge := p.MaxEdge
	if maxEdge == 0 {
		maxEdge = DefaultPreviewEdge
	}
	if maxEdge < minPreviewEdge {
		return PreviewResult{}, wire.Errorf(wire.CodeInvalidParams, "max_edge must be %d–%d", minPreviewEdge, MaxPreviewEdge)
	}
	maxEdge = min(maxEdge, MaxPreviewEdge)
	if p.Display != "" && p.Window != "" {
		return PreviewResult{}, wire.Errorf(wire.CodeInvalidParams, "give display or window, not both")
	}
	if err := d.checkSecure(ctx); err != nil {
		return PreviewResult{}, err
	}
	ds, err := d.refreshDisplays(ctx)
	if err != nil {
		return PreviewResult{}, fromBackend(err)
	}
	res := PreviewResult{}
	var disp geom.Display
	var region geom.Rect
	var win backend.Window
	if p.Window != "" {
		win, err = d.findWindow(ctx, p.Window)
		if err != nil {
			return PreviewResult{}, err
		}
		if d.Protected(win.App) {
			return PreviewResult{}, refuse(CodeProtected, "that window belongs to an app the daemon never touches", map[string]any{"app": win.App.ID})
		}
		var ok bool
		if disp, ok = geom.DisplayOf(ds, win.Bounds); !ok {
			return PreviewResult{}, wire.Errorf(wire.CodeNotFound, "window %q is on no display", p.Window)
		}
		region = win.Bounds.Intersect(disp.Bounds)
		res.Window = win.ID
	} else {
		disp = ds[0]
		found := p.Display == ""
		for _, x := range ds {
			if (p.Display == "" && x.Primary) || x.ID == p.Display {
				disp, found = x, true
				break
			}
		}
		if !found {
			return PreviewResult{}, wire.Errorf(wire.CodeNotFound, "no display %q", p.Display)
		}
		region = disp.Bounds
	}
	res.Display = disp.ID
	if region.Empty() {
		return PreviewResult{}, wire.Errorf(wire.CodeNotFound, "nothing of that is on screen")
	}
	img, err := d.previewCapture(ctx, win, disp, region)
	if err != nil {
		return PreviewResult{}, err
	}
	b := img.Bounds()
	res.Width, res.Height = geom.Fit(b.Dx(), b.Dy(), maxEdge)
	if res.Image, err = d.encode(shot.Downscale(img, res.Width, res.Height), shot.JPEG, previewQuality); err != nil {
		return PreviewResult{}, err
	}
	return res, nil
}

// previewCapture draws a window by itself where the backend can, so a window half under another
// still shows whole; otherwise, or when that fails, it captures the screen region the window covers.
func (d *Desk) previewCapture(ctx context.Context, win backend.Window, disp geom.Display, region geom.Rect) (*image.RGBA, error) {
	if wc, ok := d.be.(backend.WindowCapturer); ok && win.ID != "" {
		if img, err := wc.CaptureWindow(ctx, win); err == nil && !img.Bounds().Empty() {
			return img, nil
		} else if err != nil {
			d.log.Debug("window capture failed, capturing the screen instead", "window", win.ID, "error", err.Error())
		}
	}
	img, err := d.be.Capture(ctx, disp, region)
	if err != nil {
		return nil, fromBackend(err)
	}
	return img, nil
}
