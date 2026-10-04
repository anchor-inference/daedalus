package main

// The installer runs before the Python host, so it asks the same public provider catalogs itself.
// Model IDs come from the providers; only endpoint addresses and price rules live here.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type freeSource struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	CatalogURL  string `json:"-"`
	DocsURL     string `json:"-"`
	BaseURL     string `json:"-"`
	KeyURL      string `json:"key_url"`
	KeyRequired bool   `json:"key_required"`
}

var installerFreeSources = []freeSource{
	{"kilo", "Kilo Gateway", "https://api.kilo.ai/api/gateway/models", "", "https://api.kilo.ai/api/gateway", "https://kilo.ai/docs/gateway/authentication", false},
	{"openrouter", "OpenRouter", "https://openrouter.ai/api/v1/models", "", "https://openrouter.ai/api/v1", "https://openrouter.ai/settings/keys", true},
	{"opencode_zen", "OpenCode Zen", "https://opencode.ai/zen/v1/models", "https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/web/src/content/docs/zen.mdx", "https://opencode.ai/zen/v1", "https://opencode.ai/auth", true},
}

type installerFreeModel struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	MayTrain   bool   `json:"may_train"`
	ToolsKnown bool   `json:"tools_reported"`
}

type installerFreeProvider struct {
	freeSource
	Models []installerFreeModel `json:"models"`
}

func zeroPrice(value any) bool {
	switch v := value.(type) {
	case string:
		price, err := strconv.ParseFloat(v, 64)
		return err == nil && price == 0
	case float64:
		return v == 0
	}
	return false
}

func zenFreeIDs(document string) map[string]bool {
	ids := map[string]string{}
	free := map[string]bool{}
	for _, line := range strings.Split(document, "\n") {
		if !strings.HasPrefix(line, "|") || strings.Contains(line, "---") {
			continue
		}
		parts := strings.Split(strings.Trim(line, "| "), "|")
		if len(parts) < 3 {
			continue
		}
		for i := range parts {
			parts[i] = strings.Trim(strings.TrimSpace(parts[i]), "` ")
		}
		if parts[2] == "https://opencode.ai/zen/v1/chat/completions" {
			ids[parts[0]] = parts[1]
		}
		if strings.EqualFold(parts[1], "Free") && strings.EqualFold(parts[2], "Free") {
			free[parts[0]] = true
		}
	}
	result := map[string]bool{}
	for name := range free {
		if id := ids[name]; id != "" {
			result[id] = true
		}
	}
	return result
}

func freeModels(source string, body []byte, zenIDs map[string]bool) ([]installerFreeModel, error) {
	var parsed struct {
		Data []map[string]any `json:"data"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil || parsed.Data == nil {
		return nil, errors.New("provider did not return a model list")
	}
	models := []installerFreeModel{}
	for _, row := range parsed.Data {
		id, _ := row["id"].(string)
		if id == "" {
			continue
		}
		free := row["isFree"] == true
		if source == "openrouter" {
			price, _ := row["pricing"].(map[string]any)
			free = zeroPrice(price["prompt"]) && zeroPrice(price["completion"])
		}
		if source == "opencode_zen" {
			free = zenIDs[id]
		}
		if !free {
			continue
		}
		name, _ := row["name"].(string)
		if name == "" {
			name = id
		}
		tools := false
		if params, ok := row["supported_parameters"].([]any); ok {
			for _, param := range params {
				if param == "tools" {
					tools = true
				}
			}
		}
		models = append(models, installerFreeModel{id, name, row["mayTrainOnYourPrompts"] == true || source == "opencode_zen", tools})
	}
	return models, nil
}

func fetchInstallerFree(ctx context.Context, client *http.Client, sources []freeSource) ([]installerFreeProvider, error) {
	if client == nil {
		client = &http.Client{}
	}
	copied := *client
	copied.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	providers := []installerFreeProvider{}
	for _, source := range sources {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, source.CatalogURL, nil)
		if err != nil {
			continue
		}
		res, err := copied.Do(req)
		if err != nil {
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(res.Body, 8<<20))
		res.Body.Close()
		if readErr != nil || res.StatusCode != http.StatusOK {
			continue
		}
		zenIDs := map[string]bool{}
		if source.DocsURL != "" {
			docReq, docErr := http.NewRequestWithContext(ctx, http.MethodGet, source.DocsURL, nil)
			if docErr != nil {
				continue
			}
			docRes, docErr := copied.Do(docReq)
			if docErr != nil {
				continue
			}
			docs, readErr := io.ReadAll(io.LimitReader(docRes.Body, 1<<20))
			docRes.Body.Close()
			if readErr != nil || docRes.StatusCode != http.StatusOK {
				continue
			}
			zenIDs = zenFreeIDs(string(docs))
		}
		models, err := freeModels(source.ID, body, zenIDs)
		if err == nil {
			providers = append(providers, installerFreeProvider{source, models})
		}
	}
	if len(providers) == 0 {
		return nil, errors.New("free model catalogs are unavailable")
	}
	return providers, nil
}

func probeInstallerFree(ctx context.Context, client *http.Client, source freeSource, model, key string) error {
	if client == nil {
		client = &http.Client{}
	}
	copied := *client
	copied.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	endpoint := source.BaseURL + "/chat/completions"
	call := func(messages []map[string]any, tools bool) (map[string]any, error) {
		payload := map[string]any{"model": model, "messages": messages, "max_tokens": 512}
		if tools {
			payload["tools"] = []map[string]any{{"type": "function", "function": map[string]any{"name": "add_numbers", "description": "Add two integers", "parameters": map[string]any{"type": "object", "properties": map[string]any{"a": map[string]string{"type": "integer"}, "b": map[string]string{"type": "integer"}}, "required": []string{"a", "b"}}}}}
			payload["tool_choice"] = "required"
		}
		body, _ := json.Marshal(payload)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if key != "" {
			req.Header.Set("Authorization", "Bearer "+key)
		}
		res, err := copied.Do(req)
		if err != nil {
			return nil, err
		}
		defer res.Body.Close()
		if res.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("provider returned HTTP %d", res.StatusCode)
		}
		var result struct {
			Choices []struct {
				Message map[string]any `json:"message"`
			} `json:"choices"`
		}
		if err := json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&result); err != nil || len(result.Choices) == 0 {
			return nil, errors.New("provider returned no answer")
		}
		return result.Choices[0].Message, nil
	}
	messages := []map[string]any{{"role": "user", "content": "Call add_numbers with a=2 and b=3, then report its result."}}
	first, err := call(messages, true)
	if err != nil {
		return err
	}
	calls, _ := first["tool_calls"].([]any)
	if len(calls) == 0 {
		return errors.New("model did not call the tool")
	}
	tool, _ := calls[0].(map[string]any)
	function, _ := tool["function"].(map[string]any)
	id, _ := tool["id"].(string)
	if function["name"] != "add_numbers" || id == "" {
		return errors.New("model called a different tool")
	}
	arguments, _ := function["arguments"].(string)
	var numbers map[string]int
	if json.Unmarshal([]byte(arguments), &numbers) != nil || len(numbers) != 2 || numbers["a"] != 2 || numbers["b"] != 3 {
		return errors.New("model supplied invalid tool arguments")
	}
	messages = append(messages, first, map[string]any{"role": "tool", "tool_call_id": id, "content": "5"})
	second, err := call(messages, false)
	if err != nil {
		return err
	}
	content, _ := second["content"].(string)
	if !strings.Contains(content, "5") {
		return errors.New("model did not use the tool result")
	}
	return nil
}

func (s *Server) handleSetupFreeCatalog(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post to list free models", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 24*time.Second)
	defer cancel()
	providers, err := fetchInstallerFree(ctx, s.helpers.client, installerFreeSources)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"providers": providers})
}

func (s *Server) handleSetupFreeProbe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post to test a free model", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	var body struct {
		Provider string `json:"provider"`
		Model    string `json:"model"`
		Key      string `json:"key"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid request"})
		return
	}
	var source *freeSource
	for i := range installerFreeSources {
		if installerFreeSources[i].ID == body.Provider {
			source = &installerFreeSources[i]
		}
	}
	if source == nil || len(body.Model) == 0 || len(body.Model) > 200 {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "unknown model or provider"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 55*time.Second)
	defer cancel()
	listed, err := fetchInstallerFree(ctx, s.helpers.client, []freeSource{*source})
	if err == nil {
		found := false
		for _, model := range listed[0].Models {
			found = found || model.ID == body.Model
		}
		if !found {
			err = errors.New("model is no longer free")
		}
	}
	key := clean(body.Key)
	if key == "" && body.Provider == "openrouter" {
		key = providerKey(CurrentSetup(s.app.paths), "openrouter")
	}
	if err == nil && source.KeyRequired && key == "" {
		err = errors.New("get a provider key first")
	}
	if err == nil {
		err = probeInstallerFree(ctx, s.helpers.client, *source, body.Model, key)
	}
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
