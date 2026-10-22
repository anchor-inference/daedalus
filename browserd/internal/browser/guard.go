package browser

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The navigation guard: a page's own top-level navigations — a link it follows, a redirect, a form it
// posts, a script that sets location — are judged by the wall's navigation rules like the agent's,
// when the operator has an allowlist. The proxy's address rules already refuse the LAN and this
// machine's ports on every request; what they cannot see is the allowlist, which names hosts the
// agent may be taken to, and a page leading the agent off it is exactly what an injected
// instruction does. Without an allowlist no request is intercepted: every navigation costs a round
// trip on the pipe while the guard is on.

// fetchKey keys, among a tab's values, the fetchState its Fetch domain was last enabled with.
type fetchKey struct{}

// fetchState is what a tab's Fetch domain pauses: documents at the request for the guard, and, in
// the operator's Chrome, documents at the response so a PDF never reaches Chrome's viewer (pdf.go).
// Fetch.enable replaces the patterns it was given before, so the two are always enabled together,
// and the domain is disabled only when neither is wanted.
type fetchState struct {
	guard bool
	pdf   bool
}

func (s fetchState) on() bool { return s.guard || s.pdf }

// params is the Fetch.enable of the state. The guard's pattern pauses main-frame and frame
// documents at the request; frames are let through at once, only a tab's own document is judged.
func (s fetchState) params() map[string]any {
	patterns := []map[string]any{}
	if s.guard {
		patterns = append(patterns, map[string]any{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"})
	}
	if s.pdf {
		patterns = append(patterns, map[string]any{"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"})
	}
	return map[string]any{"patterns": patterns}
}

// guarding reports whether the wall wants page navigations judged.
func (m *Manager) guarding() bool {
	return m.deps.Wall != nil && m.deps.Wall.Allowlist()
}

// fetchWanted is what the Fetch domain of a page of b should pause now. The PDF pattern is the
// operator's Chrome's only: a browser of the daemon's own has no PDF viewer to lose a tab to, and
// every paused response is a round trip on the pipe.
func (m *Manager) fetchWanted(b *Browser) fetchState {
	return fetchState{guard: m.guarding(), pdf: b.attached}
}

// SyncGuard turns the guard on or off in every open tab, after the wall's rules changed.
func (m *Manager) SyncGuard(ctx context.Context) {
	m.mu.Lock()
	tabs := make([]*Tab, 0, len(m.tabs))
	for _, t := range m.tabs {
		tabs = append(tabs, t)
	}
	m.mu.Unlock()
	for _, t := range tabs {
		want := m.fetchWanted(t.Group.Browser)
		was, _ := t.Value(fetchKey{}).(fetchState)
		if was == want {
			continue
		}
		method, params := "Fetch.disable", any(nil)
		if want.on() {
			method, params = "Fetch.enable", want.params()
		}
		if err := t.Call(ctx, method, params, nil); err == nil {
			t.SetValue(fetchKey{}, want)
		}
	}
}

// guardPaused answers one paused request: a frame's document goes on; the tab's own is judged, and
// refused ones fail as blocked by the client and are published as navigation.blocked, which the host
// tells the agent about. While a person drives, their clicks are theirs: the navigation goes on, as
// an address they type does through the operator's own path.
func (m *Manager) guardPaused(t *Tab, raw json.RawMessage) {
	var p struct {
		RequestID string `json:"requestId"`
		FrameID   string `json:"frameId"`
		Request   struct {
			URL string `json:"url"`
		} `json:"request"`
	}
	if json.Unmarshal(raw, &p) != nil || p.RequestID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	allow := p.FrameID != t.TargetID || m.deps.Wall == nil || t.Group.Control().Owner == OwnerHuman
	var verdict map[string]any
	if !allow {
		err := m.deps.Wall.Navigation(ctx, t.Group.Browser.ID, p.Request.URL)
		allow = err == nil
		var we *wire.Error
		if errors.As(err, &we) {
			verdict, _ = we.Data.(map[string]any)
		}
	}
	if allow {
		_ = t.Call(ctx, "Fetch.continueRequest", map[string]any{"requestId": p.RequestID}, nil)
		return
	}
	_ = t.Call(ctx, "Fetch.failRequest", map[string]any{"requestId": p.RequestID, "errorReason": "BlockedByClient"}, nil)
	ev := map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "url": p.Request.URL, "from": t.URL(), "by": "page"}
	for _, k := range []string{"host", "port", "decision", "reason"} {
		if v, ok := verdict[k]; ok {
			ev[k] = v
		}
	}
	m.log.Info("a page's navigation was stopped", "group", t.Group.ID, "tab", t.ID, "reason", ev["reason"])
	m.publish("navigation.blocked", ev)
}
