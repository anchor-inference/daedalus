// Package view serves live views of tabs: the screencast of each watched tab, the clients watching
// it with their own pace and flow control, and a person's input while they drive.
package view

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// PingEvery is how often a client is pinged, as a terminal attachment is.
const PingEvery = 20 * time.Second

// Hub is every live view of the daemon.
type Hub struct {
	m   *browser.Manager
	ev  *events.Log
	log *slog.Logger
	fps int

	// HumanInput is told of every input a person sends to a tab, so the page model can remember the
	// fields they typed into as secret. It may be nil.
	HumanInput func(t *browser.Tab, kind string)
	// Observe is told of each input of the person who drives, before it is dispatched: the
	// recording of their steps reads what the input lands on while the page is still as they saw it.
	// It may be nil.
	Observe func(t *browser.Tab, in wire.Input)
	// Selection reads the text selected on a tab for a person's copy (page.Model.Selection). It may
	// be nil, and a copy is then answered with an error.
	Selection func(ctx context.Context, t *browser.Tab, max int) (text string, truncated, withheld bool, err error)

	mu      sync.Mutex
	casts   map[*browser.Tab]*cast
	clients map[string]*Client
}

// New returns a hub over the manager, following its events for the clients.
func New(m *browser.Manager, ev *events.Log, log *slog.Logger) *Hub {
	return &Hub{m: m, ev: ev, log: log, fps: m.Limits().FPSCap, casts: map[*browser.Tab]*cast{}, clients: map[string]*Client{}}
}

// ClientInfo is view.attach's client.
type ClientInfo struct {
	Kind     string `json:"kind,omitempty"`
	Label    string `json:"label,omitempty"`
	Via      string `json:"via,omitempty"`
	ReadOnly bool   `json:"read_only,omitempty"`
}

// Attach opens a view channel on conn for a client of group g.
func (h *Hub) Attach(conn *server.Conn, g *browser.Group, info ClientInfo) (uint32, string, error) {
	switch info.Kind {
	case "", "human":
		info.Kind = "human"
	case "viewer":
		info.ReadOnly = true
	default:
		return 0, "", protowire.Errorf(protowire.CodeInvalidParams, "client.kind must be human or viewer")
	}
	if len(info.Label) > 256 || len(info.Via) > 64 {
		return 0, "", protowire.Errorf(protowire.CodeInvalidParams, "client.label is at most 256 bytes and client.via 64")
	}
	raw := make([]byte, 4)
	_, _ = rand.Read(raw)
	cl := &Client{id: "c" + hex.EncodeToString(raw), hub: h, conn: conn, group: g, kind: info.Kind, label: info.Label,
		via: info.Via, readOnly: info.ReadOnly, input: make(chan wire.Input, 64), quit: make(chan struct{})}
	cl.channel = conn.OpenChannel(cl)
	h.mu.Lock()
	h.clients[cl.id] = cl
	h.mu.Unlock()
	go cl.run()
	return cl.channel, cl.id, nil
}

// Detach ends the client on channel of conn.
func (h *Hub) Detach(conn *server.Conn, channel uint32) error {
	h.mu.Lock()
	var found *Client
	for _, cl := range h.clients {
		if cl.conn == conn && cl.channel == channel {
			found = cl
		}
	}
	h.mu.Unlock()
	if found == nil {
		return protowire.Errorf(protowire.CodeNotFound, "no view on channel %d", channel)
	}
	found.end(true)
	return nil
}

// Client returns a client by id, for control.set.
func (h *Hub) Client(id string) *Client {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.clients[id]
}

// Count is the number of clients, for daemon.info.
func (h *Hub) Count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.clients)
}

// Busy reports whether anyone watches a tab of browser b: a watched browser is never idle.
func (h *Hub) Busy(b *browser.Browser) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, cl := range h.clients {
		if cl.group.Browser == b {
			return true
		}
	}
	return false
}

func (h *Hub) castFor(t *browser.Tab) *cast {
	h.mu.Lock()
	defer h.mu.Unlock()
	c := h.casts[t]
	if c == nil {
		c = newCast(h, t)
		h.casts[t] = c
	}
	return c
}

func (h *Hub) leaveCast(t *browser.Tab, cl *Client) {
	h.mu.Lock()
	c := h.casts[t]
	h.mu.Unlock()
	if c == nil {
		return
	}
	if c.remove(cl) {
		h.mu.Lock()
		if h.casts[t] == c && c.count() == 0 {
			delete(h.casts, t)
		}
		h.mu.Unlock()
	}
}

func (h *Hub) forget(cl *Client) {
	h.mu.Lock()
	delete(h.clients, cl.id)
	h.mu.Unlock()
}

// clientsOf lists the clients of a group, or of one tab when t is set.
func (h *Hub) clientsOf(g *browser.Group, t *browser.Tab) []*Client {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []*Client
	for _, cl := range h.clients {
		if cl.group != g {
			continue
		}
		if t != nil && cl.currentTab() != t {
			continue
		}
		out = append(out, cl)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].id < out[j].id })
	return out
}

// sendViewers tells every client of a tab who else watches it.
func (h *Hub) sendViewers(g *browser.Group, t *browser.Tab) {
	if t == nil {
		return
	}
	cls := h.clientsOf(g, t)
	for _, cl := range cls {
		others := []map[string]any{}
		for _, o := range cls {
			if o != cl {
				others = append(others, map[string]any{"id": o.id, "kind": o.kind, "label": o.label})
			}
		}
		cl.event(map[string]any{"type": "viewers", "count": len(cls), "others": others})
	}
}

// TabEvent takes the screencast frames of watched tabs.
func (h *Hub) TabEvent(t *browser.Tab, e cdp.Event) {
	if e.Method != "Page.screencastFrame" {
		return
	}
	h.mu.Lock()
	c := h.casts[t]
	h.mu.Unlock()
	if c == nil {
		// A frame of a cast just stopped: acknowledged so Chromium does not wait, and dropped.
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			_ = t.Call(ctx, "Page.stopScreencast", nil, nil)
		}()
		return
	}
	c.frame(e.Params)
}

func (h *Hub) BrowserEvent(*browser.Browser, cdp.Event) {}

// TabGone moves a closed tab's clients to their group's active tab.
func (h *Hub) TabGone(t *browser.Tab) {
	h.mu.Lock()
	delete(h.casts, t)
	h.mu.Unlock()
	for _, cl := range h.clientsOf(t.Group, t) {
		cl.event(map[string]any{"type": "error", "code": "tab_closed", "message": "the tab was closed"})
		cl.switchTab(nil)
	}
}

// Run follows the daemon's events and tells each client what concerns its group, and pings them,
// until stop is closed.
func (h *Hub) Run(stop <-chan struct{}) {
	cursor := h.ev.Last()
	ping := time.NewTicker(PingEvery)
	defer ping.Stop()
	for {
		wake := h.ev.Wait()
		evs, _ := h.ev.After(cursor, 256)
		for _, e := range evs {
			cursor = e.Seq
			h.route(e)
		}
		if len(evs) > 0 {
			continue
		}
		select {
		case <-wake:
		case <-ping.C:
			at := time.Now().UnixMilli()
			h.mu.Lock()
			cls := make([]*Client, 0, len(h.clients))
			for _, cl := range h.clients {
				cls = append(cls, cl)
			}
			h.mu.Unlock()
			for _, cl := range cls {
				cl.event(map[string]any{"type": "ping", "at": at})
			}
		case <-stop:
			return
		}
	}
}

// route turns one daemon event into the view events of the clients it concerns.
func (h *Hub) route(e events.Event) {
	data, _ := e.Data.(map[string]any)
	if data == nil {
		return
	}
	if e.Type == "browser.exited" {
		groups, _ := data["groups"].([]string)
		for _, id := range groups {
			h.endGroup(id)
		}
		return
	}
	gid, _ := data["group_id"].(string)
	if gid == "" {
		return
	}
	h.mu.Lock()
	var cls []*Client
	for _, cl := range h.clients {
		if cl.group.ID == gid {
			cls = append(cls, cl)
		}
	}
	h.mu.Unlock()
	if len(cls) == 0 {
		return
	}
	switch e.Type {
	case "group.closed":
		h.endGroup(gid)
	case "tab.created", "tab.closed":
		for _, cl := range cls {
			cl.sendTabs()
		}
	case "tab.updated":
		for _, cl := range cls {
			if t := cl.currentTab(); t != nil && t.ID == data["tab_id"] {
				cl.event(map[string]any{"type": "tab", "id": t.ID, "url": data["url"], "title": data["title"],
					"favicon_url": data["favicon_url"], "loading": data["loading"]})
			}
			cl.sendTabs()
		}
	case "control":
		for _, cl := range cls {
			cl.sendControl()
		}
	case "action", "action_done", "needs_you":
		for _, cl := range cls {
			cl.event(withType(e.Type, data))
		}
	case "workflow.started", "workflow.step", "workflow.stopped":
		// The panel of the person recording shows each step as it is taken. A stopped recording is
		// said without its steps, which the host keeps; a view needs only that it ended.
		v := map[string]any{"type": "workflow", "state": strings.TrimPrefix(e.Type, "workflow."), "id": data["id"]}
		if wf, ok := data["workflow"].(interface{ Summary() map[string]any }); ok {
			for k, x := range wf.Summary() {
				v[k] = x
			}
		}
		if st, ok := data["step"]; ok {
			v["step"], v["steps"], v["recording"] = st, data["steps"], true
			if n, ok := data["replaces"]; ok {
				v["replaces"] = n
			}
		}
		for _, cl := range cls {
			cl.event(v)
		}
	case "dialog.opened", "dialog.closed":
		for _, cl := range cls {
			v := withType("dialog", data)
			v["open"] = e.Type == "dialog.opened"
			cl.event(v)
		}
	case "download.started", "download.done":
		for _, cl := range cls {
			cl.event(withType("download", data))
		}
	}
}

func withType(typ string, data map[string]any) map[string]any {
	out := make(map[string]any, len(data)+1)
	out["type"] = typ
	for k, v := range data {
		out[k] = v
	}
	return out
}

func (h *Hub) endGroup(id string) {
	h.mu.Lock()
	var cls []*Client
	for _, cl := range h.clients {
		if cl.group.ID == id {
			cls = append(cls, cl)
		}
	}
	h.mu.Unlock()
	for _, cl := range cls {
		cl.end(true)
	}
}
