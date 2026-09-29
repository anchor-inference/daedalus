package page

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// doc is one document the page model reads through a world of its own: the tab's, or a frame's of
// another site. The tab's world reaches every same-origin frame by itself; a frame of another site is
// out of its reach, so it gets a world in its own frame — in its own session when Chromium runs it in
// a process of its own (another site), in the tab's session when it shares the page's process (the
// same site, another origin).
type doc struct {
	session string // "" is the tab's own
	frame   string // the frame the world is in; "" is the tab's main frame
	prefix  string // what the document's refs begin with: "" for the tab's, the frame's ref for a frame
	off     point  // where the document's viewport begins in the tab's
	parent  *doc   // the document holding the frame, nil for the tab's
}

// top is the tab's own document.
var top = &doc{}

// maxFrameDepth bounds how deep frames of other sites inside each other are followed.
const maxFrameDepth = 4

// maxCrossFrames bounds how many frames of other sites one read goes into: an advertising page can
// hold dozens, each a round of calls. maxMaskedFrames is the bound for hiding secrets before a
// screenshot, where every frame matters and a page past it is not captured.
const (
	maxCrossFrames  = 10
	maxMaskedFrames = 64
)

type routed struct {
	Local bool   `json:"local"`
	Frame string `json:"frame"`
}

// docOf finds the document a ref lives in, going into frames of other sites as its prefix says.
// A ref that is the frame's own ref names the frame's whole document: the second result is then "".
func (p *Model) docOf(ctx context.Context, t *browser.Tab, ref string) (*doc, string, error) {
	d := top
	for depth := 0; ; depth++ {
		if d.prefix != "" && ref == d.prefix {
			return d, "", nil
		}
		raw, err := p.callIn(ctx, t, d, "route", ref)
		if err != nil {
			return nil, "", err
		}
		if err := checkScript(raw); err != nil {
			return nil, "", err
		}
		var r routed
		if err := json.Unmarshal(raw, &r); err != nil {
			return nil, "", err
		}
		if r.Local || r.Frame == "" || depth >= maxFrameDepth {
			if !r.Local {
				return nil, "", errStale(ref)
			}
			return d, ref, nil
		}
		child, err := p.child(ctx, t, d, r.Frame, true)
		if err != nil {
			return nil, "", err
		}
		d = child
	}
}

// child is the document of the frame fref of d, which is of another site. With scroll set the frame
// is brought into view first, as an element is before an action.
func (p *Model) child(ctx context.Context, t *browser.Tab, d *doc, fref string, scroll bool) (*doc, error) {
	obj, err := p.object(ctx, t, d, "frameElement", fref)
	if err != nil {
		return nil, err
	}
	var node struct {
		Node struct {
			FrameID string `json:"frameId"`
		} `json:"node"`
	}
	err = t.CallIn(ctx, d.session, "DOM.describeNode", map[string]any{"objectId": obj}, &node)
	_ = t.CallIn(ctx, d.session, "Runtime.releaseObject", map[string]any{"objectId": obj}, nil)
	if err != nil {
		return nil, err
	}
	if node.Node.FrameID == "" {
		return nil, errFrameUnread(fref, "it has no document yet")
	}
	fn := "frameOrigin"
	if scroll {
		fn = "prepareFrame"
	}
	raw, err := p.callIn(ctx, t, d, fn, fref)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var o point
	_ = json.Unmarshal(raw, &o)
	c := &doc{session: d.session, frame: node.Node.FrameID, prefix: fref, parent: d,
		off: point{X: d.off.X + o.X, Y: d.off.Y + o.Y}}
	// A frame of another site runs in a process of its own, reached through its own session.
	if s := t.FrameSession(node.Node.FrameID); s != "" {
		c.session = s
	}
	return c, nil
}

// reoffset measures again where d's viewport sits, after something scrolled.
func (p *Model) reoffset(ctx context.Context, t *browser.Tab, d *doc) error {
	if d.parent == nil {
		return nil
	}
	if err := p.reoffset(ctx, t, d.parent); err != nil {
		return err
	}
	raw, err := p.callIn(ctx, t, d.parent, "frameOrigin", d.prefix)
	if err != nil {
		return err
	}
	if err := checkScript(raw); err != nil {
		return err
	}
	var o point
	_ = json.Unmarshal(raw, &o)
	d.off = point{X: d.parent.off.X + o.X, Y: d.parent.off.Y + o.Y}
	return nil
}

// crossDocs are the documents of the frames of other sites under d, and theirs, depth first, at most
// limit of them; missed counts the frames that could not be reached, for a caller to whom
// every one matters (a screenshot's masks).
func (p *Model) crossDocs(ctx context.Context, t *browser.Tab, d *doc, limit int) (out []*doc, missed int) {
	var visit func(d *doc, depth int)
	visit = func(d *doc, depth int) {
		if depth >= maxFrameDepth {
			return
		}
		raw, err := p.callIn(ctx, t, d, "crossFrames")
		if err != nil {
			missed++
			return
		}
		var frames []string
		_ = json.Unmarshal(raw, &frames)
		for _, f := range frames {
			if len(out) >= limit {
				missed++
				continue
			}
			c, err := p.child(ctx, t, d, f, false)
			if err != nil {
				missed++
				continue
			}
			out = append(out, c)
			visit(c, depth+1)
		}
	}
	visit(d, 0)
	return out, missed
}

func errFrameUnread(fref, why string) *wire.Error {
	e := wire.Errorf(browser.CodeStaleRef, "frame %s cannot be read: %s", fref, why)
	e.Data = map[string]any{"ref": fref}
	return e
}

// isFrameRef says s is a frame's ref: f<k>, or one inside another frame's (f2f1).
func isFrameRef(s string) bool {
	if s == "" {
		return false
	}
	for s != "" {
		if s[0] != 'f' {
			return false
		}
		i := 1
		for i < len(s) && s[i] >= '0' && s[i] <= '9' {
			i++
		}
		if i == 1 {
			return false
		}
		s = s[i:]
	}
	return true
}

// indent shifts outline lines right by depth levels, for a frame's lines spliced under its own.
func indent(lines []string, depth int) []string {
	pad := strings.Repeat("  ", depth)
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = pad + l
	}
	return out
}
