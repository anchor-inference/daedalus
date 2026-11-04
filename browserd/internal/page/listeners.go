package page

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
)

// The events whose listener makes an element something a person clicks or presses.
var clickEvents = map[string]bool{"click": true, "mousedown": true, "mouseup": true, "pointerdown": true,
	"pointerup": true, "touchstart": true, "touchend": true}

// maxListened bounds the elements marked in one snapshot: each is a round trip on the pipe.
const maxListened = 2000

// maxPierced bounds the elements DOMDebugger.getEventListeners walks with pierce: the main document's
// element count (listenerRoot) misses its shadow roots and same-origin frames, which pierce goes
// through too. Its reply is one relay frame from the operator's Chrome; unbounded, a page of a
// storefront made a frame of several megabytes, which once closed the relay mid-snapshot.
const maxPierced = 10000

// piercedCount counts, in the daemon's world, the elements a pierced walk from the document meets,
// stopping past maxPierced. A closed shadow root is not seen here; Chromium walks it anyway.
const piercedCount = `(() => {
  const limit = %d;
  let n = 0;
  const walk = (root) => {
    const all = root.querySelectorAll("*");
    n += all.length;
    for (const el of all) {
      if (n > limit) return;
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
        let inner = null;
        try { inner = el.contentDocument; } catch (e) {}
        if (inner) walk(inner);
      }
    }
  };
  walk(document);
  return n;
})()`

// markGroup is the object group the marking's handles are made in, released together after it.
const markGroup = "browserd-listeners"

// marksKey keys, among a tab's values, the nodes a document's world has marked.
type marksKey struct{ session, frame string }

// marked is the backend ids of the nodes a world has marked, for the world it was.
type marked struct {
	world int
	nodes map[int]bool
}

// markListeners tells the document's world which elements a page script listens on for a click: a
// <div> given a handler with addEventListener is as clickable as a button and nothing in its markup
// says so. The page script cannot learn it from inside (an isolated world sees only its own
// listeners); Chromium's DOMDebugger.getEventListeners reports every world's, for the whole tree in
// one call, with the node of each. Each node is then resolved into the daemon's world and marked
// there. It is skipped on a page past the script's element count, and every failure only leaves the
// page as the markup alone describes it.
func (p *Model) markListeners(ctx context.Context, t *browser.Tab, d *doc) {
	if t.Dialog() != nil {
		// A page stopped under a dialog answers nothing; the snapshot's own call says why.
		return
	}
	id, err := p.world(ctx, t, d)
	if err != nil {
		return
	}
	defer func() {
		_ = t.CallIn(ctx, d.session, "Runtime.releaseObjectGroup", map[string]any{"objectGroup": markGroup}, nil)
	}()
	var count struct {
		Result struct {
			Value int `json:"value"`
		} `json:"result"`
	}
	if err := t.CallIn(ctx, d.session, "Runtime.evaluate", map[string]any{"expression": fmt.Sprintf(piercedCount, maxPierced),
		"contextId": id, "returnByValue": true}, &count); err != nil || count.Result.Value > maxPierced {
		return
	}
	var root evalResult
	if err := t.CallIn(ctx, d.session, "Runtime.evaluate", map[string]any{"expression": "__browserd.listenerRoot()",
		"contextId": id, "objectGroup": markGroup}, &root); err != nil || root.Result.ObjectID == "" {
		return
	}
	// The document is asked about through a handle of the page's own world, made from its node with
	// no script run. Asked through the daemon's world, Chromium's renderer hangs for good on the
	// second such question with pierce (measured on the pinned build: every later command of the tab
	// went unanswered, snapshots and all).
	var node struct {
		Node struct {
			BackendNodeID int `json:"backendNodeId"`
		} `json:"node"`
	}
	if err := t.CallIn(ctx, d.session, "DOM.describeNode", map[string]any{"objectId": root.Result.ObjectID}, &node); err != nil {
		return
	}
	var main struct {
		Object struct {
			ObjectID string `json:"objectId"`
		} `json:"object"`
	}
	if err := t.CallIn(ctx, d.session, "DOM.resolveNode", map[string]any{"backendNodeId": node.Node.BackendNodeID,
		"objectGroup": markGroup}, &main); err != nil || main.Object.ObjectID == "" {
		return
	}
	var ls struct {
		Listeners []struct {
			Type          string `json:"type"`
			BackendNodeID int    `json:"backendNodeId"`
		} `json:"listeners"`
	}
	// pierce is what makes Chromium report the page's listeners and not only this world's; it also
	// goes through shadow roots and same-origin frames. A frame of another site is marked from its
	// own world, and a node of one that does not resolve here is skipped.
	if err := t.CallIn(ctx, d.session, "DOMDebugger.getEventListeners", map[string]any{"objectId": main.Object.ObjectID,
		"depth": -1, "pierce": true}, &ls); err != nil {
		return
	}
	// The nodes this world has marked already are not resolved again: a page asked about twice
	// is mostly the same page.
	key := marksKey{d.session, d.frame}
	m, _ := t.Value(key).(*marked)
	if m == nil || m.world != id {
		m = &marked{world: id, nodes: map[int]bool{}}
		t.SetValue(key, m)
	}
	seen := map[int]bool{}
	var nodes []int
	for _, l := range ls.Listeners {
		if !clickEvents[l.Type] || l.BackendNodeID == 0 || seen[l.BackendNodeID] || m.nodes[l.BackendNodeID] {
			continue
		}
		seen[l.BackendNodeID] = true
		m.nodes[l.BackendNodeID] = true
		nodes = append(nodes, l.BackendNodeID)
		if len(nodes) >= maxListened {
			break
		}
	}
	if len(nodes) == 0 {
		return
	}
	// Resolved many at a time: the calls go out on the pipe back to back and are answered in order.
	handles := make([]string, len(nodes))
	var wg sync.WaitGroup
	sem := make(chan struct{}, 64)
	for i, n := range nodes {
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			var r struct {
				Object struct {
					ObjectID string `json:"objectId"`
				} `json:"object"`
			}
			if t.CallIn(ctx, d.session, "DOM.resolveNode", map[string]any{"backendNodeId": n, "executionContextId": id,
				"objectGroup": markGroup}, &r) == nil {
				handles[i] = r.Object.ObjectID
			}
		}()
	}
	wg.Wait()
	var args []map[string]any
	for _, h := range handles {
		if h != "" {
			args = append(args, map[string]any{"objectId": h})
		}
	}
	for len(args) > 0 {
		chunk := args[:min(len(args), 500)]
		args = args[len(chunk):]
		var r json.RawMessage
		_ = t.CallIn(ctx, d.session, "Runtime.callFunctionOn", map[string]any{
			"functionDeclaration": "function() { return __browserd.markListened(Array.from(arguments)); }",
			"executionContextId":  id, "arguments": chunk, "returnByValue": true}, &r)
	}
}
