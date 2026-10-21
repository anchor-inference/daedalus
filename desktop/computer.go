package main

// The launcher's computer-use API: what the host asks on the operator's behalf — the state of the
// desktop daemon, the apps on screen to choose a scope from, a lease granted or revoked, the stop,
// the resume after the operator's own hand, a system permission asked for. The launcher answers it
// through its operator connection to deskd (deskd_client.go); the host never holds that token.
//
// Every route asks for the launcher's token and checks the address it was sent to, reads included:
// window titles and app names are the operator's own, and the loopback port is reachable by every
// page they open. Native mode only — Docker mode never offers computer use.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// computerCallTimeout bounds one call to the daemon. A permission request is the exception: on
// macOS it may wait on the system's own prompt.
const (
	computerCallTimeout       = 10 * time.Second
	computerPermissionTimeout = 60 * time.Second
)

// computerMaxBody bounds what a request may carry: the largest is a grant, a few hundred bytes.
const computerMaxBody = 16 << 10

// The bounds the daemon itself enforces on a grant, checked here first so a malformed request from
// the host is a 400 that names the field rather than a JSON-RPC error that names a parameter.
const (
	computerMinLeaseMs  = 1000
	computerMaxLeaseMs  = 14400000
	computerMaxSession  = 128
	computerMaxScopeRef = 256
)

// computer is the server's operator client, made the first time a route needs it.
func (s *Server) computer() *deskdOperator {
	s.computerOnce.Do(func() { s.computerClient = newDeskdOperator(s.app.paths) })
	return s.computerClient
}

// computerGuard answers the request itself and reports false when it may not go on: the wrong
// method, a request without the token or aimed at another address, a Docker installation.
func (s *Server) computerGuard(w http.ResponseWriter, r *http.Request, method string) bool {
	if r.Method != method {
		w.Header().Set("Allow", method)
		writeComputerError(w, http.StatusMethodNotAllowed, "this route answers "+method+" only")
		return false
	}
	if !s.ownAddress(r.Host) || !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return false
	}
	if !s.app.Native() {
		writeComputerError(w, http.StatusNotImplemented, "computer use belongs to the native application; in Docker mode there is no desktop for the agent to reach")
		return false
	}
	return true
}

func (s *Server) handleComputer(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodGet) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), computerCallTimeout)
	defer cancel()
	client := s.computer()
	infoRaw, err := client.Call(ctx, "info", nil)
	if err != nil {
		writeComputerFailure(w, err)
		return
	}
	statusRaw, err := client.Call(ctx, "lease.status", nil)
	if err != nil {
		writeComputerFailure(w, err)
		return
	}
	var info map[string]json.RawMessage
	var status map[string]json.RawMessage
	if json.Unmarshal(infoRaw, &info) != nil || json.Unmarshal(statusRaw, &status) != nil {
		writeComputerError(w, http.StatusBadGateway, "the desktop daemon answered something the launcher cannot read")
		return
	}
	body := map[string]json.RawMessage{"available": json.RawMessage("true")}
	for _, key := range []string{"platform", "permissions", "secure", "displays", "limits", "version"} {
		if value, ok := info[key]; ok {
			body[key] = value
		}
	}
	// The lease and the pause from lease.status, asked after info, so they are the newer of the two.
	for _, key := range []string{"lease", "human"} {
		if value, ok := status[key]; ok {
			body[key] = value
		} else if value, ok := info[key]; ok {
			body[key] = value
		}
	}
	writeComputerJSON(w, http.StatusOK, body)
}

func (s *Server) handleComputerApps(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodGet) {
		return
	}
	s.computerCall(w, r, "apps.list", nil, computerCallTimeout)
}

// computerMaxPreviewAsk bounds the max the preview route accepts. The daemon caps a preview at 960
// pixels itself; this only stops a nonsense number before it travels.
const computerMaxPreviewAsk = 8192

// handleComputerPreview answers a thumbnail of a display or a window as JPEG bytes, for the grant
// picker's <img>. The daemon carries it base64 inside JSON; the launcher unwraps it so the host can
// pass it on without decoding anything. A refusal (a protected window, a locked screen) stays JSON.
func (s *Server) handleComputerPreview(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodGet) {
		return
	}
	q := r.URL.Query()
	params := map[string]any{}
	display, window := q.Get("display"), q.Get("window")
	if display != "" && window != "" {
		writeComputerError(w, http.StatusBadRequest, "give display or window, not both")
		return
	}
	if len(display) > computerMaxScopeRef || len(window) > computerMaxScopeRef {
		writeComputerError(w, http.StatusBadRequest, "display or window is too long")
		return
	}
	if display != "" {
		params["display"] = display
	}
	if window != "" {
		params["window"] = window
	}
	if raw := q.Get("max"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > computerMaxPreviewAsk {
			writeComputerError(w, http.StatusBadRequest, "max must be a whole number of pixels between 1 and 8192")
			return
		}
		params["max_edge"] = n
	}
	ctx, cancel := context.WithTimeout(r.Context(), computerCallTimeout)
	defer cancel()
	result, err := s.computer().Call(ctx, "preview", params)
	if err != nil {
		writeComputerFailure(w, err)
		return
	}
	var preview struct {
		Image string `json:"image"`
	}
	picture, decodeErr := []byte(nil), json.Unmarshal(result, &preview)
	if decodeErr == nil {
		picture, decodeErr = base64.StdEncoding.DecodeString(preview.Image)
	}
	if decodeErr != nil || len(picture) == 0 {
		writeComputerError(w, http.StatusBadGateway, "the desktop daemon answered something the launcher cannot read")
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	// The operator's screen, as it was a moment ago: never worth keeping, never worth reusing.
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Length", strconv.Itoa(len(picture)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(picture)
}

// computerScope is a lease's scope as the daemon takes it.
type computerScope struct {
	Kind    string `json:"kind"`
	Window  string `json:"window,omitempty"`
	App     string `json:"app,omitempty"`
	Display string `json:"display,omitempty"`
}

type computerGrant struct {
	Session string        `json:"session"`
	Scope   computerScope `json:"scope"`
	TTLMs   int64         `json:"ttl_ms,omitempty"`
}

// validate names what is wrong with a grant, or returns "". A scope carries the one reference its
// kind needs and no other, so a window scope with a stray app in it is not quietly read as either.
func (g computerGrant) validate() string {
	if g.Session == "" || len(g.Session) > computerMaxSession {
		return "session must be between 1 and 128 characters"
	}
	want := map[string]string{"window": g.Scope.Window, "app": g.Scope.App, "display": g.Scope.Display}
	switch g.Scope.Kind {
	case "window", "app", "display":
		if want[g.Scope.Kind] == "" {
			return "a " + g.Scope.Kind + " scope names its " + g.Scope.Kind
		}
	case "desktop":
	default:
		return "scope.kind must be window, app, display or desktop"
	}
	for kind, value := range want {
		if kind != g.Scope.Kind && value != "" {
			return "a " + g.Scope.Kind + " scope does not name a " + kind
		}
		if len(value) > computerMaxScopeRef {
			return "scope." + kind + " is too long"
		}
	}
	if g.TTLMs != 0 && (g.TTLMs < computerMinLeaseMs || g.TTLMs > computerMaxLeaseMs) {
		return "ttl_ms must be between 1000 and 14400000"
	}
	return ""
}

func (s *Server) handleComputerGrant(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodPost) {
		return
	}
	var grant computerGrant
	if !readComputerBody(w, r, &grant) {
		return
	}
	if problem := grant.validate(); problem != "" {
		writeComputerError(w, http.StatusBadRequest, problem)
		return
	}
	s.computerCall(w, r, "lease.grant", grant, computerCallTimeout)
}

func (s *Server) handleComputerRevoke(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodPost) {
		return
	}
	var revoke struct {
		Session string `json:"session,omitempty"`
	}
	if !readComputerBody(w, r, &revoke) {
		return
	}
	if len(revoke.Session) > computerMaxSession {
		writeComputerError(w, http.StatusBadRequest, "session must be at most 128 characters")
		return
	}
	s.computerCall(w, r, "lease.revoke", revoke, computerCallTimeout)
}

// handleComputerBare serves the two calls that take nothing: the stop and the resume.
func (s *Server) handleComputerBare(method string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.computerGuard(w, r, http.MethodPost) {
			return
		}
		var empty struct{}
		if !readComputerBody(w, r, &empty) {
			return
		}
		s.computerCall(w, r, method, nil, computerCallTimeout)
	}
}

func (s *Server) handleComputerPermissions(w http.ResponseWriter, r *http.Request) {
	if !s.computerGuard(w, r, http.MethodPost) {
		return
	}
	var ask struct {
		Which string `json:"which"`
	}
	if !readComputerBody(w, r, &ask) {
		return
	}
	switch ask.Which {
	case "screen", "accessibility", "input_monitoring":
	default:
		writeComputerError(w, http.StatusBadRequest, "which must be screen, accessibility or input_monitoring")
		return
	}
	s.computerCall(w, r, "permissions.request", ask, computerPermissionTimeout)
}

// computerCall makes one call and answers with its result as it came.
func (s *Server) computerCall(w http.ResponseWriter, r *http.Request, method string, params any, timeout time.Duration) {
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()
	result, err := s.computer().Call(ctx, method, params)
	if err != nil {
		writeComputerFailure(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(result)
}

// readComputerBody decodes a body strictly, as the daemon decodes its parameters: an unknown field
// is refused rather than dropped. An empty body counts as {}.
func readComputerBody(w http.ResponseWriter, r *http.Request, into any) bool {
	raw, err := io.ReadAll(io.LimitReader(r.Body, computerMaxBody+1))
	if err != nil {
		writeComputerError(w, http.StatusBadRequest, "the request body could not be read")
		return false
	}
	if len(raw) > computerMaxBody {
		writeComputerError(w, http.StatusRequestEntityTooLarge, "the request body is too large")
		return false
	}
	if strings.TrimSpace(string(raw)) == "" {
		raw = []byte("{}")
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(into); err != nil {
		writeComputerError(w, http.StatusBadRequest, "the request body is not what this route takes: "+err.Error())
		return false
	}
	return true
}

// writeComputerFailure answers a failed call: the daemon's refusal as a 409 carrying its code,
// message and data untouched; no daemon to ask as a 503 with the reason in a sentence.
func writeComputerFailure(w http.ResponseWriter, err error) {
	var refused *deskdError
	if errors.As(err, &refused) {
		data := refused.Data
		if len(data) == 0 {
			data = json.RawMessage("null")
		}
		writeComputerJSON(w, http.StatusConflict, map[string]any{"error": map[string]any{"code": refused.Code, "message": refused.Message, "data": data}})
		return
	}
	reason := "the desktop daemon cannot be reached"
	var missing *errDeskdUnavailable
	if errors.As(err, &missing) {
		reason = missing.reason
	}
	writeComputerJSON(w, http.StatusServiceUnavailable, map[string]any{"available": false, "reason": reason})
}

func writeComputerError(w http.ResponseWriter, status int, message string) {
	writeComputerJSON(w, status, map[string]any{"error": map[string]any{"message": message}})
}

func writeComputerJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
