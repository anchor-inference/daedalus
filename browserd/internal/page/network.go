package page

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"math"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The page's requests as the agent reads them: what a page asked its servers for, and what came
// back, so an agent can find the JSON a page is built from, or why its own site answers 500. It is a
// reading of what the page did through the network wall, never a way to send anything. Each tab
// keeps its last requestsKept; headers are kept with their secrets cut (redact.go), and a response
// body is read from Chromium only when asked for one request, bounded, and cut the same way.
const (
	requestsKept = 300
	headersKept  = 50
	headerMax    = 500
	// NetworkMax is the most requests one page.network lists.
	NetworkMax = 200
	// BodyMax is the most characters of a response body one page.request returns.
	BodyMax = 200_000
)

// Request is one request of a tab: a redirect's every hop is one of its own.
type Request struct {
	ID        string `json:"id"`
	Seq       int64  `json:"seq"`
	Method    string `json:"method"`
	URL       string `json:"url"`
	Type      string `json:"type"`
	Status    int    `json:"status,omitempty"`
	MIME      string `json:"mime,omitempty"`
	At        int64  `json:"at"`
	MS        int64  `json:"ms,omitempty"`
	Size      int64  `json:"size,omitempty"`      // bytes that came over the network, headers included
	BodySize  int64  `json:"body_size,omitempty"` // the response body, decoded
	Cached    bool   `json:"cached,omitempty"`
	Pending   bool   `json:"pending,omitempty"`
	Failed    string `json:"failed,omitempty"`
	Blocked   string `json:"blocked,omitempty"` // the network wall's reason, when it refused the request
	Initiator string `json:"initiator,omitempty"`
	Redirect  string `json:"redirect,omitempty"` // where a redirect's hop sent the page

	// The detail page.request gives: headers with their secrets cut, whether the request sent a body
	// (never the body itself: a sign-in sends its password in one).
	RequestHeaders  []Header `json:"request_headers,omitempty"`
	ResponseHeaders []Header `json:"response_headers,omitempty"`
	PostData        bool     `json:"post_data,omitempty"`
	StatusText      string   `json:"status_text,omitempty"`
	Protocol        string   `json:"protocol,omitempty"`
	Frame           bool     `json:"frame,omitempty"` // made by a frame of the page, not the page itself

	requestID string
	started   float64 // Chromium's monotonic seconds
}

// Header is one header as the agent may read it.
type Header struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type networkKey struct{}

// requestLog is one tab's requests.
type requestLog struct {
	mu      sync.Mutex
	entries []*Request
	byID    map[string]*Request // Chromium's request id -> its current hop
	seq     int64
	lost    int64
	mainDoc *Request // the page's own document, the newest
}

func (p *Model) requestsOf(t *browser.Tab, create bool) *requestLog {
	if l, ok := t.Value(networkKey{}).(*requestLog); ok {
		return l
	}
	if !create {
		return nil
	}
	l := &requestLog{byID: map[string]*Request{}}
	t.SetValue(networkKey{}, l)
	return l
}

func (l *requestLog) addLocked(r *Request) {
	l.seq++
	r.Seq = l.seq
	r.ID = "r" + strconv.FormatInt(l.seq, 10)
	l.entries = append(l.entries, r)
	if len(l.entries) > requestsKept {
		drop := l.entries[:len(l.entries)-requestsKept]
		for _, old := range drop {
			if l.byID[old.requestID] == old {
				delete(l.byID, old.requestID)
			}
			l.lost = old.Seq
		}
		l.entries = append([]*Request(nil), l.entries[len(l.entries)-requestsKept:]...)
	}
}

// networkEvent follows the tab's requests from Chromium's Network events.
func (p *Model) networkEvent(t *browser.Tab, e cdp.Event) {
	switch e.Method {
	case "Network.requestWillBeSent":
		var r struct {
			RequestID string  `json:"requestId"`
			FrameID   string  `json:"frameId"`
			Type      string  `json:"type"`
			Timestamp float64 `json:"timestamp"`
			WallTime  float64 `json:"wallTime"`
			Request   struct {
				URL         string            `json:"url"`
				Method      string            `json:"method"`
				Headers     map[string]string `json:"headers"`
				HasPostData bool              `json:"hasPostData"`
			} `json:"request"`
			Initiator struct {
				Type       string      `json:"type"`
				URL        string      `json:"url"`
				LineNumber *int        `json:"lineNumber"`
				Stack      *stackTrace `json:"stack"`
			} `json:"initiator"`
			RedirectResponse *response `json:"redirectResponse"`
		}
		if json.Unmarshal(e.Params, &r) != nil || strings.HasPrefix(r.Request.URL, "data:") {
			return
		}
		if r.Initiator.Type == "other" && r.Type == "Other" && strings.HasSuffix(strings.SplitN(r.Request.URL, "?", 2)[0], "/favicon.ico") {
			// The browser's own request for the site's icon, which the page never made (logs.go).
			return
		}
		l := p.requestsOf(t, true)
		l.mu.Lock()
		defer l.mu.Unlock()
		if prev := l.byID[r.RequestID]; prev != nil && r.RedirectResponse != nil {
			// The same request id goes on to the next hop: the one before ends here, with its answer.
			prev.answer(r.RedirectResponse)
			prev.Redirect = RedactURL(r.Request.URL)
			prev.finish(r.Timestamp, 0)
		}
		q := &Request{requestID: r.RequestID, Method: r.Request.Method, URL: RedactURL(r.Request.URL), Type: strings.ToLower(orDefault(r.Type, "other")),
			At: wallMS(r.WallTime), started: r.Timestamp, Pending: true, PostData: r.Request.HasPostData, Frame: r.FrameID != "" && r.FrameID != t.TargetID,
			RequestHeaders: headers(r.Request.Headers)}
		switch in := r.Initiator; in.Type {
		case "script":
			if f := in.Stack.first(); f != nil {
				q.Initiator = "script " + f.URL + ":" + strconv.Itoa(f.LineNumber+1)
			} else {
				q.Initiator = "script"
			}
		case "parser":
			q.Initiator = "parser"
			if in.URL != "" {
				q.Initiator += " " + RedactURL(in.URL)
			}
		case "":
		default:
			q.Initiator = in.Type
		}
		l.addLocked(q)
		l.byID[r.RequestID] = q
		if q.Type == "document" && !q.Frame {
			l.mainDoc = q
		}
	case "Network.responseReceived":
		var r struct {
			RequestID string   `json:"requestId"`
			Type      string   `json:"type"`
			Response  response `json:"response"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		l := p.requestsOf(t, false)
		if l == nil {
			return
		}
		l.mu.Lock()
		if q := l.byID[r.RequestID]; q != nil {
			q.answer(&r.Response)
		}
		l.mu.Unlock()
	case "Network.dataReceived":
		var r struct {
			RequestID  string `json:"requestId"`
			DataLength int64  `json:"dataLength"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if l := p.requestsOf(t, false); l != nil {
			l.mu.Lock()
			if q := l.byID[r.RequestID]; q != nil {
				q.BodySize += r.DataLength
			}
			l.mu.Unlock()
		}
	case "Network.loadingFinished":
		var r struct {
			RequestID         string  `json:"requestId"`
			Timestamp         float64 `json:"timestamp"`
			EncodedDataLength float64 `json:"encodedDataLength"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if l := p.requestsOf(t, false); l != nil {
			l.mu.Lock()
			if q := l.byID[r.RequestID]; q != nil {
				q.finish(r.Timestamp, int64(r.EncodedDataLength))
			}
			l.mu.Unlock()
		}
	case "Network.loadingFailed":
		var r struct {
			RequestID     string  `json:"requestId"`
			Timestamp     float64 `json:"timestamp"`
			ErrorText     string  `json:"errorText"`
			Canceled      bool    `json:"canceled"`
			BlockedReason string  `json:"blockedReason"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if l := p.requestsOf(t, false); l != nil {
			l.mu.Lock()
			if q := l.byID[r.RequestID]; q != nil {
				q.Failed = orDefault(r.ErrorText, "failed")
				if r.Canceled {
					q.Failed = "canceled"
				}
				if r.BlockedReason != "" {
					q.Failed += " (" + r.BlockedReason + ")"
				}
				q.finish(r.Timestamp, 0)
			}
			l.mu.Unlock()
		}
	case "Network.requestWillBeSentExtraInfo", "Network.responseReceivedExtraInfo":
		// The headers as they went over the wire, which the events above leave out: the cookies a
		// request carried and the ones its answer set. Only that they were there is kept.
		var r struct {
			RequestID string            `json:"requestId"`
			Headers   map[string]string `json:"headers"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if l := p.requestsOf(t, false); l != nil {
			l.mu.Lock()
			if q := l.byID[r.RequestID]; q != nil {
				if e.Method == "Network.requestWillBeSentExtraInfo" {
					q.RequestHeaders = mergeHeaders(q.RequestHeaders, r.Headers)
				} else {
					q.ResponseHeaders = mergeHeaders(q.ResponseHeaders, r.Headers)
				}
			}
			l.mu.Unlock()
		}
	case "Network.requestServedFromCache":
		var r struct {
			RequestID string `json:"requestId"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if l := p.requestsOf(t, false); l != nil {
			l.mu.Lock()
			if q := l.byID[r.RequestID]; q != nil {
				q.Cached = true
			}
			l.mu.Unlock()
		}
	}
}

type response struct {
	URL               string            `json:"url"`
	Status            int               `json:"status"`
	StatusText        string            `json:"statusText"`
	Headers           map[string]string `json:"headers"`
	MimeType          string            `json:"mimeType"`
	FromDiskCache     bool              `json:"fromDiskCache"`
	FromServiceWorker bool              `json:"fromServiceWorker"`
	FromPrefetchCache bool              `json:"fromPrefetchCache"`
	EncodedDataLength float64           `json:"encodedDataLength"`
	Protocol          string            `json:"protocol"`
}

func (q *Request) answer(r *response) {
	q.Status, q.StatusText, q.MIME, q.Protocol = r.Status, r.StatusText, r.MimeType, r.Protocol
	q.Cached = q.Cached || r.FromDiskCache || r.FromPrefetchCache
	// The wire's own headers may have come first (responseReceivedExtraInfo): kept beside these.
	q.ResponseHeaders = keepHeaders(headers(r.Headers), q.ResponseHeaders)
	for k, v := range r.Headers {
		if strings.EqualFold(k, "x-browserd-blocked") {
			// The daemon's own wall answered, not the site: said as such, not as the site's 403.
			q.Blocked = v
		}
	}
}

func (q *Request) finish(ts float64, encoded int64) {
	q.Pending = false
	if ts > 0 && q.started > 0 {
		q.MS = int64(math.Round((ts - q.started) * 1000))
	}
	if encoded > 0 {
		q.Size = encoded
	}
}

// headers are a request's or response's headers as the agent may read them: sorted, bounded, and
// every credential cut.
func headers(h map[string]string) []Header {
	if len(h) == 0 {
		return nil
	}
	names := make([]string, 0, len(h))
	for k := range h {
		names = append(names, k)
	}
	sort.Strings(names)
	out := make([]Header, 0, min(len(names), headersKept))
	for _, k := range names {
		if len(out) == headersKept {
			break
		}
		out = append(out, Header{Name: strings.ToLower(k), Value: clipText(RedactHeader(k, h[k]), headerMax)})
	}
	return out
}

// mergeHeaders adds to have the headers of more it does not hold, their secrets cut as headers's.
func mergeHeaders(have []Header, more map[string]string) []Header {
	extra := map[string]string{}
	for k, v := range more {
		if !strings.HasPrefix(k, ":") { // HTTP/2's pseudo-headers say again what the entry says
			extra[k] = v
		}
	}
	return keepHeaders(have, headers(extra))
}

// keepHeaders is have with the headers of more it does not name, sorted and bounded.
func keepHeaders(have, more []Header) []Header {
	known := map[string]bool{}
	for _, h := range have {
		known[h.Name] = true
	}
	out := append([]Header(nil), have...)
	for _, h := range more {
		if !known[h.Name] {
			known[h.Name] = true
			out = append(out, h)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if len(out) > headersKept {
		out = out[:headersKept]
	}
	return out
}

func wallMS(s float64) int64 {
	if s <= 0 {
		return time.Now().UnixMilli()
	}
	return int64(s * 1000)
}

// NetworkFilter narrows page.network.
type NetworkFilter struct {
	Types    []string // Chromium's resource types, lower case; "api" is xhr, fetch, websocket and eventsource
	Host     string   // the host, or a domain the host is under
	Method   string
	Contains string // part of the address
	Failed   bool   // only what failed, or answered 400 or above
}

// NetworkResult is page.network's answer.
type NetworkResult struct {
	URL      string     `json:"url"`
	Requests []*Request `json:"requests"`
	// Last is where to read after next time: the newest request considered.
	Last int64 `json:"last"`
	// Skipped counts the requests that matched but were left out for limit: the oldest of them.
	Skipped int  `json:"skipped"`
	Dropped bool `json:"dropped"`
	Total   int  `json:"total"` // the requests the tab keeps
}

var apiTypes = map[string]bool{"xhr": true, "fetch": true, "websocket": true, "eventsource": true}

func (f NetworkFilter) match(q *Request) bool {
	if len(f.Types) > 0 {
		ok := false
		for _, ty := range f.Types {
			if ty == q.Type || ty == "api" && apiTypes[q.Type] {
				ok = true
			}
		}
		if !ok {
			return false
		}
	}
	if f.Method != "" && !strings.EqualFold(f.Method, q.Method) {
		return false
	}
	if f.Host != "" {
		u, err := url.Parse(q.URL)
		h := strings.ToLower(strings.TrimPrefix(f.Host, "."))
		if err != nil || (u.Hostname() != h && !strings.HasSuffix(u.Hostname(), "."+h)) {
			return false
		}
	}
	if f.Contains != "" && !strings.Contains(strings.ToLower(q.URL), strings.ToLower(f.Contains)) {
		return false
	}
	if f.Failed && q.Failed == "" && q.Status < 400 && q.Blocked == "" {
		return false
	}
	return true
}

// summary is a request as the list gives it: no headers, which page.request has.
func (q *Request) summary() *Request {
	c := *q
	c.RequestHeaders, c.ResponseHeaders, c.StatusText, c.Protocol = nil, nil, "", ""
	return &c
}

// Network lists the tab's requests after `after` that pass the filter, oldest first. Past limit the
// newest are kept: after a page loads its API calls come last, behind its scripts and pictures.
func (p *Model) Network(t *browser.Tab, after int64, f NetworkFilter, limit int) (*NetworkResult, error) {
	if limit == 0 {
		limit = 50
	}
	if limit < 1 || limit > NetworkMax {
		return nil, wire.Errorf(wire.CodeInvalidParams, "limit is 1-%d", NetworkMax)
	}
	if after < 0 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "after is a sequence number, 0 or more")
	}
	for i, ty := range f.Types {
		f.Types[i] = strings.ToLower(strings.TrimSpace(ty))
	}
	out := &NetworkResult{URL: t.URL(), Requests: []*Request{}}
	l := p.requestsOf(t, false)
	if l == nil {
		return out, nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if after > l.seq {
		after = 0
	}
	out.Last, out.Dropped, out.Total = l.seq, l.lost > after, len(l.entries)
	var matched []*Request
	for _, q := range l.entries {
		if q.Seq > after && f.match(q) {
			matched = append(matched, q)
		}
	}
	if len(matched) > limit {
		out.Skipped = len(matched) - limit
		matched = matched[len(matched)-limit:]
	}
	for _, q := range matched {
		out.Requests = append(out.Requests, q.summary())
	}
	return out, nil
}

// DocumentStatus is the HTTP status the page's own document came with, when it is the page now
// shown; 0 when it is not known.
func (p *Model) DocumentStatus(t *browser.Tab) int {
	l := p.requestsOf(t, false)
	if l == nil {
		return 0
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.mainDoc == nil {
		return 0
	}
	// A page restored from the back-forward cache, or moved within its document, made no request:
	// the newest document is then another page's, and its status says nothing of this one.
	shown, _, _ := strings.Cut(RedactURL(t.URL()), "#")
	doc, _, _ := strings.Cut(l.mainDoc.URL, "#")
	if shown != doc {
		return 0
	}
	return l.mainDoc.Status
}

// RequestDetail is page.request's answer.
type RequestDetail struct {
	Request   *Request `json:"request"`
	Body      *string  `json:"body,omitempty"`
	Truncated bool     `json:"truncated,omitempty"`
	BodyError string   `json:"body_error,omitempty"`
}

// textual says a response of this type is text an agent can read: JSON, text, scripts, XML, forms.
func textual(mime string) bool {
	m := strings.ToLower(mime)
	return strings.HasPrefix(m, "text/") || strings.Contains(m, "json") || strings.Contains(m, "javascript") ||
		strings.Contains(m, "xml") || strings.Contains(m, "x-www-form-urlencoded") || strings.Contains(m, "graphql") ||
		strings.Contains(m, "ecmascript")
}

// Request is one request of the tab with its headers, and with body its response's body: read from
// Chromium, which keeps the newest responses a while (browser.NetworkBufferBytes), at most max
// characters, with its credentials cut.
func (p *Model) Request(ctx context.Context, t *browser.Tab, id string, body bool, max int) (*RequestDetail, error) {
	if max == 0 {
		max = 20_000
	}
	if max < 100 || max > BodyMax {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_chars is 100-%d", BodyMax)
	}
	l := p.requestsOf(t, false)
	var q Request
	found := false
	if l != nil {
		l.mu.Lock()
		for _, e := range l.entries {
			if e.ID == id {
				q, found = *e, true
				break
			}
		}
		l.mu.Unlock()
	}
	if !found {
		return nil, wire.Errorf(wire.CodeNotFound, "no request %s on this tab; the tab keeps its last %d, and page.network lists them", id, requestsKept)
	}
	out := &RequestDetail{Request: &q}
	if !body {
		return out, nil
	}
	switch {
	case q.Pending:
		out.BodyError = "the response has not finished yet"
	case q.Failed != "":
		out.BodyError = "the request failed, so there is no response body"
	case q.Redirect != "":
		out.BodyError = "a redirect has no body; its next hop is its own request"
	case q.Method == "HEAD" || q.Status == 204 || q.Status == 304:
		out.BodyError = "this response has no body"
	case !textual(q.MIME):
		out.BodyError = "the response is " + orDefault(q.MIME, "of no stated type") + ", not text: only JSON, text, scripts and XML are read"
	}
	if out.BodyError != "" {
		return out, nil
	}
	if d := t.Dialog(); d != nil {
		return nil, browser.ErrDialogOpen(d)
	}
	var r struct {
		Body          string `json:"body"`
		Base64Encoded bool   `json:"base64Encoded"`
	}
	if err := t.Call(ctx, "Network.getResponseBody", map[string]any{"requestId": q.requestID}, &r); err != nil {
		// Chromium answers "No resource with given identifier found" or "No data found" for a body
		// it did not keep: too large, or pushed out by newer ones.
		out.BodyError = "the browser no longer holds this response's body (it keeps the newest, each up to 1 MB); reload the page and ask again"
		return out, nil
	}
	text := r.Body
	if r.Base64Encoded {
		raw, err := base64.StdEncoding.DecodeString(r.Body)
		if err != nil || !utf8.Valid(raw) {
			out.BodyError = "the response body is not text"
			return out, nil
		}
		text = string(raw)
	}
	text = RedactBody(text, q.MIME)
	if utf8.RuneCountInString(text) > max {
		text = string([]rune(text)[:max])
		out.Truncated = true
	}
	out.Body = &text
	return out, nil
}
