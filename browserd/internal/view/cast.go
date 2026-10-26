package view

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"image/jpeg"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

// The sizes of the two tiers. A live frame is at most LiveMax; a thumbnail is ThumbMax at ThumbQuality,
// at most once per ThumbEvery. LiveMax was 1600×1000 once, which on a large window with the page fitted
// to it scaled every frame down and the viewer up again: text went soft for no reason the operator
// could change. A frame is never larger than the page's own pixels anyway; how many of them a client
// takes is its own choice (its box, its quality preset).
const (
	LiveMaxW       = 4096
	LiveMaxH       = 4096
	MaxQuality     = 95 // what a client may ask: past it JPEG grows much faster than it sharpens
	ThumbMaxW      = 320
	ThumbMaxH      = 200
	ThumbQuality   = 45
	LiveQuality    = 60
	ThumbEvery     = time.Second
	DegradeQuality = 40
)

// picture is one frame Chromium sent, with its metadata, and the smaller encodings made of it on
// demand (each at most once, whoever asks first).
type picture struct {
	n    uint64
	meta wire.Meta // without tab and tier, which are the receiving client's
	jpeg []byte

	thumbOnce sync.Once
	thumb     []byte
	thumbW    int
	thumbH    int

	halfOnce sync.Once
	half     []byte
	halfW    int
	halfH    int
}

func (p *picture) thumbnail() ([]byte, int, int) {
	p.thumbOnce.Do(func() {
		if p.meta.W <= ThumbMaxW && p.meta.H <= ThumbMaxH {
			p.thumb, p.thumbW, p.thumbH = p.jpeg, p.meta.W, p.meta.H
			return
		}
		b, w, h, err := Scale(p.jpeg, ThumbMaxW, ThumbMaxH, ThumbQuality)
		if err != nil {
			p.thumb, p.thumbW, p.thumbH = p.jpeg, p.meta.W, p.meta.H
			return
		}
		p.thumb, p.thumbW, p.thumbH = b, w, h
	})
	return p.thumb, p.thumbW, p.thumbH
}

// degraded is the picture at half size and quality 40, for a client whose link is slow.
func (p *picture) degraded() ([]byte, int, int) {
	p.halfOnce.Do(func() {
		b, w, h, err := Scale(p.jpeg, max(64, p.meta.W/2), max(64, p.meta.H/2), DegradeQuality)
		if err != nil {
			p.half, p.halfW, p.halfH = p.jpeg, p.meta.W, p.meta.H
			return
		}
		p.half, p.halfW, p.halfH = b, w, h
	})
	return p.half, p.halfW, p.halfH
}

// cast is the screencast of one tab, shared by every client watching it.
type cast struct {
	hub *Hub
	tab *browser.Tab

	mu      sync.Mutex
	clients map[*Client]bool
	newest  *picture
	seq     uint64
	// What runs now; zero while stopped.
	running                bool
	runW, runH, runQuality int
	liveNow                bool
	lastAck                time.Time
	ackTimer               *time.Timer

	// fps is the pace Chromium is acknowledged at while a live client watches: the fastest any of them
	// asked for, at most the daemon's cap.
	fps int

	// Starting and stopping are CDP calls made in order, one at a time, outside the lock.
	ops chan struct{}
}

func newCast(h *Hub, t *browser.Tab) *cast {
	c := &cast{hub: h, tab: t, clients: map[*Client]bool{}, ops: make(chan struct{}, 1)}
	c.ops <- struct{}{}
	return c
}

// wanted is what the screencast should be for the clients now: the largest live client's box at the
// best quality asked for, else a thumbnail, else nothing.
func (c *cast) wantedLocked() (w, h, q int, live, some bool) {
	c.fps = 0
	for cl := range c.clients {
		cl.mu.Lock()
		tier, mw, mh, cq, fps := cl.tier, cl.maxW, cl.maxH, cl.quality, cl.fpsLocked()
		cl.mu.Unlock()
		if tier == "" {
			continue
		}
		some = true
		if tier != "live" {
			continue
		}
		live = true
		w, h = max(w, min(mw, LiveMaxW)), max(h, min(mh, LiveMaxH))
		q = max(q, cq)
		c.fps = max(c.fps, fps)
	}
	if some && !live {
		return ThumbMaxW, ThumbMaxH, ThumbQuality, false, true
	}
	return w, h, q, live, some
}

// reconfigure starts, restarts or stops the screencast to match the clients.
func (c *cast) reconfigure(force bool) {
	go func() {
		<-c.ops
		defer func() { c.ops <- struct{}{} }()
		c.mu.Lock()
		w, h, q, live, some := c.wantedLocked()
		same := c.running && some && c.runW == w && c.runH == h && c.runQuality == q
		wasRunning := c.running
		c.liveNow = live
		c.mu.Unlock()
		if (same && !force) || (!wasRunning && !some) {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if wasRunning {
			_ = c.tab.Call(ctx, "Page.stopScreencast", nil, nil)
			c.mu.Lock()
			c.running = false
			c.mu.Unlock()
		}
		if !some || c.tab.Closed() {
			return
		}
		err := c.tab.Call(ctx, "Page.startScreencast", map[string]any{"format": "jpeg", "quality": q,
			"maxWidth": w, "maxHeight": h, "everyNthFrame": 1}, nil)
		if err != nil {
			c.hub.log.Warn("screencast did not start", "tab", c.tab.ID, "error", err.Error())
			return
		}
		c.mu.Lock()
		c.running, c.runW, c.runH, c.runQuality = true, w, h, q
		c.mu.Unlock()
	}()
}

// frame takes one Page.screencastFrame: keeps it as the newest, acknowledges it to Chromium at the
// paced time, and offers it to every client.
func (c *cast) frame(params json.RawMessage) {
	var f struct {
		Data      string `json:"data"`
		SessionID int    `json:"sessionId"`
		Metadata  struct {
			OffsetTop       float64 `json:"offsetTop"`
			PageScaleFactor float64 `json:"pageScaleFactor"`
			DeviceWidth     float64 `json:"deviceWidth"`
			DeviceHeight    float64 `json:"deviceHeight"`
			ScrollOffsetX   float64 `json:"scrollOffsetX"`
			ScrollOffsetY   float64 `json:"scrollOffsetY"`
			Timestamp       float64 `json:"timestamp"`
		} `json:"metadata"`
	}
	if json.Unmarshal(params, &f) != nil {
		return
	}
	c.scheduleAck(f.SessionID)
	// Chromium may deliver a frame queued before a window resize (or before the first
	// window sizing completed). Its metadata would map clicks onto the wrong pixels.
	vp := c.tab.Group.ViewportNow()
	if f.Metadata.DeviceWidth != float64(vp.W) || f.Metadata.DeviceHeight != float64(vp.H) {
		return
	}
	data, err := base64.StdEncoding.DecodeString(f.Data)
	if err != nil || len(data) == 0 {
		return
	}
	cfg, err := jpeg.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return
	}
	ts := int64(f.Metadata.Timestamp * 1000)
	if ts == 0 {
		ts = time.Now().UnixMilli()
	}
	c.mu.Lock()
	c.seq++
	p := &picture{n: c.seq, jpeg: data, meta: wire.Meta{W: cfg.Width, H: cfg.Height, VW: f.Metadata.DeviceWidth,
		VH: f.Metadata.DeviceHeight, ScrollX: f.Metadata.ScrollOffsetX, ScrollY: f.Metadata.ScrollOffsetY,
		OffsetTop: f.Metadata.OffsetTop, PageScale: f.Metadata.PageScaleFactor, TS: ts}}
	c.newest = p
	clients := make([]*Client, 0, len(c.clients))
	for cl := range c.clients {
		clients = append(clients, cl)
	}
	c.mu.Unlock()
	for _, cl := range clients {
		cl.offer(p)
	}
}

// scheduleAck acknowledges a frame to Chromium no sooner than the pace allows. Chromium sends the
// next frame only after an acknowledgement, and keeps two or three in flight; delaying the
// acknowledgement is what bounds its rate (measured: an immediate one gives 50–60 frames a second
// and costs well over a CPU). The clients' own pace is kept separately.
func (c *cast) scheduleAck(session int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	// One acknowledgement, one frame: measured through the host with the app as the viewer, twice
	// the frame's interval here gave half the rate (7.5 live frames a second against a cap of 15,
	// and a thumbnail every two seconds), not the two frames in flight this was written for.
	interval := ThumbEvery
	if c.liveNow {
		fps := c.fps
		if fps <= 0 {
			fps = c.hub.fps
		}
		interval = time.Second / time.Duration(fps)
	}
	now := time.Now()
	at := c.lastAck.Add(interval)
	if at.Before(now) {
		at = now
	}
	c.lastAck = at
	ack := func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = c.tab.Call(ctx, "Page.screencastFrameAck", map[string]any{"sessionId": session}, nil)
	}
	if d := at.Sub(now); d > 0 {
		c.ackTimer = time.AfterFunc(d, ack)
	} else {
		go ack()
	}
}

func (c *cast) add(cl *Client) {
	c.mu.Lock()
	c.clients[cl] = true
	newest := c.newest
	c.mu.Unlock()
	if newest != nil {
		cl.offer(newest)
	}
	c.reconfigure(false)
}

// remove takes a client off; it reports whether the cast has no clients left.
func (c *cast) remove(cl *Client) bool {
	c.mu.Lock()
	delete(c.clients, cl)
	empty := len(c.clients) == 0
	c.mu.Unlock()
	c.reconfigure(false)
	return empty
}

func (c *cast) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.clients)
}
