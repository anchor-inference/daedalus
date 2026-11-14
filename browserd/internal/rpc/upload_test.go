//go:build unix

package rpc_test

import (
	"encoding/base64"
	"errors"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/browserd/internal/page"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// put hands the daemon one file for the group, as the host does before an upload.
func (h *harness) put(group, name, content string) string {
	h.t.Helper()
	var up struct {
		UploadID string `json:"upload_id"`
	}
	h.must("upload.put", map[string]any{"group_id": group, "name": name, "offset": 0, "data_b64": base64.StdEncoding.EncodeToString([]byte(content))}, &up)
	return up.UploadID
}

// errData is the data of the daemon's error, as the client decoded it.
func errData(err error) map[string]any {
	var we *protowire.Error
	if errors.As(err, &we) {
		if m, ok := we.Data.(map[string]any); ok {
			return m
		}
	}
	return nil
}

// The form an upload once failed on, four Allows in: a React-style widget whose input is hidden (0×0,
// visibility: hidden) behind a button of the page's own, read by a listener on the document that
// empties the field after. Named by the button, the files reach the page; named by the hidden input,
// they reach it too; and the tool says which input took them.
func TestUploadToAHiddenInputBehindAButton(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/upload.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	button := refOf(t, s, "button", "Attach")
	first, second := h.put("g1", "first.pdf", "one"), h.put("g1", "second.png", "three")

	// The dry run the host asks before its question resolves the button to the input and is not
	// refused for the input's size.
	dry := h.mustAct(tab, map[string]any{"action": "upload", "ref": button, "element": "Attach", "dry_run": true})
	if dry.Element == nil || !dry.Element.File || dry.Ref == "" || dry.Ref == button {
		t.Fatalf("the dry run did not find the input behind the button: %+v ref %q", dry.Element, dry.Ref)
	}

	r := h.mustAct(tab, map[string]any{"action": "upload", "ref": button, "element": "Attach", "upload_ids": []string{first, second}})
	up, _ := r.Effects["uploaded"].(map[string]any)
	if up == nil || up["how"] != "near" || up["cleared"] != true {
		t.Fatalf("what the upload says it did: %+v", r.Effects)
	}
	text := h.snapshot(tab).Text
	if strings.Count(text, "attached first.pdf 3") != 1 || strings.Count(text, "attached second.png 5") != 1 {
		// Once each: told twice, a widget uploads every file twice.
		t.Fatalf("the page did not get the files, once each:\n%s", text)
	}

	// Straight to the hidden input, by the ref an inspection gives it.
	var found map[string]any
	h.must("page.inspect", map[string]any{"tab_id": tab, "selector": "#files"}, &found)
	input, _ := found["ref"].(string)
	if input == "" {
		t.Fatalf("no ref for the hidden input: %v", found)
	}
	third := h.put("g1", "third.txt", "seven..")
	r = h.mustAct(tab, map[string]any{"action": "upload", "ref": input, "element": "the hidden input", "upload_ids": []string{third}})
	if up, _ := r.Effects["uploaded"].(map[string]any); up == nil || up["how"] != "itself" {
		t.Fatalf("an upload to the hidden input: %+v", r.Effects)
	}
	if text := h.snapshot(tab).Text; !strings.Contains(text, "attached third.txt 7") {
		t.Fatalf("the page did not get the file from the hidden input:\n%s", text)
	}

	// A file larger than one piece of what crosses into the operator's Chrome arrives whole.
	big := strings.Repeat("0123456789abcdef", 90000)
	var stored struct {
		UploadID string `json:"upload_id"`
	}
	for off := 0; off < len(big); off += page.ChunkBytes {
		end := min(off+page.ChunkBytes, len(big))
		params := map[string]any{"group_id": "g1", "name": "big.bin", "offset": off, "data_b64": base64.StdEncoding.EncodeToString([]byte(big[off:end]))}
		if stored.UploadID != "" {
			params["upload_id"] = stored.UploadID
		}
		h.must("upload.put", params, &stored)
	}
	h.mustAct(tab, map[string]any{"action": "upload", "ref": button, "element": "Attach", "upload_ids": []string{stored.UploadID}})
	if text := h.snapshot(tab).Text; !strings.Contains(text, "attached big.bin 1440000") {
		t.Fatalf("a large file did not arrive whole:\n%s", text)
	}
}

// A button whose input the page makes on the fly and never puts in the document: the click opens
// the chooser, the daemon intercepts it and the files go to the input the browser names.
func TestUploadThroughTheChooserAButtonOpens(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/upload.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	id := h.put("g1", "photo.jpg", "jpeg!")
	r := h.mustAct(tab, map[string]any{"action": "upload", "ref": refOf(t, s, "button", "Add a picture"), "element": "Add a picture", "upload_ids": []string{id}})
	if up, _ := r.Effects["uploaded"].(map[string]any); up == nil || up["how"] != "chooser" {
		t.Fatalf("through the chooser: %+v", r.Effects)
	}
	if text := h.snapshot(tab).Text; !strings.Contains(text, "picked photo.jpg 5") {
		t.Fatalf("the page did not get the file from its chooser:\n%s", text)
	}
}

// An upload whose files do not arrive is an error that says nothing was done, never "done"; and so
// is a button that opens no chooser.
func TestAnUploadThatDoesNotAttachIsAnError(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/upload.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	id := h.put("g1", "lost.txt", "gone")
	var r page.ActResult
	err := h.call("page.act", map[string]any{"tab_id": tab, "action": "upload", "ref": refOf(t, s, "button", "Sink"), "element": "Sink", "upload_ids": []string{id}}, &r)
	if err == nil || !strings.Contains(err.Error(), "did not attach") {
		t.Fatalf("a swallowed upload: %v %+v", err, r.Effects)
	}
	if data := errData(err); data["not_done"] != true || data["reason"] != "not_attached" {
		t.Fatalf("the refusal's data: %v", data)
	}
	err = h.call("page.act", map[string]any{"tab_id": tab, "action": "upload", "ref": refOf(t, s, "button", "Not an upload"), "element": "plain", "upload_ids": []string{id}}, &r)
	if err == nil || !strings.Contains(err.Error(), "opened no file chooser") || errData(err)["not_done"] != true {
		t.Fatalf("a button that opens no chooser: %v", err)
	}
}
