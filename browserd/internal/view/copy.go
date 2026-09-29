package view

import (
	"context"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// MaxCopyText bounds the text one copy carries, in characters. The answer rides in one EVENT, and
// the socket framing holds a megabyte; a quarter of a million characters stays inside it for any
// text but one made of control characters, which copiedEvent trims further.
const MaxCopyText = 256 << 10

// maxCopyID bounds a copy's id, which the client makes up and gets back unchanged.
const maxCopyID = 64

// copySelection answers a person's copy: the page's selected text goes back to this client alone as
// a "copied" event, never to the other viewers, the event log or the daemon's log. The copy runs in
// the client's input order, so a cut sent after it (the key that deletes the text) finds the
// selection still there.
func (cl *Client) copySelection(ctx context.Context, t *browser.Tab, id string) {
	if id == "" || len(id) > maxCopyID {
		cl.event(map[string]any{"type": "error", "code": "input", "message": "a copy needs an id of 1-64 bytes"})
		return
	}
	if cl.hub.Selection == nil {
		cl.event(map[string]any{"type": "copied", "id": id, "text": "", "error": "copying is not available"})
		return
	}
	text, truncated, withheld, err := cl.hub.Selection(ctx, t, MaxCopyText)
	if err != nil {
		cl.event(map[string]any{"type": "copied", "id": id, "text": "", "error": err.Error()})
		return
	}
	cl.sendRaw(copiedEvent(id, text, truncated, withheld))
}

// copiedEvent encodes the answer, trimming the text until the event fits one socket frame: JSON
// writes a control character as six bytes, so a text within MaxCopyText can still be too long.
func copiedEvent(id, text string, truncated, withheld bool) []byte {
	for {
		b, err := wire.EncodeEvent(copied{Type: "copied", ID: id, Text: text, Truncated: truncated, Withheld: withheld})
		if err == nil && len(b) <= protowire.MaxPayload {
			return b
		}
		cut := len(text) / 2
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
		text, truncated = text[:cut], true
	}
}

// copied is the answer in the order the specification lists its keys, as the golden frame holds it.
type copied struct {
	Type      string `json:"type"`
	ID        string `json:"id"`
	Text      string `json:"text"`
	Truncated bool   `json:"truncated"`
	Withheld  bool   `json:"withheld"`
}

// sendRaw sends an already encoded event, as event does, only while the client is attached.
func (cl *Client) sendRaw(b []byte) {
	cl.mu.Lock()
	attached := cl.tier != "" && !cl.ended
	cl.mu.Unlock()
	if attached {
		cl.send(b)
	}
}
