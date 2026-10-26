package page

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The agent's own JavaScript, run in a page now, for testing a change to a site before it becomes a
// page script or an extension: it runs in the page's own world, where the page's functions and
// variables are, as a developer's console runs it, so its effect is the page's.
const (
	EvalMaxChars     = 50_000
	evalDefaultChars = 4000
	EvalMaxTimeout   = 60 * time.Second
	// evalSettle is how long the console is given after the answer: Chromium sends the console calls
	// a script made before its answer, but the daemon hands events over on a goroutine of its own, so
	// the last of them may still be on its way when the answer is read.
	evalSettle = 150 * time.Millisecond
	evalLogs   = 30
)

// serialize renders a value as JSON a reader can follow: elements as their tag and a little of their
// markup, functions by name, a cycle where it closes, maps and sets as what they hold, and a budget
// of nodes past which the rest is left out, so window itself is a page of text and not a hang.
const serialize = `function (budget) {
  let left = budget;
  const seen = new WeakSet();
  const node = (n) => {
    if (n.nodeType === 1) {
      const html = n.outerHTML || "";
      return "<" + n.tagName.toLowerCase() + "> " + (html.length > 300 ? html.slice(0, 300) + "…" : html);
    }
    if (n.nodeType === 3) return "#text " + JSON.stringify((n.textContent || "").slice(0, 200));
    return n.nodeName;
  };
  const walk = (v, depth) => {
    if (--left < 0) return "…";
    if (v === null) return null;
    switch (typeof v) {
      case "undefined": return "undefined";
      case "number": return Number.isFinite(v) ? v : String(v);
      case "bigint": return v.toString() + "n";
      case "string": case "boolean": return v;
      case "symbol": return v.toString();
      case "function": return "[function " + (v.name || "anonymous") + "]";
    }
    if (seen.has(v)) return "[circular]";
    if (typeof Node !== "undefined" && v instanceof Node) return node(v);
    if (v instanceof Error) return { error: v.name, message: v.message, stack: String(v.stack || "").split("\n").slice(1, 4).map((s) => s.trim()) };
    if (v instanceof Date) return isNaN(v) ? "Invalid Date" : v.toISOString();
    if (v instanceof RegExp) return v.toString();
    if (depth > 6) return "[" + Object.prototype.toString.call(v).slice(8, -1) + "]";
    seen.add(v);
    if (v instanceof Map) return { "[Map]": [...v].slice(0, 100).map(([k, x]) => [walk(k, depth + 1), walk(x, depth + 1)]) };
    if (v instanceof Set) return { "[Set]": [...v].slice(0, 100).map((x) => walk(x, depth + 1)) };
    if (Array.isArray(v) || ArrayBuffer.isView(v) || (typeof NodeList !== "undefined" && v instanceof NodeList) || (typeof HTMLCollection !== "undefined" && v instanceof HTMLCollection)) {
      const out = Array.from(v).slice(0, 200).map((x) => walk(x, depth + 1));
      if (v.length > 200) out.push("… " + (v.length - 200) + " more");
      return out;
    }
    const out = {};
    let n = 0;
    for (const k in v) {
      if (n++ >= 200) { out["…"] = "more keys"; break; }
      try { out[k] = walk(v[k], depth + 1); } catch (e) { out[k] = "[throws " + e + "]"; }
    }
    return out;
  };
  return JSON.stringify(walk(this, 0), null, 1);
}`

// EvalResult is page.evaluate's answer.
type EvalResult struct {
	URL string `json:"url"`
	// Type is the value's JavaScript type (its subtype for an object: array, node, error …).
	Type string `json:"type"`
	// Value is the value as JSON, or a primitive's text; Truncated says max_chars cut it.
	Value     string `json:"value"`
	Truncated bool   `json:"truncated,omitempty"`
	// Exception is what the code threw, with where; the value is then empty.
	Exception string `json:"exception,omitempty"`
	// Logs are what the page's console took while the code ran: its own console calls among them.
	Logs []LogEntry `json:"logs"`
	// Scripts are the agent's page scripts that run on this page.
	Scripts []string `json:"scripts"`
}

// Evaluate runs expression in the page's main world and returns its value. The code is read as a
// console reads it (replMode): a top-level await waits, a let declared again does not throw, and the
// value of the last statement is the answer. A promise is waited for, at most timeout.
func (p *Model) Evaluate(ctx context.Context, t *browser.Tab, expression string, maxChars int, timeout time.Duration) (*EvalResult, error) {
	if t.Group.Browser.Attached() {
		return nil, browser.ErrAttachRefused("running the agent's own JavaScript")
	}
	if strings.TrimSpace(expression) == "" {
		return nil, wire.Errorf(wire.CodeInvalidParams, "expression is empty")
	}
	if len(expression) > browser.MaxScriptBytes {
		return nil, wire.Errorf(wire.CodeInvalidParams, "expression is at most %d bytes", browser.MaxScriptBytes)
	}
	if maxChars <= 0 {
		maxChars = evalDefaultChars
	}
	maxChars = min(maxChars, EvalMaxChars)
	if timeout <= 0 || timeout > EvalMaxTimeout {
		timeout = 10 * time.Second
	}
	if dl := t.Dialog(); dl != nil {
		return nil, browser.ErrDialogOpen(dl)
	}
	var mark int64
	if c := p.consoleOf(t, false); c != nil {
		c.mu.Lock()
		mark = c.seq
		c.mu.Unlock()
	}
	ectx, cancel := context.WithTimeout(ctx, timeout+2*time.Second)
	defer cancel()
	const group = "browserd-evaluate"
	var r struct {
		Result struct {
			Type        string          `json:"type"`
			Subtype     string          `json:"subtype"`
			Value       json.RawMessage `json:"value"`
			Unserial    string          `json:"unserializableValue"`
			Description string          `json:"description"`
			ObjectID    string          `json:"objectId"`
		} `json:"result"`
		ExceptionDetails *struct {
			Text       string        `json:"text"`
			LineNumber int           `json:"lineNumber"`
			Exception  *remoteObject `json:"exception"`
		} `json:"exceptionDetails"`
	}
	began := time.Now()
	err := t.Call(ectx, "Runtime.evaluate", map[string]any{"expression": expression, "replMode": true, "awaitPromise": true,
		"userGesture": true, "objectGroup": group, "timeout": timeout.Milliseconds()}, &r)
	stopped := "the code ran longer than " + timeout.String() + " and was stopped"
	if err != nil && ctx.Err() == nil && time.Since(began) >= timeout && !errors.Is(err, context.DeadlineExceeded) {
		// Chromium ends a script past its timeout with an internal error rather than an exception
		// (page.go's eval meets the same); the page itself goes on.
		return &EvalResult{URL: t.URL(), Exception: stopped, Logs: []LogEntry{}, Scripts: scriptsOn(t)}, nil
	}
	defer func() {
		rctx, rcancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer rcancel()
		_ = t.Call(rctx, "Runtime.releaseObjectGroup", map[string]any{"objectGroup": group}, nil)
	}()
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
			return nil, wire.Errorf(wire.CodeInvalidParams, "the code did not finish within %s (a promise that never settled?)", timeout)
		}
		if dl := t.Dialog(); dl != nil {
			return nil, browser.ErrDialogOpen(dl)
		}
		return nil, err
	}
	out := &EvalResult{URL: t.URL(), Type: r.Result.Type, Logs: []LogEntry{}, Scripts: scriptsOn(t)}
	if r.Result.Subtype != "" {
		out.Type = r.Result.Subtype
	}
	if d := r.ExceptionDetails; d != nil {
		text := d.Text
		if d.Exception != nil {
			desc := d.Exception.Description
			if desc == "" {
				desc = d.Exception.render(0)
			}
			text = stackHead(desc, logFrames)
		}
		if strings.Contains(text, "Execution was terminated") {
			text = stopped
		}
		out.Exception, out.Type = clipText(text, 2000), ""
	} else {
		switch {
		case r.Result.ObjectID != "":
			var s struct {
				Result struct {
					Value string `json:"value"`
				} `json:"result"`
				ExceptionDetails *struct {
					Text string `json:"text"`
				} `json:"exceptionDetails"`
			}
			err := t.Call(ectx, "Runtime.callFunctionOn", map[string]any{"objectId": r.Result.ObjectID, "functionDeclaration": serialize,
				"arguments": []any{map[string]any{"value": 5000}}, "returnByValue": true, "objectGroup": group}, &s)
			if err != nil || s.ExceptionDetails != nil {
				out.Value = r.Result.Description
			} else {
				out.Value = s.Result.Value
			}
		case r.Result.Type == "undefined":
			out.Value = "undefined"
		case r.Result.Unserial != "":
			out.Value = r.Result.Unserial
		case r.Result.Type == "string":
			var str string
			_ = json.Unmarshal(r.Result.Value, &str)
			out.Value = str
		default:
			out.Value = string(r.Result.Value)
			if out.Value == "" {
				out.Value = r.Result.Description
			}
		}
		if utf8.RuneCountInString(out.Value) > maxChars {
			out.Value, out.Truncated = string([]rune(out.Value)[:maxChars]), true
		}
	}
	select {
	case <-time.After(evalSettle):
	case <-ctx.Done():
	}
	if logs, err := p.Logs(t, mark, "debug", evalLogs); err == nil {
		out.Logs = logs.Entries
	}
	return out, nil
}

func scriptsOn(t *browser.Tab) []string {
	if s := t.Group.Browser.ScriptsOn(t.URL()); s != nil {
		return s
	}
	return []string{}
}

// WorkerEvent keeps the console of an extension's worker (browser.WorkerListener).
func (p *Model) WorkerEvent(b *browser.Browser, extension, session string, e cdp.Event) {
	entry, ok := logEntryOf(e)
	if !ok {
		return
	}
	key := b.ID + "/" + extension
	p.mu.Lock()
	if p.workerLogs == nil {
		p.workerLogs = map[string]*consoleLog{}
	}
	c := p.workerLogs[key]
	if c == nil {
		if len(p.workerLogs) >= 64 {
			// A bound on browsers that came and went: their workers' consoles go first.
			for k := range p.workerLogs {
				delete(p.workerLogs, k)
				break
			}
		}
		c = &consoleLog{}
		p.workerLogs[key] = c
	}
	p.mu.Unlock()
	c.add(entry)
	if e.Method != "Log.entryAdded" {
		releaseConsole(c, func(ctx context.Context) error {
			return b.Conn().Call(ctx, session, "Runtime.releaseObjectGroup", map[string]any{"objectGroup": "console"}, nil)
		}, func() bool {
			select {
			case <-b.Conn().Done():
				return true
			default:
				return false
			}
		})
	}
}

// ExtensionLogs are the console entries of an extension's workers after `after`, as Logs reads a tab's.
func (p *Model) ExtensionLogs(b *browser.Browser, extension string, after int64, level string, limit int) (*LogsResult, error) {
	p.mu.Lock()
	c := p.workerLogs[b.ID+"/"+extension]
	p.mu.Unlock()
	return readLog(c, "chrome-extension://"+extension+"/", after, level, limit)
}
