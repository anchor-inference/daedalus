package browser

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"mime"
	"net/url"
	"path"
	"strconv"
	"strings"
	"time"
)

// PDF documents in the operator's Chrome. A top-level navigation of an agent tab to a PDF opens
// Chrome's PDF viewer, which is another extension's page: chrome.debugger lets go of the tab, every
// later call to it fails, and the agent never gets the file. So the tab's document responses are
// paused (fetchState.pdf), and a PDF is taken out of the response, kept as a finished download of
// the tab's group, and its navigation dropped: the tab stays on the page it was on and keeps
// answering, and the agent reads the file with download.read.

// Kept is a file kept for a tab out of a response, as a navigation's or an action's result names it.
type Kept struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Size int64  `json:"size"`
	Mime string `json:"mime"`
}

// FileKeeper is a Listener that also keeps files as a group's downloads: the page model, which owns
// the downloads. The manager does not import it, so it finds the keeper among its listeners.
type FileKeeper interface {
	KeepFile(t *Tab, name, url, mime string, data []byte) (Kept, error)
}

// pdfRelayBytes bounds a PDF taken out of a response. Its body comes back from the extension as
// base64 in one relay frame, at most relay.MaxFromExtension (64 MiB) of JSON, and base64 grows by a
// third: 32 MiB stays well under. A larger one is left to Chrome when its Content-Length says so up
// front; one of no stated length is read as a stream and given up past the limit (streamBody).
const pdfRelayBytes = 32 << 20

func (m *Manager) keeper() FileKeeper {
	for _, l := range m.listeners {
		if k, ok := l.(FileKeeper); ok {
			return k
		}
	}
	return nil
}

// pdfChunkBytes is how much of a streamed PDF one IO.read asks for: one relay frame each, well
// under the frame's limit once in base64.
const pdfChunkBytes = 1 << 20

// errPDFTooLarge is a streamed PDF that went past the limit.
var errPDFTooLarge = errors.New("the PDF is larger than can be kept")

// streamBody reads a paused response's body as a stream, a chunk at a time, and stops past limit. A
// response with no Content-Length (one sent in chunks, a generated report) used to be read whole
// with Fetch.getResponseBody and measured afterwards: a very large one crossed the extension as one
// base64 string and failed on the relay's frame limit, after holding all of it in the service
// worker. taken says the body was taken from Chrome, after which the request can only be failed or
// fulfilled, never continued.
func streamBody(ctx context.Context, t *Tab, requestID string, limit int64) ([]byte, bool, error) {
	var stream struct {
		Stream string `json:"stream"`
	}
	if err := t.Call(ctx, "Fetch.takeResponseBodyAsStream", map[string]any{"requestId": requestID}, &stream); err != nil {
		return nil, false, err
	}
	defer func() { _ = t.Call(ctx, "IO.close", map[string]any{"handle": stream.Stream}, nil) }()
	var data []byte
	for {
		var chunk struct {
			Data          string `json:"data"`
			Base64Encoded bool   `json:"base64Encoded"`
			EOF           bool   `json:"eof"`
		}
		if err := t.Call(ctx, "IO.read", map[string]any{"handle": stream.Stream, "size": pdfChunkBytes}, &chunk); err != nil {
			return nil, true, err
		}
		piece := []byte(chunk.Data)
		if chunk.Base64Encoded {
			var err error
			if piece, err = base64.StdEncoding.DecodeString(chunk.Data); err != nil {
				return nil, true, err
			}
		}
		if int64(len(data)+len(piece)) > limit {
			return nil, true, errPDFTooLarge
		}
		data = append(data, piece...)
		if chunk.EOF {
			return data, true, nil
		}
	}
}

// pausedResponse is a Fetch.requestPaused at the response stage.
type pausedResponse struct {
	RequestID string `json:"requestId"`
	FrameID   string `json:"frameId"`
	Request   struct {
		URL string `json:"url"`
	} `json:"request"`
	ResponseStatusCode  *int   `json:"responseStatusCode"`
	ResponseErrorReason string `json:"responseErrorReason"`
	ResponseHeaders     []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"responseHeaders"`
}

func (p *pausedResponse) header(name string) string {
	for _, h := range p.ResponseHeaders {
		if strings.EqualFold(h.Name, name) {
			return h.Value
		}
	}
	return ""
}

// atResponse reports whether a paused request is paused at its response, which only the PDF
// pattern asks for; the guard's pauses are at the request.
func atResponse(raw json.RawMessage) bool {
	var p struct {
		ResponseStatusCode  *int   `json:"responseStatusCode"`
		ResponseErrorReason string `json:"responseErrorReason"`
	}
	return json.Unmarshal(raw, &p) == nil && (p.ResponseStatusCode != nil || p.ResponseErrorReason != "")
}

// isPDF reports whether a Content-Type is a PDF's.
func isPDF(contentType string) bool {
	mt, _, err := mime.ParseMediaType(contentType)
	if err != nil {
		mt = strings.ToLower(strings.TrimSpace(contentType))
	}
	return mt == "application/pdf" || mt == "application/x-pdf"
}

// pdfName is the file's name: the one Content-Disposition gives, else the address's last part, else
// document.pdf. The page model makes it a plain name.
func pdfName(disposition, raw string) string {
	if _, params, err := mime.ParseMediaType(disposition); err == nil && strings.TrimSpace(params["filename"]) != "" {
		return params["filename"]
	}
	if u, err := url.Parse(raw); err == nil {
		base := path.Base(u.Path)
		if unescaped, err := url.PathUnescape(base); err == nil {
			base = unescaped
		}
		if base != "" && base != "." && base != "/" {
			if path.Ext(base) == "" {
				base += ".pdf"
			}
			return base
		}
	}
	return "document.pdf"
}

// responsePaused answers one paused document response. Anything but the tab's own main-frame
// document arriving whole as a PDF goes on at once; that one is kept as a file and its navigation
// aborted. Whatever goes wrong on the way lets the response go on to Chrome, which is where it went
// before: a PDF viewer the tab loses its debugger to (and says so, Tab.Detached) is better than a
// navigation that hangs.
func (m *Manager) responsePaused(t *Tab, raw json.RawMessage) {
	var p pausedResponse
	if json.Unmarshal(raw, &p) != nil || p.RequestID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	carryOn := func() {
		_ = t.Call(ctx, "Fetch.continueRequest", map[string]any{"requestId": p.RequestID}, nil)
	}
	status := 0
	if p.ResponseStatusCode != nil {
		status = *p.ResponseStatusCode
	}
	if p.FrameID != t.TargetID || p.ResponseErrorReason != "" || status < 200 || status > 299 || !isPDF(p.header("Content-Type")) {
		carryOn()
		return
	}
	// Only while the agent drives: a PDF the operator opens while control is theirs (their own input
	// in the tab, the live view, a pause) is theirs to read in Chrome's viewer, and taking it away
	// left them with an aborted navigation and the file in the agent's downloads.
	if t.Given || t.Group.Control().Owner != OwnerAgent {
		carryOn()
		return
	}
	limit := int64(pdfRelayBytes)
	if most := m.Limits().MaxDownloadBytes; most > 0 && most < limit {
		limit = most
	}
	n, lengthErr := strconv.ParseInt(strings.TrimSpace(p.header("Content-Length")), 10, 64)
	sized := lengthErr == nil
	if sized && n > limit {
		m.log.Info("a PDF too large to keep is left to Chrome", "tab", t.ID, "bytes", n)
		carryOn()
		return
	}
	k := m.keeper()
	if k == nil {
		carryOn()
		return
	}
	t.beginKeeping()
	defer t.endKeeping()
	var data []byte
	if sized {
		var body struct {
			Body          string `json:"body"`
			Base64Encoded bool   `json:"base64Encoded"`
		}
		if err := t.Call(ctx, "Fetch.getResponseBody", map[string]any{"requestId": p.RequestID}, &body); err != nil {
			m.log.Warn("reading a PDF response", "tab", t.ID, "error", err.Error())
			carryOn()
			return
		}
		data = []byte(body.Body)
		if body.Base64Encoded {
			var err error
			if data, err = base64.StdEncoding.DecodeString(body.Body); err != nil {
				carryOn()
				return
			}
		}
		if int64(len(data)) > limit {
			carryOn()
			return
		}
	} else {
		var taken bool
		var err error
		data, taken, err = streamBody(ctx, t, p.RequestID, limit)
		if err != nil {
			if !taken {
				m.log.Warn("reading a PDF response", "tab", t.ID, "error", err.Error())
				carryOn()
				return
			}
			// A body taken as a stream cannot be handed back to Chrome, so past the limit, or broken
			// off, the navigation fails rather than hangs.
			m.log.Info("a PDF of no stated length could not be kept", "tab", t.ID, "error", err.Error())
			_ = t.Call(ctx, "Fetch.failRequest", map[string]any{"requestId": p.RequestID, "errorReason": "Aborted"}, nil)
			return
		}
	}
	got, err := k.KeepFile(t, pdfName(p.header("Content-Disposition"), p.Request.URL), p.Request.URL, "application/pdf", data)
	if err != nil {
		m.log.Warn("keeping a PDF", "tab", t.ID, "error", err.Error())
		carryOn()
		return
	}
	// Recorded before the navigation is aborted: Page.navigate answers once it is, and reads it.
	t.recordKept(got)
	_ = t.Call(ctx, "Fetch.failRequest", map[string]any{"requestId": p.RequestID, "errorReason": "Aborted"}, nil)
	m.log.Info("a PDF was kept as a download instead of opening in the tab", "group", t.Group.ID, "tab", t.ID, "download", got.ID, "bytes", got.Size)
}
