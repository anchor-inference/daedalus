package page

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"mime"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// uploadTarget is where an upload named by a ref goes: the file input's ref, and how it was found
// from the ref named ("itself", "label", "inside", "near"); Ref is "" when no input was found.
type uploadTarget struct {
	Ref string `json:"ref"`
	How string `json:"how"`
}

// chooserWait is how long a click is given to open the page's file chooser.
const chooserWait = 3 * time.Second

// pieceBytes bounds one piece of a file sent into the operator's Chrome: a command to the extension
// is at most relay.MaxToExtension (1 MiB), and base64 grows a piece by a third.
const pieceBytes = 512 << 10

func (p *Model) fileTarget(ctx context.Context, t *browser.Tab, d *doc, ref string) (*uploadTarget, error) {
	raw, err := p.callIn(ctx, t, d, "fileTarget", ref)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var u uploadTarget
	if err := json.Unmarshal(raw, &u); err != nil {
		return nil, err
	}
	return &u, nil
}

// The functions run on the file input itself (Runtime.callFunctionOn), in the daemon's own world:
// what they keep on the element there is invisible to the page.
const (
	// armFiles starts listening for what the page will be told. On the window and in the capture
	// phase, so it hears the change before any listener of the page's, one of which may empty the
	// field (React upload widgets do, to let the same file be chosen again) or replace the input.
	armFiles = `function() {
  const el = this, rec = { input: 0, change: 0, seen: [] };
  const note = (e) => {
    if (e.target !== el) return;
    rec[e.type]++;
    if (e.type === "change") rec.seen = Array.from(el.files || [], (f) => f.name);
  };
  window.addEventListener("input", note, true);
  window.addEventListener("change", note, true);
  rec.off = () => { window.removeEventListener("input", note, true); window.removeEventListener("change", note, true); };
  el.__browserdUpload = rec;
  return { multiple: !!el.multiple, disabled: !!el.disabled, type: (el.getAttribute("type") || "").toLowerCase() };
}`
	// readFiles says what the input holds now and what the page was told; with stop, it stops
	// listening.
	readFiles = `function(stop) {
  const rec = this.__browserdUpload || { input: 0, change: 0, seen: [] };
  if (stop) {
    if (rec.off) rec.off();
    delete this.__browserdUpload;
  }
  return { now: Array.from(this.files || [], (f) => f.name), seen: rec.seen, input: rec.input, change: rec.change, connected: this.isConnected };
}`
	// tellFiles sends the events a page's framework listens to, for files that are in the input.
	tellFiles = `function() {
  this.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  this.dispatchEvent(new Event("change", { bubbles: true }));
}`
	// pieceOf keeps one piece of file i.
	pieceOf = `function(i, b64) {
  const parts = (this.__browserdParts ||= []);
  const bin = atob(b64), bytes = new Uint8Array(bin.length);
  for (let k = 0; k < bin.length; k++) bytes[k] = bin.charCodeAt(k);
  (parts[i] ||= []).push(bytes);
  return true;
}`
	// putFiles makes the files from their pieces, puts them in the input and tells the page, as a
	// person's choice in the chooser would.
	putFiles = `function(files) {
  const parts = this.__browserdParts || [];
  delete this.__browserdParts;
  const dt = new DataTransfer();
  files.forEach((f, i) => dt.items.add(new File(parts[i] || [], f.name, { type: f.type })));
  this.files = dt.files;
  this.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  this.dispatchEvent(new Event("change", { bubbles: true }));
  return this.files.length;
}`
)

func (p *Model) on(ctx context.Context, t *browser.Tab, d *doc, obj, fn string, result any, args ...any) error {
	params := map[string]any{"objectId": obj, "functionDeclaration": fn, "returnByValue": true, "awaitPromise": true}
	if len(args) > 0 {
		list := make([]map[string]any, len(args))
		for i, a := range args {
			list[i] = map[string]any{"value": a}
		}
		params["arguments"] = list
	}
	var r evalResult
	if err := t.CallIn(ctx, d.session, "Runtime.callFunctionOn", params, &r); err != nil {
		return err
	}
	if r.ExceptionDetails != nil {
		return wire.Errorf(wire.CodeInternal, "the page refused the files: %s", r.ExceptionDetails.text())
	}
	if result != nil && len(r.Result.Value) > 0 {
		return json.Unmarshal(r.Result.Value, result)
	}
	return nil
}

// attachFiles puts the files at paths into the file input obj and proves they arrived: the input
// holds them, or the page was told of them (its change event saw them) and took them away itself.
// Anything else is an error, never "done": a tool once answered done on a form that had no file.
//
// The daemon's own browser takes the files from its disk (DOM.setFileInputFiles, a hidden input as
// well as a shown one). The operator's Chrome is on another computer, where those paths are not, and
// an extension may not hand Chrome local files, so there the files cross as their bytes and are put
// in the input by the script, with the events a person's choice sends.
func (p *Model) attachFiles(ctx context.Context, t *browser.Tab, d *doc, obj string, paths []string, how string, res *ActResult) error {
	var armed struct {
		Multiple bool   `json:"multiple"`
		Disabled bool   `json:"disabled"`
		Type     string `json:"type"`
	}
	if err := p.on(ctx, t, d, obj, armFiles, &armed); err != nil {
		return notDone(err)
	}
	read := func() (*filesNow, error) {
		var r filesNow
		return &r, p.on(ctx, t, d, obj, readFiles, &r, true)
	}
	refuse := func(format string, args ...any) error {
		_, _ = read()
		return notDone(wire.Errorf(wire.CodeForbidden, format, args...))
	}
	if armed.Type != "file" {
		return refuse("the chooser belongs to something that is not a file input")
	}
	if armed.Disabled {
		return refuse("the file input is disabled; the page enables it once the form allows a file")
	}
	if len(paths) > 1 && !armed.Multiple {
		return refuse("this file input takes one file; upload them one at a time, or to an input that takes several")
	}
	want := make([]string, len(paths))
	for i, path := range paths {
		want[i] = filepath.Base(path)
	}
	if t.Group.Browser.Attached() {
		if err := p.sendFiles(ctx, t, d, obj, paths); err != nil {
			_, _ = read()
			return notDone(err)
		}
	} else if err := t.CallIn(ctx, d.session, "DOM.setFileInputFiles", map[string]any{"files": paths, "objectId": obj}, nil); err != nil {
		_, _ = read()
		return notDone(err)
	}
	// The browser tells the page as it sets the files; a moment is allowed for it all the same.
	for i := 0; i < 10; i++ {
		var peek filesNow
		if err := p.on(ctx, t, d, obj, readFiles, &peek, false); err != nil || peek.Change > 0 {
			break
		}
		time.Sleep(30 * time.Millisecond)
	}
	got, err := read()
	if err != nil {
		return notDone(err)
	}
	if got.Change == 0 && sameNames(got.Now, want) {
		// The files are in, and the page was not told: say it as the chooser would.
		if err := p.on(ctx, t, d, obj, armFiles, nil); err == nil {
			_ = p.on(ctx, t, d, obj, tellFiles, nil)
			if again, err := read(); err == nil {
				got = again
			}
		}
	}
	told := got.Change > 0 && sameNames(got.Seen, want)
	if !told && !sameNames(got.Now, want) {
		e := wire.Errorf(wire.CodeForbidden, "the files did not attach: the file input holds %d of the %d after the upload, and the page was not told of them; "+
			"look at the page (BrowserLook) for a message of its own, or try the button that opens its chooser", len(got.Now), len(want))
		e.Data = map[string]any{"not_done": true, "reason": "not_attached", "now": got.Now}
		return e
	}
	res.Effects["uploaded"] = map[string]any{"files": want, "how": how, "cleared": len(got.Now) == 0 || !got.Connected}
	return nil
}

type filesNow struct {
	Now       []string `json:"now"`
	Seen      []string `json:"seen"`
	Input     int      `json:"input"`
	Change    int      `json:"change"`
	Connected bool     `json:"connected"`
}

func sameNames(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	x, y := slices.Clone(a), slices.Clone(b)
	slices.Sort(x)
	slices.Sort(y)
	return slices.Equal(x, y)
}

// sendFiles carries each file into the input's world a piece at a time, then puts them in.
func (p *Model) sendFiles(ctx context.Context, t *browser.Tab, d *doc, obj string, paths []string) error {
	type meta struct {
		Name string `json:"name"`
		Type string `json:"type"`
	}
	metas := make([]meta, len(paths))
	buf := make([]byte, pieceBytes)
	for i, path := range paths {
		name := filepath.Base(path)
		kind := mime.TypeByExtension(strings.ToLower(filepath.Ext(name)))
		if kind == "" {
			kind = "application/octet-stream"
		}
		metas[i] = meta{Name: name, Type: kind}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		for {
			n, err := io.ReadFull(f, buf)
			if n > 0 {
				if perr := p.on(ctx, t, d, obj, pieceOf, nil, i, base64.StdEncoding.EncodeToString(buf[:n])); perr != nil {
					f.Close()
					return perr
				}
			}
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				break
			}
			if err != nil {
				f.Close()
				return err
			}
		}
		f.Close()
	}
	return p.on(ctx, t, d, obj, putFiles, nil, metas)
}

// chooserKey keys, among a tab's values, the channel an upload through a chooser waits on.
type chooserKey struct{}

type chooserOpened struct {
	BackendNodeID int    `json:"backendNodeId"`
	Mode          string `json:"mode"`
}

// chooserEvent hands Page.fileChooserOpened to the upload waiting for it, if one is.
func (p *Model) chooserEvent(t *browser.Tab, e cdp.Event) {
	ch, _ := t.Value(chooserKey{}).(chan chooserOpened)
	if ch == nil {
		return
	}
	var c chooserOpened
	if json.Unmarshal(e.Params, &c) != nil {
		return
	}
	select {
	case ch <- c:
	default:
	}
}

// uploadThroughChooser clicks ref with the page's file chooser intercepted, and puts the files in
// the input the chooser was opened for. A click that opens none is refused as not done.
func (p *Model) uploadThroughChooser(ctx context.Context, t *browser.Tab, d *doc, ref string, pt point, paths []string, res *ActResult, g *gesture) error {
	if err := t.CallIn(ctx, d.session, "Page.setInterceptFileChooserDialog", map[string]any{"enabled": true}, nil); err != nil {
		return notDone(err)
	}
	ch := make(chan chooserOpened, 1)
	t.SetValue(chooserKey{}, ch)
	defer func() {
		t.SetValue(chooserKey{}, (chan chooserOpened)(nil))
		// Off again, so a chooser the operator opens later is the browser's own.
		_ = t.CallIn(context.WithoutCancel(ctx), d.session, "Page.setInterceptFileChooserDialog", map[string]any{"enabled": false}, nil)
	}()
	if err := p.clickIn(ctx, t, d, ref, pt, "left", 1, g); err != nil {
		return err
	}
	var opened chooserOpened
	select {
	case opened = <-ch:
	case <-time.After(chooserWait):
		e := wire.Errorf(wire.CodeInvalidParams, "%s is not a file input, none is near it, and clicking it opened no file chooser; "+
			"find the page's file input (BrowserInspect(selector='input[type=file]')) and upload to its ref", ref)
		e.Data = map[string]any{"not_done": true, "reason": "no_chooser"}
		return e
	case <-ctx.Done():
		return ctx.Err()
	}
	if opened.Mode == "selectSingle" && len(paths) > 1 {
		return notDone(wire.Errorf(wire.CodeForbidden, "the page's chooser takes one file; upload them one at a time"))
	}
	id, err := p.world(ctx, t, d)
	if err != nil {
		return notDone(err)
	}
	var node struct {
		Object struct {
			ObjectID string `json:"objectId"`
		} `json:"object"`
	}
	if err := t.CallIn(ctx, d.session, "DOM.resolveNode", map[string]any{"backendNodeId": opened.BackendNodeID, "executionContextId": id}, &node); err != nil {
		return notDone(err)
	}
	return p.attachFiles(ctx, t, d, node.Object.ObjectID, paths, "chooser", res)
}
