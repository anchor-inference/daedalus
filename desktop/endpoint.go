package main

import (
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
)

// EndpointProbe is the answer to "does this address serve models". URL is the normalised base that
// answered, which may carry a /v1 the operator did not type.
type EndpointProbe struct {
	OK     bool     `json:"ok"`
	Models []string `json:"models"`
	Millis int64    `json:"ms"`
	Error  string   `json:"error,omitempty"`
	URL    string   `json:"url"`
}

const (
	endpointTimeout   = 8 * time.Second
	endpointBodyLimit = 2 << 20
	endpointModelCap  = 500
)

// normaliseEndpoint reduces what the operator typed to a base URL. Userinfo, a query and a fragment
// are refused rather than stripped: a key pasted into the address would otherwise be sent on, or
// dropped silently, and either way the saved address would not be the one that was checked.
func normaliseEndpoint(raw string) (string, error) {
	text := strings.TrimRight(strings.TrimSpace(raw), "/")
	if text == "" {
		return "", errors.New("the address is empty")
	}
	parsed, err := url.Parse(text)
	if err != nil {
		return "", errors.New("the address is not a valid URL")
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", errors.New("the address must start with http:// or https://")
	}
	if parsed.Hostname() == "" {
		return "", errors.New("the address has no host")
	}
	if parsed.User != nil {
		return "", errors.New("the address must not contain a user name or password; put the key in the key field")
	}
	if parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || strings.Contains(text, "#") {
		return "", errors.New("the address must not contain a query or a fragment")
	}
	return text, nil
}

// ProbeEndpoint asks <base>/models and reads the model names out of the answer. Redirects are not
// followed: the check must only talk to the address the operator typed, and a redirect would carry
// the key to somewhere they did not choose.
func ProbeEndpoint(ctx context.Context, client *http.Client, base, key string) EndpointProbe {
	started := time.Now()
	result := EndpointProbe{Models: []string{}}
	finish := func() EndpointProbe {
		result.Millis = time.Since(started).Milliseconds()
		result.Error = scrubKey(result.Error, key)
		return result
	}
	normalised, err := normaliseEndpoint(base)
	if err != nil {
		result.Error = err.Error()
		return finish()
	}
	result.URL = normalised

	ctx, cancel := context.WithTimeout(ctx, endpointTimeout)
	defer cancel()
	copied := http.Client{}
	if client != nil {
		copied = *client
	}
	copied.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

	status, body, err := fetchModels(ctx, &copied, normalised+"/models", key)
	if err == nil && status == http.StatusNotFound && !strings.HasSuffix(normalised, "/v1") {
		// Servers such as Ollama and llama.cpp mount the API under /v1 and the operator often types
		// the bare host. Only a 404 earns the second try; any other answer is the endpoint's own.
		fallback := normalised + "/v1"
		status2, body2, err2 := fetchModels(ctx, &copied, fallback+"/models", key)
		if err2 == nil && status2 != http.StatusNotFound {
			status, body, err = status2, body2, nil
			result.URL = fallback
		}
	}
	if err != nil {
		result.Error = describeEndpointError(ctx, err)
		return finish()
	}
	switch {
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		result.Error = fmt.Sprintf("the endpoint answered %d: it refused the key", status)
	case status >= 300 && status < 400:
		result.Error = fmt.Sprintf("the endpoint answered %d and redirects elsewhere; type the final address", status)
	case status != http.StatusOK:
		result.Error = fmt.Sprintf("the endpoint answered %d", status)
	default:
		models, ok := parseModelNames(body)
		switch {
		case !ok:
			result.Error = "the answer was not a model list"
		case len(models) == 0:
			result.Error = "no models listed"
		default:
			result.OK, result.Models = true, models
		}
	}
	return finish()
}

func fetchModels(ctx context.Context, client *http.Client, target, key string) (int, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Accept", "application/json")
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	// One byte over the limit tells an oversized list from one that ends exactly on it.
	body, err := io.ReadAll(io.LimitReader(resp.Body, endpointBodyLimit+1))
	if err != nil {
		return 0, nil, err
	}
	if len(body) > endpointBodyLimit {
		return resp.StatusCode, nil, nil
	}
	return resp.StatusCode, body, nil
}

// parseModelNames accepts the OpenAI shape {"data":[{"id":..}]} and the one Ollama's own API and a
// few proxies use, {"models":[{"name":..}|{"id":..}]}. ok is false when it is neither.
func parseModelNames(body []byte) ([]string, bool) {
	var doc struct {
		Data   []map[string]any `json:"data"`
		Models []map[string]any `json:"models"`
	}
	if len(body) == 0 || json.Unmarshal(body, &doc) != nil {
		return nil, false
	}
	if doc.Data == nil && doc.Models == nil {
		return nil, false
	}
	seen := map[string]bool{}
	names := []string{}
	for _, entry := range append(doc.Data, doc.Models...) {
		name, _ := entry["id"].(string)
		if name == "" {
			name, _ = entry["name"].(string)
		}
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		names = append(names, name)
		if len(names) == endpointModelCap {
			break
		}
	}
	return names, true
}

func describeEndpointError(ctx context.Context, err error) string {
	var netErr net.Error
	var dns *net.DNSError
	switch {
	case errors.Is(err, context.DeadlineExceeded) || ctx.Err() == context.DeadlineExceeded || (errors.As(err, &netErr) && netErr.Timeout()):
		return "the endpoint did not answer in time"
	case errors.Is(err, context.Canceled):
		return "the check was cancelled"
	case strings.Contains(err.Error(), "connection refused"):
		return "connection refused"
	case errors.As(err, &dns):
		return "the host name could not be found"
	case strings.Contains(err.Error(), "certificate"):
		return "the TLS certificate was not accepted"
	}
	return "the endpoint could not be reached"
}

func scrubKey(text, key string) string {
	if key == "" {
		return text
	}
	return strings.ReplaceAll(text, key, "***")
}
