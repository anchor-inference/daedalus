package browser

import (
	"context"
	"fmt"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
)

// The screen position of a click. A mouse event a person makes carries where on the screen it
// happened: its place in the page, plus the frame's place in the page, plus the window's place on
// the screen and the height of the browser's own toolbar. Some Chromium builds give an event sent
// through Input.dispatchMouseEvent its place in the page as its screen position (crbug 40280325),
// so screenX equals clientX and screenY equals clientY, which no window with a toolbar can produce,
// and a challenge that compares them knows the click was not a person's — the operator's clicks
// during a takeover included, since they reach the page the same way.
//
// The builds measured here (Chrome for Testing 151, Debian's Chromium 155) already report it right,
// headless and headed, in the page and in frames of other sites. So the daemon asks each browser
// once, on its first page, and puts the script in only where the answer is wrong: on a build that
// is right the script would change nothing a click reports, yet a page could still find the
// replaced getter, and a fix that can be seen is a tell of its own.

// screenFixScript replaces MouseEvent's screenX and screenY getters with ones that, for a trusted
// event bearing the defect's mark (screen position equal to page position), add what a real window
// adds. frameW and frameH are how much larger the window is than its page, as the daemon measured
// it: a frame below the top cannot read that itself, since its innerHeight is the frame's. A frame's
// own place in the page is added where the frames above it are of its origin; a frame of another
// site cannot learn it.
//
// The getters stay the native ones wrapped in a Proxy, so their name, length and toString are a
// native function's; an event the page made itself (isTrusted false) is never touched.
func screenFixScript(frameW, frameH int) string {
	return fmt.Sprintf(`(() => {
  const P = MouseEvent.prototype;
  const dx = Object.getOwnPropertyDescriptor(P, "screenX"), dy = Object.getOwnPropertyDescriptor(P, "screenY");
  const ox = Object.getOwnPropertyDescriptor(P, "clientX"), oy = Object.getOwnPropertyDescriptor(P, "clientY");
  if (!dx || !dy || !ox || !oy || typeof dx.get !== "function" || typeof dy.get !== "function") return;
  const apply = Reflect.apply, sx = dx.get, sy = dy.get, cx = ox.get, cy = oy.get;
  const W = %d, H = %d;
  const chrome = () => {
    if (window === window.top) {
      const side = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
      return [side, Math.max(0, window.outerHeight - window.innerHeight - side)];
    }
    return [W / 2, Math.max(0, H - W / 2)];
  };
  const place = () => {
    let x = 0, y = 0, w = window;
    try {
      while (w !== w.top) {
        const f = w.frameElement;
        if (!f) break;
        const r = f.getBoundingClientRect();
        x += r.left + f.clientLeft;
        y += r.top + f.clientTop;
        w = w.parent;
      }
    } catch (e) {}
    return [x, y];
  };
  const fixed = (e, v, i) => {
    if (!e.isTrusted) return v;
    const x = apply(cx, e, []), y = apply(cy, e, []);
    const other = apply(i ? sx : sy, e, []);
    if ((i ? other : v) !== x || (i ? v : other) !== y) return v;
    const c = chrome(), p = place();
    return i ? y + p[1] + window.screenY + c[1] : x + p[0] + window.screenX + c[0];
  };
  const wrap = (native, i) => new Proxy(native, {
    apply(target, that, args) {
      const v = apply(target, that, args);
      return that instanceof MouseEvent ? fixed(that, v, i) : v;
    },
  });
  Object.defineProperty(P, "screenX", { get: wrap(sx, 0), set: dx.set, enumerable: dx.enumerable, configurable: true });
  Object.defineProperty(P, "screenY", { get: wrap(sy, 1), set: dy.set, enumerable: dy.enumerable, configurable: true });
})();`, frameW, frameH)
}

// screenProbe is the point the probe moves the mouse to on a browser's first page: inside any
// viewport the daemon makes, and nowhere a page has drawn anything yet (the page is blank).
var screenProbe = [2]int{13, 17}

// probeScreen learns, on a browser's first page, whether a dispatched mouse event reports its page
// position as its screen position, and so whether this browser's pages get screenFixScript. It is
// asked once per browser; a page set up meanwhile waits for the answer (screenFix). The pointer is
// left where the probe put it, and the tab is told so.
func (m *Manager) probeScreen(ctx context.Context, b *Browser, t *Tab) {
	session := t.Session
	b.mu.Lock()
	if b.screenAsked {
		b.mu.Unlock()
		return
	}
	b.screenAsked = true
	b.mu.Unlock()
	fix := false
	defer func() {
		b.mu.Lock()
		b.screenFix = fix
		b.mu.Unlock()
		close(b.screenKnown)
	}()
	switch m.deps.Config.Input.ScreenFix {
	case config.ScreenFixAlways:
		fix = true
		return
	case config.ScreenFixNever:
		return
	}
	pctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	sx, sy, cx, cy, err := m.askScreen(pctx, b, session)
	t.SetPointer(float64(screenProbe[0]), float64(screenProbe[1]))
	if err != nil {
		// Unknown is left alone: a fix applied to a browser that reports right would be the tell.
		m.log.Warn("screen position probe", "browser", b.ID, "error", err.Error())
		return
	}
	fix = sx == cx && sy == cy
	m.log.Info("screen position probe", "browser", b.ID, "screen", [2]float64{sx, sy}, "client", [2]float64{cx, cy}, "fix", fix)
}

// askScreen moves the mouse on the page and reads back the event's screen and page positions. The
// listener lives in a world of the daemon's own, which sees the page's events and leaves nothing in
// the page's.
func (m *Manager) askScreen(ctx context.Context, b *Browser, session string) (sx, sy, cx, cy float64, err error) {
	var tree struct {
		FrameTree struct {
			Frame struct {
				ID string `json:"id"`
			} `json:"frame"`
		} `json:"frameTree"`
	}
	if err = b.conn.Call(ctx, session, "Page.getFrameTree", nil, &tree); err != nil {
		return
	}
	var world struct {
		ContextID int `json:"executionContextId"`
	}
	if err = b.conn.Call(ctx, session, "Page.createIsolatedWorld", map[string]any{"frameId": tree.FrameTree.Frame.ID,
		"worldName": "browserd-screen"}, &world); err != nil {
		return
	}
	if err = b.conn.Call(ctx, session, "Runtime.evaluate", map[string]any{"contextId": world.ContextID,
		"expression": `globalThis.moved = new Promise(r => addEventListener("mousemove", e => r([e.screenX, e.screenY, e.clientX, e.clientY]), {once: true})); 0`}, nil); err != nil {
		return
	}
	if err = b.conn.Call(ctx, session, "Input.dispatchMouseEvent", map[string]any{"type": "mouseMoved",
		"x": screenProbe[0], "y": screenProbe[1]}, nil); err != nil {
		return
	}
	var r struct {
		Result struct {
			Value []float64 `json:"value"`
		} `json:"result"`
	}
	if err = b.conn.Call(ctx, session, "Runtime.evaluate", map[string]any{"contextId": world.ContextID,
		"expression": "moved", "awaitPromise": true, "returnByValue": true}, &r); err != nil {
		return
	}
	if len(r.Result.Value) != 4 {
		err = fmt.Errorf("the probe's event came back as %v", r.Result.Value)
		return
	}
	v := r.Result.Value
	return v[0], v[1], v[2], v[3], nil
}

// screenScript is the script this browser's pages get before their first document, or "" when
// they get none. A page set up while the browser's first page is still being asked waits for the
// answer, briefly: the first page is a blank one and the probe takes milliseconds.
func (b *Browser) screenScript(ctx context.Context) string {
	select {
	case <-b.screenKnown:
	case <-ctx.Done():
		return ""
	case <-time.After(3 * time.Second):
		return ""
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.screenFix {
		return ""
	}
	return screenFixScript(b.frameW, b.frameH)
}

// screenAnswered says whether the probe has answered, without waiting for it.
func (b *Browser) screenAnswered() bool {
	select {
	case <-b.screenKnown:
		return true
	default:
		return false
	}
}
