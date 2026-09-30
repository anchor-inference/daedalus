package view

import (
	"context"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

// Client is one live view: a channel, the tab it shows, its tier, and its flow control.
type Client struct {
	id   string
	hub  *Hub
	conn interface {
		SendChannel(uint32, []byte) error
		CloseChannel(uint32)
	}
	channel  uint32
	group    *browser.Group
	kind     string
	label    string
	via      string
	readOnly bool

	input chan wire.Input
	quit  chan struct{}

	mu       sync.Mutex
	ended    bool
	tab      *browser.Tab
	tier     string // "" until ATTACH
	maxW     int
	maxH     int
	quality  int
	frameNo  uint32
	inFlight uint32 // the frame sent and not yet acknowledged, 0 for none
	sentAt   time.Time
	lastPic  uint64
	pending  *picture
	timer    *time.Timer
	slow     int
	fast     int
	degraded bool
	hidden   bool
	warnedAt time.Time
}

// ID is the client's id, which control.set names.
func (cl *Client) ID() string { return cl.id }

// Group is the group the client watches.
func (cl *Client) Group() *browser.Group { return cl.group }

// ReadOnly reports whether the client only watches.
func (cl *Client) ReadOnly() bool { return cl.readOnly }

func (cl *Client) currentTab() *browser.Tab {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	return cl.tab
}

// Frame is the server package's call for each frame the client sends.
func (cl *Client) Frame(payload []byte) {
	f, err := wire.Decode(payload)
	if err != nil {
		cl.event(map[string]any{"type": "error", "code": "bad_frame", "message": err.Error()})
		return
	}
	switch f.Type {
	case wire.TypeAttach:
		a, err := wire.DecodeAttach(f)
		if err != nil {
			cl.event(map[string]any{"type": "error", "code": "bad_frame", "message": err.Error()})
			return
		}
		cl.attach(wire.View{Tier: a.Tier, Tab: a.Tab, MaxW: a.MaxW, MaxH: a.MaxH, DPR: a.DPR, Quality: a.Quality}, true)
	case wire.TypeView:
		v, err := wire.DecodeView(f)
		if err != nil {
			cl.event(map[string]any{"type": "error", "code": "bad_frame", "message": err.Error()})
			return
		}
		cl.attach(v, false)
	case wire.TypeAck:
		cl.ack(f.FrameNo)
	case wire.TypeInput:
		in, err := wire.DecodeInput(f)
		if err != nil {
			cl.event(map[string]any{"type": "error", "code": "bad_frame", "message": err.Error()})
			return
		}
		cl.takeInput(in)
	default:
		cl.event(map[string]any{"type": "error", "code": "bad_frame", "message": "not a frame a client sends"})
	}
}

// Closed is the server package's call when the other side closed the channel or the connection went.
func (cl *Client) Closed() { cl.end(false) }

// end forgets the client; closeChannel closes its channel from this side.
func (cl *Client) end(closeChannel bool) {
	cl.mu.Lock()
	if cl.ended {
		cl.mu.Unlock()
		return
	}
	cl.ended = true
	t := cl.tab
	if cl.timer != nil {
		cl.timer.Stop()
	}
	cl.mu.Unlock()
	close(cl.quit)
	cl.hub.forget(cl)
	if t != nil {
		cl.hub.leaveCast(t, cl)
		cl.hub.sendViewers(cl.group, t)
	}
	if closeChannel {
		cl.conn.CloseChannel(cl.channel)
	}
}

func (cl *Client) send(b []byte) {
	if err := cl.conn.SendChannel(cl.channel, b); err != nil {
		cl.end(false)
	}
}

func (cl *Client) event(v map[string]any) {
	cl.mu.Lock()
	attached := cl.tier != "" && !cl.ended
	cl.mu.Unlock()
	if !attached {
		return
	}
	b, err := wire.EncodeEvent(v)
	if err != nil {
		return
	}
	cl.send(b)
}

// attach applies an ATTACH (first) or a VIEW: tier, tab, size, quality. An ATTACH always sends the
// client's hello, the tabs and the viewers; a frame follows as soon as there is one.
func (cl *Client) attach(v wire.View, full bool) {
	tier := v.Tier
	if full || tier != "" {
		if tier != "live" && tier != "thumb" {
			cl.forceEvent(map[string]any{"type": "error", "code": "bad_frame", "message": "tier must be live or thumb"})
			return
		}
	}
	for _, n := range []int{v.MaxW, v.MaxH} {
		if (full || n != 0) && (n < 64 || n > 4096) {
			cl.forceEvent(map[string]any{"type": "error", "code": "bad_frame", "message": "max_w and max_h must be 64-4096"})
			return
		}
	}
	if v.Quality != 0 && (v.Quality < 30 || v.Quality > 90) {
		cl.forceEvent(map[string]any{"type": "error", "code": "bad_frame", "message": "quality must be 30-90"})
		return
	}
	var tab *browser.Tab
	if v.Tab != "" {
		t, err := cl.hub.m.Tab(v.Tab)
		if err != nil || t.Group != cl.group {
			cl.forceEvent(map[string]any{"type": "error", "code": "tab_closed", "message": "no such tab in this group"})
			return
		}
		tab = t
	}
	cl.mu.Lock()
	if cl.ended {
		cl.mu.Unlock()
		return
	}
	if !full && cl.tier == "" {
		cl.mu.Unlock()
		return // a VIEW before any ATTACH is ignored: nothing is sent before an ATTACH
	}
	if tier != "" {
		cl.tier = tier
	}
	if v.MaxW != 0 {
		cl.maxW, cl.maxH = v.MaxW, v.MaxH
	}
	shown := false
	if v.Hidden != nil {
		shown = cl.hidden && !*v.Hidden
		cl.hidden = *v.Hidden
	}
	if v.Quality != 0 {
		cl.quality = v.Quality
	} else if full {
		cl.quality = LiveQuality
		if cl.tier == "thumb" {
			cl.quality = ThumbQuality
		}
	}
	old := cl.tab
	if tab == nil && (full || old == nil) {
		tab = cl.group.ActiveTab()
	}
	if tab == nil {
		tab = old
	}
	cl.mu.Unlock()
	if tab != nil && tab != old {
		if n := cl.hub.castFor(tab).count(); n >= cl.hub.m.Limits().MaxViewersPerTab {
			cl.forceEvent(map[string]any{"type": "error", "code": "limit", "message": "this tab has as many viewers as it may"})
			return
		}
	}
	cl.mu.Lock()
	cl.tab = tab
	if tab != old {
		// A new tab's pictures start over: nothing of the old one waits or counts.
		cl.pending, cl.lastPic = nil, 0
	}
	cl.mu.Unlock()
	if shown {
		cl.trySend()
	}
	if full {
		cl.hello()
		cl.sendTabs()
	}
	if old != nil && old != tab {
		cl.hub.leaveCast(old, cl)
		cl.hub.sendViewers(cl.group, old)
	}
	if tab != nil {
		c := cl.hub.castFor(tab)
		if tab != old {
			c.add(cl)
			cl.hub.sendViewers(cl.group, tab)
		} else {
			c.reconfigure()
			if full {
				cl.hub.sendViewers(cl.group, tab)
			}
		}
	}
}

// forceEvent sends an event even before the client attached (a refused ATTACH says why).
func (cl *Client) forceEvent(v map[string]any) {
	b, err := wire.EncodeEvent(v)
	if err == nil {
		cl.send(b)
	}
}

func (cl *Client) hello() {
	cl.mu.Lock()
	tier, tab := cl.tier, cl.tab
	cl.mu.Unlock()
	var tabID any
	if tab != nil {
		tabID = tab.ID
	}
	g := cl.group
	cl.event(map[string]any{"type": "hello", "client_id": cl.id, "read_only": cl.readOnly, "tier": tier,
		"group": map[string]any{"id": g.ID, "profile": g.Profile, "viewport": g.ViewportNow()}, "tab_id": tabID,
		"control": g.Control().View(cl.id), "fps_cap": cl.hub.fps})
}

func (cl *Client) sendTabs() {
	tabs := cl.group.Tabs()
	list := make([]map[string]any, 0, len(tabs))
	for _, t := range tabs {
		v := t.View()
		list = append(list, map[string]any{"id": v["id"], "url": v["url"], "title": v["title"],
			"favicon_url": v["favicon_url"], "loading": v["loading"], "active": v["active"]})
	}
	var active any
	if a := cl.group.ActiveTab(); a != nil {
		active = a.ID
	}
	cl.event(map[string]any{"type": "tabs", "tabs": list, "active": active})
}

func (cl *Client) sendControl() {
	v := cl.group.Control().View(cl.id)
	v["type"] = "control"
	cl.event(v)
}

// switchTab moves the client to t, or to the group's active tab when t is nil.
func (cl *Client) switchTab(t *browser.Tab) {
	if t == nil {
		t = cl.group.ActiveTab()
	}
	cl.mu.Lock()
	old := cl.tab
	cl.tab = t
	cl.pending, cl.lastPic = nil, 0
	cl.inFlight = 0
	cl.mu.Unlock()
	if old != nil && old != t {
		cl.hub.leaveCast(old, cl)
	}
	if t != nil && t != old {
		cl.hub.castFor(t).add(cl)
		cl.hub.sendViewers(cl.group, t)
	}
	cl.sendTabs()
}

// offer puts the newest picture in the client's mailbox of one and sends it when the client may
// receive: its previous frame acknowledged, and its pace allowing.
func (cl *Client) offer(p *picture) {
	cl.mu.Lock()
	if cl.ended || cl.tier == "" {
		cl.mu.Unlock()
		return
	}
	cl.pending = p
	cl.mu.Unlock()
	cl.trySend()
}

func (cl *Client) gap() time.Duration {
	if cl.tier == "thumb" {
		return ThumbEvery
	}
	return time.Second / time.Duration(cl.hub.fps)
}

func (cl *Client) trySend() {
	cl.mu.Lock()
	if cl.ended || cl.pending == nil || cl.inFlight != 0 || cl.tab == nil || cl.hidden {
		cl.mu.Unlock()
		return
	}
	p := cl.pending
	if p.n == cl.lastPic {
		cl.pending = nil
		cl.mu.Unlock()
		return
	}
	if wait := cl.gap() - time.Since(cl.sentAt); wait > 0 {
		if cl.timer == nil {
			cl.timer = time.AfterFunc(wait, func() {
				cl.mu.Lock()
				cl.timer = nil
				cl.mu.Unlock()
				cl.trySend()
			})
		}
		cl.mu.Unlock()
		return
	}
	cl.pending = nil
	cl.lastPic = p.n
	cl.frameNo++
	if cl.frameNo == 0 {
		cl.frameNo = 1
	}
	n := cl.frameNo
	cl.inFlight = n
	cl.sentAt = time.Now()
	tier, degraded, tabID := cl.tier, cl.degraded, cl.tab.ID
	cl.mu.Unlock()
	img, w, h := p.jpeg, p.meta.W, p.meta.H
	switch {
	case tier == "thumb":
		img, w, h = p.thumbnail()
	case degraded:
		img, w, h = p.degraded()
	}
	meta := p.meta
	meta.Tab, meta.Tier, meta.W, meta.H = tabID, tier, w, h
	b, err := wire.EncodeFrame(n, meta, img)
	if err != nil {
		return
	}
	cl.send(b)
}

// ack takes a client's acknowledgement: the frame in flight is drawn, the next may go, and the time
// it took says whether the link is slow.
func (cl *Client) ack(n uint32) {
	cl.mu.Lock()
	// One frame is in flight at a time, so the only acknowledgement that means anything is its own;
	// a stale or invented number changes nothing.
	if cl.inFlight == 0 || n != cl.inFlight {
		cl.mu.Unlock()
		return
	}
	rtt := time.Since(cl.sentAt)
	cl.inFlight = 0
	if cl.tier == "live" {
		switch {
		case rtt > 400*time.Millisecond:
			cl.slow++
			cl.fast = 0
			if cl.slow >= 5 {
				cl.degraded = true
			}
		case rtt < 120*time.Millisecond:
			cl.fast++
			cl.slow = 0
			if cl.fast >= 20 {
				cl.degraded = false
			}
		default:
			cl.slow, cl.fast = 0, 0
		}
	}
	cl.mu.Unlock()
	cl.trySend()
}

// takeInput accepts input from the holder of human control only, and runs it in order.
func (cl *Client) takeInput(in wire.Input) {
	if cl.readOnly {
		return
	}
	if !cl.group.RenewHuman(cl.id, config.HumanControlTTL) {
		cl.mu.Lock()
		warn := time.Since(cl.warnedAt) > time.Second
		if warn {
			cl.warnedAt = time.Now()
		}
		cl.mu.Unlock()
		if warn {
			cl.event(map[string]any{"type": "error", "code": "not_holder",
				"message": "take control of the browser before sending input"})
		}
		return
	}
	select {
	case cl.input <- in:
	default:
		// More than the queue holds is a stuck page; the newest input is dropped, never the channel.
	}
}

// run dispatches the client's input, one at a time, in the order it came.
func (cl *Client) run() {
	for {
		select {
		case in := <-cl.input:
			t := cl.currentTab()
			if t == nil {
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			if in.T == "copy" {
				cl.copySelection(ctx, t, in.ID)
				cl.hub.log.Debug("input", "t", in.T)
				cancel()
				continue
			}
			if cl.hub.Observe != nil {
				cl.hub.Observe(t, in)
			}
			err := cl.hub.dispatch(ctx, t, in)
			cl.hub.log.Debug("input", "t", in.T, "type", in.Type, "error", err)
			if err != nil {
				cl.event(map[string]any{"type": "error", "code": "input", "message": err.Error()})
			}
			cancel()
		case <-cl.quit:
			return
		}
	}
}
