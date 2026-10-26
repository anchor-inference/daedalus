//go:build unix

package rpc_test

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/page"
)

func (h *harness) evaluate(tab, expression string) page.EvalResult {
	h.t.Helper()
	var r page.EvalResult
	h.must("page.evaluate", map[string]any{"tab_id": tab, "expression": expression}, &r)
	return r
}

// evalUntil evaluates until the value is want: a reload's new document runs a moment after the call
// that asked for it returns.
func (h *harness) evalUntil(tab, expression, want string) {
	h.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		var r page.EvalResult
		err := h.call("page.evaluate", map[string]any{"tab_id": tab, "expression": expression}, &r)
		if err == nil && r.Value == want {
			return
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("%s is %q (%v), not %q", expression, r.Value, err, want)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func (h *harness) reload(tab string) {
	h.t.Helper()
	h.must("page.reload", map[string]any{"tab_id": tab}, nil)
}

func TestEvaluateRunsInThePageAndSaysWhatItLogged(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "p1", "/still")
	if attachRun() {
		// The agent works read-only in the operator's Chrome: its own code is never run there.
		if err := h.call("page.evaluate", map[string]any{"tab_id": o.Tab.ID, "expression": "1"}, nil); code(err) != 1004 {
			t.Fatalf("evaluate in the operator's Chrome: %v", err)
		}
		return
	}
	if r := h.evaluate(o.Tab.ID, "document.title"); r.Value != "Still" || r.Type != "string" {
		t.Fatalf("title: %+v", r)
	}
	// The page's own world: a global the page defines is there, and one set here stays for the page.
	h.evaluate(o.Tab.ID, "window.answer = 41")
	if r := h.evaluate(o.Tab.ID, "answer + 1"); r.Value != "42" || r.Type != "number" {
		t.Fatalf("a global of the page: %+v", r)
	}
	r := h.evaluate(o.Tab.ID, "console.log('from the agent', 7); const x = {el: document.querySelector('h1'), n: [1, 2], self: null}; x.self = x; x")
	if !strings.Contains(r.Value, "<h1>") || !strings.Contains(r.Value, "[circular]") || r.Type != "object" {
		t.Fatalf("an object with an element and a cycle: %+v", r)
	}
	if len(r.Logs) == 0 || !strings.Contains(r.Logs[len(r.Logs)-1].Text, "from the agent 7") {
		t.Fatalf("the console of the call: %+v", r.Logs)
	}
	if r := h.evaluate(o.Tab.ID, "await new Promise((ok) => setTimeout(() => ok('later'), 50))"); r.Value != "later" {
		t.Fatalf("a top-level await: %+v", r)
	}
	if r := h.evaluate(o.Tab.ID, "nothing.here"); !strings.Contains(r.Exception, "ReferenceError") || r.Value != "" {
		t.Fatalf("an exception: %+v", r)
	}
	var cut page.EvalResult
	h.must("page.evaluate", map[string]any{"tab_id": o.Tab.ID, "expression": "'x'.repeat(5000)", "max_chars": 100}, &cut)
	if len(cut.Value) != 100 || !cut.Truncated {
		t.Fatalf("max_chars: %d %v", len(cut.Value), cut.Truncated)
	}
	var slow page.EvalResult
	err := h.call("page.evaluate", map[string]any{"tab_id": o.Tab.ID, "expression": "while (true) {}", "timeout_ms": 500}, &slow)
	if err != nil || !strings.Contains(slow.Exception, "stopped") {
		t.Fatalf("a loop past its time: %+v %v", slow, err)
	}
	if r := h.evaluate(o.Tab.ID, "document.title"); r.Value != "Still" {
		t.Fatalf("the page after the stopped loop: %+v", r)
	}
}

func TestPageScriptsRunAtDocumentStartOnTheirAddresses(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "p1", "/still")
	paint := map[string]any{"group_id": "g1", "name": "paint", "matches": []string{"127.0.0.1"},
		"css": "h1 { color: rgb(1, 2, 3) }", "source": "inline",
		// Before the page's own document is parsed: it has no element yet.
		"js": "window.seen = String(document.documentElement === null); console.log('paint ran')"}
	if attachRun() {
		if err := h.call("script.add", paint, nil); code(err) != 1004 {
			t.Fatalf("a page script in the operator's Chrome: %v", err)
		}
		return
	}
	var added struct {
		Scripts []map[string]any `json:"scripts"`
	}
	h.must("script.add", paint, &added)
	if len(added.Scripts) != 1 || added.Scripts[0]["name"] != "paint" || added.Scripts[0]["source"] != "inline" {
		t.Fatalf("added: %+v", added)
	}
	elsewhere := map[string]any{"group_id": "g1", "name": "elsewhere", "matches": []string{"https://example.com/*"},
		"js": "window.elsewhere = 'yes'"}
	h.must("script.add", elsewhere, nil)
	h.reload(o.Tab.ID)
	h.evalUntil(o.Tab.ID, "window.seen", "true")
	if r := h.evaluate(o.Tab.ID, "getComputedStyle(document.querySelector('h1')).color"); r.Value != "rgb(1, 2, 3)" {
		t.Fatalf("the script's css: %+v", r)
	}
	r := h.evaluate(o.Tab.ID, "window.elsewhere ?? 'none'")
	if r.Value != "none" || len(r.Scripts) != 1 || r.Scripts[0] != "paint" {
		t.Fatalf("a script for another site ran, or the active ones are wrong: %+v", r)
	}
	h.waitLogs(o.Tab.ID, "paint ran")
	// A tab opened later gets the scripts, and its listing names them.
	var tab map[string]any
	h.must("tab.new", map[string]any{"group_id": "g1", "url": h.site.URL + "/button"}, &tab)
	second, _ := tab["id"].(string)
	h.evalUntil(second, "window.seen", "true")
	var listing struct {
		Tabs []map[string]any `json:"tabs"`
	}
	h.must("tab.list", map[string]any{"group_id": "g1"}, &listing)
	for _, tb := range listing.Tabs {
		if s, _ := tb["scripts"].([]any); len(s) != 1 || s[0] != "paint" {
			t.Fatalf("a tab's scripts: %+v", tb)
		}
	}
	// A syntax error is told at once, in the agent's own lines.
	err := h.call("script.add", map[string]any{"group_id": "g1", "name": "broken", "js": "let a = 1;\nlet b = ;"}, nil)
	if code(err) != -32602 || !strings.Contains(err.Error(), "line 2") {
		t.Fatalf("a script that does not parse: %v", err)
	}
	// An exception in a script is told under its name.
	h.must("script.add", map[string]any{"group_id": "g1", "name": "thrower", "js": "throw new Error('boom from a script')"}, nil)
	h.reload(o.Tab.ID)
	h.waitLogs(o.Tab.ID, "page script thrower threw")
	// Removed, it no longer runs; the profile keeps the rest.
	var removed struct {
		Removed []string         `json:"removed"`
		Scripts []map[string]any `json:"scripts"`
	}
	h.must("script.remove", map[string]any{"group_id": "g1", "name": "paint"}, &removed)
	if len(removed.Removed) != 1 || len(removed.Scripts) != 2 {
		t.Fatalf("removed: %+v", removed)
	}
	h.reload(o.Tab.ID)
	h.evalUntil(o.Tab.ID, "window.seen ?? 'gone'", "gone")
	// Kept in the profile: a browser started again on it has them.
	var list struct {
		Browsers []map[string]any `json:"browsers"`
	}
	h.must("browser.list", nil, &list)
	h.must("browser.close", map[string]any{"browser_id": list.Browsers[0]["id"]}, nil)
	h.must("browser.open", map[string]any{"group_id": "g2", "profile": "p1", "url": h.site.URL + "/still"}, &o)
	var kept struct {
		Scripts []map[string]any `json:"scripts"`
	}
	h.must("script.list", map[string]any{"group_id": "g2"}, &kept)
	if len(kept.Scripts) != 2 {
		t.Fatalf("the scripts after the browser started again: %+v", kept)
	}
	h.must("script.remove", map[string]any{"group_id": "g2", "all": true}, nil)
	h.must("script.list", map[string]any{"group_id": "g2"}, &kept)
	if len(kept.Scripts) != 0 {
		t.Fatalf("all removed: %+v", kept)
	}
}

// extensionZip is a small extension: a content script that marks the page at document start and a
// service worker that logs, both saying which version they are.
func extensionZip(t *testing.T, version string) string {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	files := map[string]string{
		// Inside a folder of its own, as zipping a folder makes it.
		"probe/manifest.json": `{"manifest_version": 3, "name": "Probe", "version": "` + version + `",
			"background": {"service_worker": "sw.js"},
			"content_scripts": [{"matches": ["<all_urls>"], "js": ["cs.js"], "run_at": "document_start"}]}`,
		"probe/sw.js": `console.log("worker up ` + version + `"); console.error("worker complains ` + version + `");`,
		"probe/cs.js": `document.documentElement.dataset.ext = "` + version + `";`,
	}
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = w.Write([]byte(body))
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(buf.Bytes())
}

func (h *harness) installExtension(group, data string) browser.Extension {
	h.t.Helper()
	var up struct {
		UploadID string `json:"upload_id"`
	}
	h.must("upload.put", map[string]any{"group_id": group, "name": "probe.zip", "offset": 0, "data_b64": data}, &up)
	var r struct {
		Extension browser.Extension `json:"extension"`
	}
	h.must("extension.install", map[string]any{"group_id": group, "upload_id": up.UploadID, "source": "/work/probe"}, &r)
	return r.Extension
}

func TestExtensionsInstallReloadAndRemove(t *testing.T) {
	launchOnly(t, "the operator's Chrome takes no extension from the agent")
	h := start(t, nil)
	o := h.open("g1", "p1", "/still")
	e := h.installExtension("g1", extensionZip(t, "1.0"))
	if e.Name != "Probe" || e.Version != "1.0" || !e.Enabled || e.Dir != "probe" {
		t.Fatalf("installed: %+v", e)
	}
	// The id is foreseen from the folder, which is how the worker is followed from its first line.
	dir := filepath.Join(h.manager.ProfileDir("p1"), "browserd", "extensions", "probe")
	if e.ID != browser.ExtensionID(dir) {
		t.Fatalf("the id %s is not the one foreseen for %s (%s)", e.ID, dir, browser.ExtensionID(dir))
	}
	h.reload(o.Tab.ID)
	h.evalUntil(o.Tab.ID, "document.documentElement.dataset.ext", "1.0")
	logs := func(want string) {
		t.Helper()
		deadline := time.Now().Add(10 * time.Second)
		for {
			var r struct {
				Logs page.LogsResult `json:"logs"`
			}
			h.must("extension.logs", map[string]any{"group_id": "g1", "id": "Probe", "level": "debug"}, &r)
			for _, l := range r.Logs.Entries {
				if strings.Contains(l.Text, want) {
					return
				}
			}
			if time.Now().After(deadline) {
				t.Fatalf("the worker's console has no %q: %+v", want, r.Logs.Entries)
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	logs("worker up 1.0")
	logs("worker complains 1.0")
	// The loop: the same source again replaces the files and reloads it, keeping its id.
	again := h.installExtension("g1", extensionZip(t, "2.0"))
	if again.ID != e.ID || again.Version != "2.0" {
		t.Fatalf("reinstalled: %+v (was %+v)", again, e)
	}
	h.reload(o.Tab.ID)
	h.evalUntil(o.Tab.ID, "document.documentElement.dataset.ext", "2.0")
	logs("worker up 2.0")
	var list struct {
		Extensions []browser.Extension `json:"extensions"`
	}
	h.must("extension.list", map[string]any{"group_id": "g1"}, &list)
	if len(list.Extensions) != 1 || list.Extensions[0].Version != "2.0" || !list.Extensions[0].Enabled || list.Extensions[0].Source != "/work/probe" {
		t.Fatalf("listed: %+v", list)
	}
	// Kept in the profile: the browser started again loads it before its first page.
	var browsers struct {
		Browsers []map[string]any `json:"browsers"`
	}
	h.must("browser.list", nil, &browsers)
	h.must("browser.close", map[string]any{"browser_id": browsers.Browsers[0]["id"]}, nil)
	o = h.open("g2", "p1", "/still")
	h.evalUntil(o.Tab.ID, "document.documentElement.dataset.ext", "2.0")
	h.must("extension.list", map[string]any{"group_id": "g2"}, &list)
	if len(list.Extensions) != 1 || !list.Extensions[0].Enabled || list.Extensions[0].Error != "" {
		t.Fatalf("listed after the restart: %+v", list)
	}
	var gone struct {
		Extension browser.Extension `json:"extension"`
	}
	h.must("extension.uninstall", map[string]any{"group_id": "g2", "id": e.ID}, &gone)
	h.reload(o.Tab.ID)
	h.evalUntil(o.Tab.ID, "document.documentElement.dataset.ext ?? 'none'", "none")
	h.must("extension.list", map[string]any{"group_id": "g2"}, &list)
	if len(list.Extensions) != 0 {
		t.Fatalf("listed after the removal: %+v", list)
	}
	// What is not an extension is refused before anything is unpacked.
	var up struct {
		UploadID string `json:"upload_id"`
	}
	h.must("upload.put", map[string]any{"group_id": "g2", "name": "x.zip", "offset": 0, "data_b64": base64.StdEncoding.EncodeToString([]byte("not a zip"))}, &up)
	if err := h.call("extension.install", map[string]any{"group_id": "g2", "upload_id": up.UploadID}, nil); code(err) != -32602 {
		t.Fatalf("not an archive: %v", err)
	}
	// A throwaway browser takes none.
	h.must("browser.open", map[string]any{"group_id": "g3", "profile": browser.EphemeralProfile}, nil)
	h.must("upload.put", map[string]any{"group_id": "g3", "name": "probe.zip", "offset": 0, "data_b64": extensionZip(t, "1.0")}, &up)
	if err := h.call("extension.install", map[string]any{"group_id": "g3", "upload_id": up.UploadID}, nil); code(err) != 1007 {
		raw, _ := json.Marshal(err)
		t.Fatalf("an extension in a throwaway browser: %v %s", err, raw)
	}
}
