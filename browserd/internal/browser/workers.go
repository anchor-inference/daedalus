package browser

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
)

// The service workers of the agent's extensions are followed for their console: an extension's
// background is where most of its faults are, and nothing else shows them. Every other worker runs
// as it is and is let go (browserEvent), as before. A worker stays followed while it lives; Chromium
// does not stop an idle worker that has a debugger attached, which keeps its state between the
// agent's looks and is the one way this browser differs from a person's for an extension in test.

// WorkerListener is told the events of an extension's worker; a Listener that implements it gets them.
type WorkerListener interface {
	WorkerEvent(b *Browser, extension, session string, e cdp.Event)
}

type extWorker struct {
	browser   *Browser
	target    string
	extension string
}

// extensionOf is the extension id of a chrome-extension:// address, or "".
func extensionOf(url string) string {
	rest, ok := strings.CutPrefix(url, "chrome-extension://")
	if !ok {
		return ""
	}
	id, _, _ := strings.Cut(rest, "/")
	return id
}

// followWorker keeps an extension's worker attached and listens to its console.
func (m *Manager) followWorker(b *Browser, session, target, extension string) {
	m.mu.Lock()
	if m.extWorkers == nil {
		m.extWorkers = map[string]*extWorker{}
	}
	m.extWorkers[session] = &extWorker{browser: b, target: target, extension: extension}
	m.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var replies []<-chan error
	for _, method := range []string{"Runtime.enable", "Log.enable", "Runtime.runIfWaitingForDebugger"} {
		replies = append(replies, b.conn.Send(ctx, session, method, nil))
	}
	for _, r := range replies {
		if err := <-r; err != nil {
			m.log.Debug("extension worker setup", "extension", extension, "error", err.Error())
		}
	}
}

// followWorkers attaches to the workers of an extension that are running and not followed yet: one
// that started before the daemon knew its id (a path whose id was not foreseen) is reached this way.
func (m *Manager) followWorkers(ctx context.Context, b *Browser, extension string) {
	var r struct {
		TargetInfos []targetInfo `json:"targetInfos"`
	}
	if err := b.conn.Call(ctx, "", "Target.getTargets", nil, &r); err != nil {
		return
	}
	for _, info := range r.TargetInfos {
		if info.Type != "service_worker" || extensionOf(info.URL) != extension || m.workerFollowed(info.TargetID) {
			continue
		}
		var a struct {
			SessionID string `json:"sessionId"`
		}
		if err := b.conn.Call(ctx, "", "Target.attachToTarget", map[string]any{"targetId": info.TargetID, "flatten": true}, &a); err != nil {
			continue
		}
		m.followWorker(b, a.SessionID, info.TargetID, extension)
	}
}

func (m *Manager) workerFollowed(target string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, w := range m.extWorkers {
		if w.target == target {
			return true
		}
	}
	return false
}

// workerEvent hands an event of a followed worker to the listeners; false when the session is not one.
func (m *Manager) workerEvent(e cdp.Event) bool {
	m.mu.Lock()
	w := m.extWorkers[e.Session]
	m.mu.Unlock()
	if w == nil {
		return false
	}
	for _, l := range m.listeners {
		if wl, ok := l.(WorkerListener); ok {
			wl.WorkerEvent(w.browser, w.extension, e.Session, e)
		}
	}
	return true
}

// forgetWorker drops a worker that went, by its session or its target.
func (m *Manager) forgetWorker(session, target string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for s, w := range m.extWorkers {
		if s == session || (target != "" && w.target == target) {
			delete(m.extWorkers, s)
		}
	}
}

// forgetWorkersOf drops every worker of a browser that exited.
func (m *Manager) forgetWorkersOf(b *Browser) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for s, w := range m.extWorkers {
		if w.browser == b {
			delete(m.extWorkers, s)
		}
	}
}

// workerAttached decides what to do with a worker Chromium attached the daemon to: an extension's of
// the agent's is followed; true when it was.
func (m *Manager) workerAttached(b *Browser, raw json.RawMessage) bool {
	var p struct {
		SessionID  string     `json:"sessionId"`
		TargetInfo targetInfo `json:"targetInfo"`
	}
	if json.Unmarshal(raw, &p) != nil || p.TargetInfo.Type != "service_worker" {
		return false
	}
	id := extensionOf(p.TargetInfo.URL)
	if id == "" || !b.wantsExtension(id) {
		return false
	}
	go m.followWorker(b, p.SessionID, p.TargetInfo.TargetID, id)
	return true
}
