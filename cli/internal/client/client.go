// Package client is daedalus-cli's side of the Daedalus API: the REST calls the app makes, and the
// session stream, read with a cursor and resumed across drops exactly as the app's own reader does.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// A paired device's token goes in `Authorization: Device <token>`; the static API token, which a
// script may pass with --token, in the header the app's own token travels in.
const (
	DeviceTokenPrefix = "dcl_"
	StaticTokenHeader = "x-daedalus-token"
)

// IsDeviceToken reports whether a token is one pairing minted.
func IsDeviceToken(token string) bool { return strings.HasPrefix(token, DeviceTokenPrefix) }

// Client talks to one server.
type Client struct {
	base  string
	token string
	// http is for requests that end; stream has no overall timeout, since a session stream is open
	// for as long as the screen is.
	http   *http.Client
	stream *http.Client
	// UserAgent names this client in the server's log.
	UserAgent string
}

// New returns a client for base (a URL without a trailing slash) with token.
func New(base, token string) *Client {
	return &Client{
		base:      strings.TrimRight(base, "/"),
		token:     token,
		http:      &http.Client{Timeout: 60 * time.Second},
		stream:    &http.Client{},
		UserAgent: "daedalus-cli",
	}
}

// Base is the server's base URL.
func (c *Client) Base() string { return c.base }

// Token is the credential the client sends.
func (c *Client) Token() string { return c.token }

// WithToken returns a copy that sends another credential: pairing starts without one.
func (c *Client) WithToken(token string) *Client {
	out := *c
	out.token = token
	return &out
}

// APIError is a response the server refused.
type APIError struct {
	Status int
	Detail string
}

func (e *APIError) Error() string {
	if e.Detail == "" {
		return fmt.Sprintf("the server answered %d", e.Status)
	}
	return fmt.Sprintf("the server answered %d: %s", e.Status, e.Detail)
}

// ErrUnauthorized means the server refused the credential: the device was revoked or never paired.
var ErrUnauthorized = errors.New("the server refused this device's credential")

// IsUnauthorized reports whether err is a refused credential.
func IsUnauthorized(err error) bool {
	var api *APIError
	return errors.Is(err, ErrUnauthorized) || (errors.As(err, &api) && (api.Status == http.StatusUnauthorized || api.Status == http.StatusForbidden))
}

// IsNotFound reports whether err is a 404.
func IsNotFound(err error) bool {
	var api *APIError
	return errors.As(err, &api) && api.Status == http.StatusNotFound
}

func (c *Client) url(path string, query url.Values) string {
	u := c.base + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	return u
}

func (c *Client) authorize(req *http.Request) {
	switch {
	case c.token == "":
	case IsDeviceToken(c.token):
		req.Header.Set("Authorization", "Device "+c.token)
	default:
		req.Header.Set(StaticTokenHeader, c.token)
	}
	req.Header.Set("User-Agent", c.UserAgent)
}

// Do sends one request with a JSON body (nil for none) and decodes a JSON answer into out (nil to
// discard it).
func (c *Client) Do(ctx context.Context, method, path string, query url.Values, body, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.url(path, query), reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	c.authorize(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return readAPIError(resp)
	}
	if out == nil || resp.StatusCode == http.StatusNoContent {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("%s %s: the answer is not the JSON expected: %w", method, path, err)
	}
	return nil
}

// readAPIError reads FastAPI's `{"detail": ...}`, whose detail is a string or a list of
// validation errors.
func readAPIError(resp *http.Response) error {
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	e := &APIError{Status: resp.StatusCode}
	var body struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(raw, &body) == nil && len(body.Detail) > 0 {
		var s string
		if json.Unmarshal(body.Detail, &s) == nil {
			e.Detail = s
		} else {
			e.Detail = string(body.Detail)
		}
	} else {
		e.Detail = strings.TrimSpace(string(raw))
		if len(e.Detail) > 300 {
			e.Detail = e.Detail[:300]
		}
	}
	return e
}
