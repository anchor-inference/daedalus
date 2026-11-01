package toolsmcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
)

// ingressSource is the name team posts carry at the hook listener, so the host tells them from a
// CLI's own hooks.
const ingressSource = "team"

// directTimeout bounds a call on the host's own team route, which answers at once and holds only a
// send that waits for its reply.
const directTimeout = time.Minute

// helloTimeout bounds the announcement that the CLI loaded the tools; it is never waited for by the
// CLI, and a host that is away hears of the tools at the first call instead.
const helloTimeout = 5 * time.Second

// route is where the tools' calls go.
type route interface {
	call(ctx context.Context, req request, hold time.Duration) (text string, isErr bool)
	// hello tells the host the CLI has started this server and read its tools: the proof that the
	// team channel is up, before any call is made on it.
	hello(ctx context.Context, source string, fields map[string]any)
}

// routeFrom picks the route from the launch's environment. The host's team route is used for the
// team's tools when the launch names one (DAEDALUS_TEAM_URL): it reaches the host directly, which only
// works where the CLI can reach the host's port. Otherwise, and for every other set, the calls go to
// this daemon's hook listener, which every launch can reach, and the host answers them as it answers
// a held hook.
func routeFrom(env func(string) string, team bool) (route, error) {
	if base := env("DAEDALUS_TEAM_URL"); base != "" && team {
		token := env("DAEDALUS_TEAM_TOKEN")
		if token == "" {
			return nil, errors.New("DAEDALUS_TEAM_URL is set without DAEDALUS_TEAM_TOKEN")
		}
		u, err := url.Parse(base)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return nil, fmt.Errorf("DAEDALUS_TEAM_URL %q is not an http URL", base)
		}
		return direct{base: strings.TrimRight(base, "/"), token: token}, nil
	}
	hookURL, token := env("DAEDALUS_HOOK_URL"), env("DAEDALUS_HOOK_TOKEN")
	if hookURL == "" || token == "" {
		return nil, errors.New("DAEDALUS_HOOK_URL and DAEDALUS_HOOK_TOKEN are not set; this is not a launch")
	}
	return ingress{url: hookURL, token: token}, nil
}

// body is what either route is posted: the fields, and for the team's listener posts the
// operation's name.
func body(req request, withTool bool) ([]byte, error) {
	m := make(map[string]any, len(req.fields)+1)
	for k, v := range req.fields {
		m[k] = v
	}
	if withTool && req.withTool {
		m["tool"] = req.op
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	if len(b) > config.HookBodyBytes {
		return nil, errInvalid{"the call is larger than a megabyte; shorten it and point to a file instead"}
	}
	return b, nil
}

// ingress posts to the launch's hook listener under the set's name (`team`, `tools`), held for the
// host's reply.
type ingress struct{ url, token string }

func (r ingress) call(ctx context.Context, req request, hold time.Duration) (string, bool) {
	b, err := body(req, true)
	if err != nil {
		return err.Error(), true
	}
	status, reply, err := hooks.Post(ctx, r.url, r.token, req.source, b, hold)
	if err != nil {
		return "the team could not be reached: " + plain(err), true
	}
	return outcome(req, status, reply)
}

func (r ingress) hello(ctx context.Context, source string, fields map[string]any) {
	fields["tool"] = "hello"
	b, err := json.Marshal(fields)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, helloTimeout)
	defer cancel()
	_, _, _ = hooks.Post(ctx, r.url, r.token, source, b, 0)
}

// direct posts to the host's team route, `<base>/<op>`: `report`, `ask`, or one of the agent tools'.
type direct struct{ base, token string }

func (r direct) call(ctx context.Context, req request, hold time.Duration) (string, bool) {
	b, err := body(req, false)
	if err != nil {
		return err.Error(), true
	}
	// Only a send that waits for its reply is held there; its hold covers the wait.
	ctx, cancel := context.WithTimeout(ctx, max(directTimeout, hold))
	defer cancel()
	hr, err := http.NewRequestWithContext(ctx, http.MethodPost, r.base+"/"+req.op, bytes.NewReader(b))
	if err != nil {
		return err.Error(), true
	}
	hr.Header.Set("Content-Type", "application/json")
	hr.Header.Set("X-Daedalus-Team-Token", r.token)
	// No proxy from the environment: it would only carry the token somewhere else.
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	resp, err := client.Do(hr)
	if err != nil {
		return "the team could not be reached: " + plain(err), true
	}
	defer resp.Body.Close()
	reply, err := io.ReadAll(io.LimitReader(resp.Body, config.HookBodyBytes))
	if err != nil {
		return "reading the team's answer: " + err.Error(), true
	}
	return outcome(req, resp.StatusCode, reply)
}

// hello has nowhere to go on the host's own route, which has no such endpoint; the host learns of the
// tools from the first call.
func (r direct) hello(context.Context, string, map[string]any) {}

// outcome turns the host's HTTP answer into the tool's result.
func outcome(req request, status int, reply []byte) (string, bool) {
	text, isErr := resultText(reply)
	switch {
	case status >= 200 && status < 300:
		if text == "" {
			return req.fallback, req.fallbackErr
		}
		return text, isErr
	case status == http.StatusUnauthorized || status == http.StatusGone:
		// The launch is over or was never this one: nobody will read the call, and saying so keeps a
		// worker from waiting on an answer.
		return "this session is no longer connected to its team; nobody received the call", true
	case status == http.StatusTooManyRequests:
		return "too many calls at once; wait a moment and call again", true
	}
	if text == "" {
		text = http.StatusText(status)
	}
	return fmt.Sprintf("the team refused the call (%d): %s", status, text), true
}

// plain is an HTTP client error without the method, URL and address it is wrapped in, which say
// nothing to a model and would put the hook URL into its context.
func plain(err error) string {
	var ue *url.Error
	if errors.As(err, &ue) {
		err = ue.Err
	}
	var oe *net.OpError
	if errors.As(err, &oe) {
		err = oe.Err
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "no answer in time"
	}
	return err.Error()
}
