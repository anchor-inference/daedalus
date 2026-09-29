// Package page is what the agent sees of a page and how it acts on one: the outline with refs, the
// readable text, masked screenshots, actions dispatched as real input, the classification of
// sensitive ones, dialogs, waiting, downloads and uploads, and the moments the operator is needed.
// Everything it reads from a page it reads through its own script in an isolated world.
package page

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

//go:embed js/page.js
var pageJS string

// worldName names the daemon's isolated world. A page cannot reach a world by its name.
const worldName = "browserd"

// Model is the page model of every tab.
type Model struct {
	m        *browser.Manager
	log      *slog.Logger
	lim      config.Limits
	stateDir string

	// run tells this daemon's actions from an earlier one's: a recording outlives the daemon, and its
	// keyframes name the action they followed, so a restarted daemon counting from a1 again would
	// have its new actions match the old keyframes.
	run       string
	mu        sync.Mutex
	actions   int
	downloads map[string]*Download
	byGUID    map[string]*Download
	uploads   map[string]*upload
	asked     map[string]string // tab -> the document a needs_you was raised for, so it is raised once

	// AfterAction is told of every action that was done, once its page settled (the recording's
	// keyframe). It must not block.
	AfterAction func(t *browser.Tab, actionID string)
}

// New returns the page model.
func New(m *browser.Manager, cfg *config.Config, log *slog.Logger) *Model {
	raw := make([]byte, 3)
	_, _ = rand.Read(raw)
	return &Model{m: m, log: log, lim: cfg.Limits, run: hex.EncodeToString(raw), stateDir: cfg.StateDir, downloads: map[string]*Download{},
		byGUID: map[string]*Download{}, uploads: map[string]*upload{}, asked: map[string]string{}}
}

type worldKey struct{}

type world struct {
	loader string
	ctx    int
}

// errStale is what the script answers for a ref that is gone.
type scriptError struct {
	Error      string   `json:"error"`
	Ref        string   `json:"ref"`
	Options    []string `json:"options"`
	CoveredBy  string   `json:"covered_by"`
	Unexpected string   `json:"-"`
}

// world returns the tab's isolated world for its current document, creating it and running the
// daemon's script in it the first time.
func (p *Model) world(ctx context.Context, t *browser.Tab) (int, error) {
	loader := t.Loader()
	if w, ok := t.Value(worldKey{}).(*world); ok && w != nil && w.loader == loader {
		return w.ctx, nil
	}
	var r struct {
		ExecutionContextID int `json:"executionContextId"`
	}
	if err := t.Call(ctx, "Page.createIsolatedWorld", map[string]any{"frameId": t.TargetID, "worldName": worldName,
		"grantUniveralAccess": false}, &r); err != nil {
		return 0, err
	}
	var ev evalResult
	if err := t.Call(ctx, "Runtime.evaluate", map[string]any{"expression": pageJS, "contextId": r.ExecutionContextID,
		"returnByValue": true}, &ev); err != nil {
		return 0, err
	}
	if ev.ExceptionDetails != nil {
		return 0, fmt.Errorf("the page script did not load: %s", ev.ExceptionDetails.text())
	}
	t.SetValue(worldKey{}, &world{loader: loader, ctx: r.ExecutionContextID})
	return r.ExecutionContextID, nil
}

type evalResult struct {
	Result struct {
		Type     string          `json:"type"`
		Value    json.RawMessage `json:"value"`
		ObjectID string          `json:"objectId"`
	} `json:"result"`
	ExceptionDetails *exception `json:"exceptionDetails"`
}

type exception struct {
	Text      string `json:"text"`
	Exception struct {
		Description string `json:"description"`
	} `json:"exception"`
}

func (e *exception) text() string {
	if e.Exception.Description != "" {
		return e.Exception.Description
	}
	return e.Text
}

// call runs __browserd.<fn>(args…) in the tab's world and returns its value. A page stopped under a
// dialog cannot run anything, so an open dialog is refused before the call rather than waited on.
func (p *Model) call(ctx context.Context, t *browser.Tab, fn string, args ...any) (json.RawMessage, error) {
	r, err := p.eval(ctx, t, fn, false, args...)
	if err != nil {
		return nil, err
	}
	return r.Result.Value, nil
}

// object is call returning a handle on the result (an element) rather than its value.
func (p *Model) object(ctx context.Context, t *browser.Tab, fn string, args ...any) (string, error) {
	r, err := p.eval(ctx, t, fn, true, args...)
	if err != nil {
		return "", err
	}
	if r.Result.ObjectID == "" {
		return "", errStale(fmt.Sprint(args...))
	}
	return r.Result.ObjectID, nil
}

func (p *Model) eval(ctx context.Context, t *browser.Tab, fn string, byRef bool, args ...any) (*evalResult, error) {
	parts := make([]string, len(args))
	for i, a := range args {
		b, err := json.Marshal(a)
		if err != nil {
			return nil, err
		}
		parts[i] = string(b)
	}
	expr := "__browserd." + fn + "(" + strings.Join(parts, ",") + ")"
	for attempt := 0; ; attempt++ {
		if d := t.Dialog(); d != nil {
			return nil, browser.ErrDialogOpen(d)
		}
		if t.Closed() {
			return nil, wire.Errorf(browser.CodeNoSuchTab, "the tab was closed")
		}
		id, err := p.world(ctx, t)
		if err != nil {
			return nil, err
		}
		var r evalResult
		err = t.Call(ctx, "Runtime.evaluate", map[string]any{"expression": expr, "contextId": id,
			"returnByValue": !byRef, "awaitPromise": true}, &r)
		if err != nil && attempt == 0 && strings.Contains(err.Error(), "context") {
			// The document changed between the world's creation and the call: once more on the new one.
			t.SetValue(worldKey{}, (*world)(nil))
			continue
		}
		if err != nil {
			return nil, err
		}
		if r.ExceptionDetails != nil {
			return nil, fmt.Errorf("the page script failed in %s: %s", fn, r.ExceptionDetails.text())
		}
		return &r, nil
	}
}

func errStale(ref string) *wire.Error {
	e := wire.Errorf(browser.CodeStaleRef, "ref %s is not on the page any more; take a new snapshot", ref)
	e.Data = map[string]any{"ref": ref}
	return e
}

// checkScript turns the script's own error answers into the contract's errors.
func checkScript(raw json.RawMessage) error {
	var se scriptError
	if json.Unmarshal(raw, &se) != nil || se.Error == "" {
		return nil
	}
	switch se.Error {
	case "stale":
		return errStale(se.Ref)
	case "no_option":
		e := wire.Errorf(wire.CodeInvalidParams, "no such option; the options are: %s", strings.Join(se.Options, ", "))
		e.Data = map[string]any{"options": se.Options}
		return e
	case "not_select":
		return wire.Errorf(wire.CodeInvalidParams, "select works on a <select>; click the option instead")
	}
	return fmt.Errorf("the page script answered %s", se.Error)
}

// TabEvent watches pages for what needs the operator: a site asking for a password (HTTP
// authentication, which the agent never answers) and a CAPTCHA.
func (p *Model) TabEvent(t *browser.Tab, e cdp.Event) {
	switch e.Method {
	case "Network.responseReceived":
		var r struct {
			Type     string `json:"type"`
			FrameID  string `json:"frameId"`
			Response struct {
				URL     string            `json:"url"`
				Status  int               `json:"status"`
				Headers map[string]string `json:"headers"`
			} `json:"response"`
		}
		if json.Unmarshal(e.Params, &r) != nil || r.Type != "Document" || r.Response.Status != 401 {
			return
		}
		for k := range r.Response.Headers {
			if strings.EqualFold(k, "www-authenticate") {
				p.needsYou(t, "basic_auth", "The site asks for a user name and password (HTTP authentication).", r.Response.URL, "")
				return
			}
		}
	case "Page.lifecycleEvent":
		var l struct {
			FrameID string `json:"frameId"`
			Name    string `json:"name"`
		}
		if json.Unmarshal(e.Params, &l) != nil || l.FrameID != t.TargetID || l.Name != "load" {
			return
		}
		go p.checkCaptcha(t)
	}
}

func (p *Model) checkCaptcha(t *browser.Tab) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, err := p.call(ctx, t, "captchas")
	if err != nil {
		return
	}
	var found []string
	if json.Unmarshal(raw, &found) != nil || len(found) == 0 {
		return
	}
	p.needsYou(t, "captcha", "The page shows a CAPTCHA.", t.URL(), t.Loader())
}

// needsYou publishes a request for the operator, once per document of a tab.
func (p *Model) needsYou(t *browser.Tab, reason, what, url, doc string) {
	key := t.ID + "/" + reason
	p.mu.Lock()
	if doc != "" && p.asked[key] == doc {
		p.mu.Unlock()
		return
	}
	p.asked[key] = doc
	p.mu.Unlock()
	p.m.Publish("needs_you", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "reason": reason, "what": what,
		"url": url, "by": "daemon"})
}

// TabGone forgets what the model kept for a tab.
func (p *Model) TabGone(t *browser.Tab) {
	p.mu.Lock()
	for k := range p.asked {
		if strings.HasPrefix(k, t.ID+"/") {
			delete(p.asked, k)
		}
	}
	p.mu.Unlock()
}

// HumanInput remembers the field a person types into as secret, for the life of its document.
func (p *Model) HumanInput(t *browser.Tab, kind string) {
	if kind != "key" && kind != "text" {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = p.call(ctx, t, "markHumanTyped")
	}()
}

// Selection is what the operator copies while they drive: the text selected on the page, at most max
// characters, and whether it was cut short. withheld says the focus is on a password field, whose
// value never leaves the page this way (js/page.js, selection).
func (p *Model) Selection(ctx context.Context, t *browser.Tab, max int) (text string, truncated, withheld bool, err error) {
	raw, err := p.call(ctx, t, "selection", max)
	if err != nil {
		return "", false, false, err
	}
	var r struct {
		Text      string `json:"text"`
		Truncated bool   `json:"truncated"`
		Withheld  bool   `json:"withheld"`
	}
	if err := json.Unmarshal(raw, &r); err != nil {
		return "", false, false, fmt.Errorf("the page script answered a selection that is not one: %w", err)
	}
	return r.Text, r.Truncated, r.Withheld, nil
}
