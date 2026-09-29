package page

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The bounds of what one read returns.
const (
	DefaultMaxChars = 40000
	MaxChars        = 200000
)

func maxChars(n int) (int, error) {
	if n == 0 {
		return DefaultMaxChars, nil
	}
	if n < 100 || n > MaxChars {
		return 0, wire.Errorf(wire.CodeInvalidParams, "max_chars must be 100-%d", MaxChars)
	}
	return n, nil
}

// Snapshot is page.snapshot's result.
type Snapshot struct {
	URL       string   `json:"url"`
	Title     string   `json:"title"`
	Text      string   `json:"text"`
	Refs      int      `json:"refs"`
	Truncated bool     `json:"truncated"`
	Frames    []Frame  `json:"frames"`
	Viewport  Size     `json:"viewport"`
	Scroll    Scrolled `json:"scroll"`
	Loading   bool     `json:"loading"`
}

// Frame is a frame the snapshot met. Read says a frame of another site's document is in the outline.
type Frame struct {
	Ref         string `json:"ref"`
	URL         string `json:"url"`
	CrossOrigin bool   `json:"cross_origin"`
	Read        bool   `json:"read"`
}

// Size is a viewport's, in CSS pixels.
type Size struct {
	W float64 `json:"w"`
	H float64 `json:"h"`
}

// Scrolled is how far the page is scrolled: the window's, or the pane's that scrolls in its place,
// in pixels and in screens of that pane.
type Scrolled struct {
	Top    float64 `json:"top"`
	Height float64 `json:"height"`
	View   float64 `json:"view"`
	Above  float64 `json:"above"`
	Below  float64 `json:"below"`
	// Pane is the ref of the pane that scrolls in the window's place, "" when the window scrolls.
	Pane string `json:"pane,omitempty"`
}

// outlined is what the script's snapshot answers: the outline in its parts, for the frames of other
// sites to be spliced in.
type outlined struct {
	URL       string   `json:"url"`
	Title     string   `json:"title"`
	Header    []string `json:"header"`
	Lines     []string `json:"lines"`
	Tail      []string `json:"tail"`
	Refs      int      `json:"refs"`
	Truncated bool     `json:"truncated"`
	Used      int      `json:"used"`
	Frames    []struct {
		Ref         string `json:"ref"`
		URL         string `json:"url"`
		CrossOrigin bool   `json:"cross_origin"`
		Line        int    `json:"line"`
		Depth       int    `json:"depth"`
	} `json:"frames"`
	Viewport Size     `json:"viewport"`
	Scroll   Scrolled `json:"scroll"`
	Loading  bool     `json:"loading"`
}

// Views the snapshot has: the whole page, or what is on screen and half a screen around it.
const (
	ViewPage     = "page"
	ViewViewport = "viewport"
)

type snapOpts struct {
	View   string     `json:"view,omitempty"`
	Band   [2]float64 `json:"band,omitzero"`
	Header bool       `json:"header"`
	Marks  bool       `json:"marks"`
}

// Snapshot is the page's outline with refs, of one element's subtree when scope is set. The
// documents of frames of other sites are read in their own worlds and spliced under their frame's
// line, while the budget lasts.
func (p *Model) Snapshot(ctx context.Context, t *browser.Tab, scope string, max int, view string) (*Snapshot, error) {
	n, err := maxChars(max)
	if err != nil {
		return nil, err
	}
	if view == "" {
		view = ViewPage
	}
	if view != ViewPage && view != ViewViewport {
		return nil, wire.Errorf(wire.CodeInvalidParams, "view must be page or viewport")
	}
	d, local := top, ""
	if scope != "" {
		if d, local, err = p.docOf(ctx, t, scope); err != nil {
			return nil, err
		}
	}
	opts := snapOpts{View: view, Header: scope == "", Marks: true}
	o, err := p.outlineIn(ctx, t, d, local, n, opts)
	if err != nil {
		return nil, err
	}
	s := &Snapshot{URL: o.URL, Title: o.Title, Refs: o.Refs, Truncated: o.Truncated, Frames: []Frame{}, Viewport: o.Viewport,
		Scroll: o.Scroll, Loading: o.Loading}
	if d != top {
		// A scoped snapshot of a frame's document is still the tab's page.
		s.URL, s.Title = t.URL(), t.Title()
	}
	lines := p.splice(ctx, t, d, o, n-o.Used, opts, s, 0)
	s.Text = strings.Join(append(append(o.Header, lines...), o.Tail...), "\n")
	return s, nil
}

func (p *Model) outlineIn(ctx context.Context, t *browser.Tab, d *doc, scope string, n int, opts snapOpts) (*outlined, error) {
	p.markListeners(ctx, t, d)
	raw, err := p.callIn(ctx, t, d, "snapshot", scope, n, opts)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var o outlined
	if err := json.Unmarshal(raw, &o); err != nil {
		return nil, err
	}
	return &o, nil
}

// splice reads the frames of other sites o met and puts their lines under their frame's line, each
// with at most the budget left, and records the frames in s. A frame it cannot read says why on its
// line.
func (p *Model) splice(ctx context.Context, t *browser.Tab, d *doc, o *outlined, left int, opts snapOpts, s *Snapshot, depth int) []string {
	under := map[int][]string{}
	note := map[int]string{}
	for _, f := range o.Frames {
		fr := Frame{Ref: f.Ref, URL: f.URL, CrossOrigin: f.CrossOrigin}
		if f.CrossOrigin {
			switch {
			case depth >= maxFrameDepth || len(s.Frames) >= maxCrossFrames:
				note[f.Line] = " (another site's frame; not read: too many frames; scope=" + f.Ref + " reads it)"
			case left < 300:
				note[f.Line] = " (another site's frame; not read: the outline is full; scope=" + f.Ref + " reads it)"
			default:
				lines, err := p.frameLines(ctx, t, d, f.Ref, left, opts, s, depth)
				if err != nil {
					note[f.Line] = " (another site's frame; not read: " + reason(err) + ")"
					break
				}
				fr.Read = true
				under[f.Line] = indent(lines, f.Depth+1)
				for _, l := range lines {
					left -= len(l) + 2*(f.Depth+1) + 1
				}
			}
		}
		s.Frames = append(s.Frames, fr)
	}
	if len(under) == 0 && len(note) == 0 {
		return o.Lines
	}
	out := make([]string, 0, len(o.Lines))
	for i, l := range o.Lines {
		out = append(out, l+note[i])
		out = append(out, under[i]...)
	}
	return out
}

// frameLines is the outline of the document of frame fref of d, its own frames of other sites in it.
func (p *Model) frameLines(ctx context.Context, t *browser.Tab, d *doc, fref string, left int, opts snapOpts, s *Snapshot, depth int) ([]string, error) {
	c, err := p.child(ctx, t, d, fref, false)
	if err != nil {
		return nil, err
	}
	co := snapOpts{View: opts.View, Marks: opts.Marks}
	if opts.View == ViewViewport {
		// The band is the tab's viewport's, in the frame's own coordinates.
		co.Band = [2]float64{-0.5*s.Viewport.H - c.off.Y, 1.5*s.Viewport.H - c.off.Y}
	}
	o, err := p.outlineIn(ctx, t, c, "", max(100, left), co)
	if err != nil {
		return nil, err
	}
	s.Refs += o.Refs
	s.Truncated = s.Truncated || o.Truncated
	lines := p.splice(ctx, t, c, o, left-o.Used, opts, s, depth+1)
	return append(lines, o.Tail...), nil
}

// reason is an error in a few words for a line of the outline.
func reason(err error) string {
	var we *wire.Error
	if asWire(err, &we) {
		return we.Message
	}
	msg := err.Error()
	if len(msg) > 120 {
		msg = msg[:120]
	}
	return msg
}

// Text is page.text's result.
type Text struct {
	URL       string `json:"url"`
	Title     string `json:"title"`
	Text      string `json:"text"`
	Truncated bool   `json:"truncated"`
}

// Text is the page's readable text, or one element's.
func (p *Model) Text(ctx context.Context, t *browser.Tab, ref string, max int) (*Text, error) {
	n, err := maxChars(max)
	if err != nil {
		return nil, err
	}
	d, local := top, ref
	if ref != "" {
		if d, local, err = p.docOf(ctx, t, ref); err != nil {
			return nil, err
		}
	}
	raw, err := p.callIn(ctx, t, d, "readable", local, n)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var out Text
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	if d != top {
		out.URL, out.Title = t.URL(), t.Title()
	}
	return &out, nil
}

// ScreenshotParams are page.screenshot's.
type ScreenshotParams struct {
	TabID    string          `json:"tab_id"`
	Ref      string          `json:"ref,omitempty"`
	FullPage bool            `json:"full_page,omitempty"`
	MaxWidth int             `json:"max_width,omitempty"`
	Format   string          `json:"format,omitempty"`
	Quality  int             `json:"quality,omitempty"`
	Origin   *browser.Origin `json:"origin,omitempty"`
}

// Screenshot is page.screenshot's result.
type Screenshot struct {
	Format string   `json:"format"`
	Width  int      `json:"width"`
	Height int      `json:"height"`
	Data   string   `json:"data_b64"`
	Masked []string `json:"masked"`
}

// Screenshot captures the viewport, the whole page, or one element, with every secret field hidden
// for the capture.
func (p *Model) Screenshot(ctx context.Context, t *browser.Tab, sp ScreenshotParams) (*Screenshot, error) {
	format := sp.Format
	if format == "" {
		format = "jpeg"
	}
	if format != "jpeg" && format != "png" {
		return nil, wire.Errorf(wire.CodeInvalidParams, "format must be jpeg or png")
	}
	quality := sp.Quality
	if quality == 0 {
		quality = 80
	}
	if quality < 30 || quality > 100 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "quality must be 30-100")
	}
	maxW := sp.MaxWidth
	if maxW == 0 {
		maxW = 1280
	}
	if maxW < 64 || maxW > 2560 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_width must be 64-2560")
	}
	var x, y, w, h float64
	beyond := false
	switch {
	case sp.Ref != "":
		d, local, err := p.docOf(ctx, t, sp.Ref)
		if err != nil {
			return nil, err
		}
		pr, err := p.prepare(ctx, t, d, local)
		if err != nil {
			return nil, err
		}
		x, y, w, h = pr.Box.X, pr.Box.Y, pr.Box.W, pr.Box.H
	case sp.FullPage:
		var lm struct {
			CSSContentSize struct {
				Width  float64 `json:"width"`
				Height float64 `json:"height"`
			} `json:"cssContentSize"`
		}
		if err := t.Call(ctx, "Page.getLayoutMetrics", nil, &lm); err != nil {
			return nil, err
		}
		w, h = lm.CSSContentSize.Width, math.Min(lm.CSSContentSize.Height, 16000)
		beyond = true
	default:
		vp := t.Group.ViewportNow()
		w, h = float64(vp.W), float64(vp.H)
	}
	if w < 1 || h < 1 {
		return nil, wire.Errorf(wire.CodeForbidden, "the element has no size on the page")
	}
	scale := math.Min(1, float64(maxW)/w)
	// Every document's secret fields are hidden, the frames' of other sites too: each world masks
	// its own.
	cross, missed := p.crossDocs(ctx, t, top, maxMaskedFrames)
	if missed > 0 {
		return nil, wire.Errorf(wire.CodeForbidden, "%d frame(s) of another site could not be reached to hide their secret fields; no screenshot is taken", missed)
	}
	docs := append([]*doc{top}, cross...)
	masked := []string{}
	defer func() {
		// The masks come off whatever happened to the capture.
		uctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		for _, d := range docs {
			_, _ = p.callIn(uctx, t, d, "mask", false)
		}
	}()
	for i, d := range docs {
		raw, err := p.callIn(ctx, t, d, "mask", true)
		if err != nil {
			if i == 0 {
				return nil, err
			}
			// A frame that cannot be masked is not captured with its secrets showing.
			return nil, wire.Errorf(wire.CodeForbidden, "a frame's secret fields could not be hidden for the screenshot: %s", reason(err))
		}
		var m struct {
			Masked []string `json:"masked"`
		}
		_ = json.Unmarshal(raw, &m)
		masked = append(masked, m.Masked...)
	}
	params := map[string]any{"format": format, "clip": map[string]any{"x": x, "y": y, "width": w, "height": h, "scale": scale},
		"captureBeyondViewport": beyond}
	if format == "jpeg" {
		params["quality"] = quality
	}
	var shot struct {
		Data string `json:"data"`
	}
	if err := t.Call(ctx, "Page.captureScreenshot", params, &shot); err != nil {
		return nil, err
	}
	return &Screenshot{Format: format, Width: int(math.Round(w * scale)), Height: int(math.Round(h * scale)), Data: shot.Data,
		Masked: masked}, nil
}

// Wait waits for a condition on the page, at most timeout.
func (p *Model) Wait(ctx context.Context, t *browser.Tab, what, value string, timeout time.Duration) (map[string]any, error) {
	wctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	result := func(matched string) map[string]any {
		return map[string]any{"matched": matched, "url": t.URL()}
	}
	switch what {
	case "load", "idle":
		name := "load"
		if what == "idle" {
			name = "networkIdle"
		}
		if err := t.WaitLifecycle(wctx, "", name); err != nil {
			if wctx.Err() != nil && ctx.Err() == nil {
				return result("timeout"), nil
			}
			return nil, err
		}
		return result(what), nil
	case "text", "gone", "url":
		if value == "" {
			return nil, wire.Errorf(wire.CodeInvalidParams, "waiting for %s needs a value", what)
		}
	default:
		return nil, wire.Errorf(wire.CodeInvalidParams, "for must be load, idle, text, gone or url")
	}
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	for {
		ok, err := p.check(wctx, t, what, value)
		if err != nil && wctx.Err() == nil {
			var we *wire.Error
			// A document replaced under the check is only a moment to check again.
			if !asWire(err, &we) || we.Code == browser.CodeDialogOpen || we.Code == browser.CodeNoSuchTab {
				return nil, err
			}
		}
		if ok {
			return result(what), nil
		}
		select {
		case <-tick.C:
		case <-wctx.Done():
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return result("timeout"), nil
		}
	}
}

func (p *Model) check(ctx context.Context, t *browser.Tab, what, value string) (bool, error) {
	switch what {
	case "url":
		raw, err := p.call(ctx, t, "href")
		if err != nil {
			return false, err
		}
		var href string
		_ = json.Unmarshal(raw, &href)
		return strings.Contains(href, value), nil
	case "text":
		raw, err := p.call(ctx, t, "hasText", value)
		if err != nil {
			return false, err
		}
		return string(raw) == "true", nil
	case "gone":
		// A ref, or else a text, that is no longer on the page.
		if isRef(value) {
			d, local, err := p.docOf(ctx, t, value)
			var we *wire.Error
			if asWire(err, &we) && we.Code == browser.CodeStaleRef {
				return true, nil
			}
			if err != nil {
				return false, err
			}
			raw, err := p.callIn(ctx, t, d, "exists", local)
			if err != nil {
				return false, err
			}
			return string(raw) == "false", nil
		}
		raw, err := p.call(ctx, t, "hasText", value)
		if err != nil {
			return false, err
		}
		return string(raw) == "false", nil
	}
	return false, nil
}

// isRef says s is an element's ref: e<n>, after the refs of the frames it is in (f2e7, f2f1e3).
func isRef(s string) bool {
	i := strings.LastIndexByte(s, 'e')
	if i < 0 || i == len(s)-1 || (i > 0 && !isFrameRef(s[:i])) {
		return false
	}
	for _, c := range s[i+1:] {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// AnswerDialog accepts or dismisses the page's dialog.
func (p *Model) AnswerDialog(ctx context.Context, t *browser.Tab, accept bool, text string) error {
	if t.Dialog() == nil {
		return wire.Errorf(wire.CodeNotFound, "no dialog is open on this page")
	}
	params := map[string]any{"accept": accept}
	if text != "" {
		params["promptText"] = text
	}
	if err := t.Call(ctx, "Page.handleJavaScriptDialog", params, nil); err != nil {
		return err
	}
	// The reply means the dialog is gone: wait for the event that says so.
	deadline := time.After(5 * time.Second)
	for t.Dialog() != nil {
		select {
		case <-t.Wake():
		case <-deadline:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}
