package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFreeCatalogUsesPublishedPriceAndFlag(t *testing.T) {
	kilo, err := freeModels("kilo", []byte(`{"data":[{"id":"free-without-suffix","isFree":true},{"id":"looks-free:free","isFree":false}]}`), nil)
	if err != nil || len(kilo) != 1 || kilo[0].ID != "free-without-suffix" {
		t.Fatalf("Kilo free flag: %v, %v", kilo, err)
	}
	router, err := freeModels("openrouter", []byte(`{"data":[{"id":"zero","pricing":{"prompt":"0","completion":"0"}},{"id":"unknown","pricing":{"prompt":"0"}},{"id":"paid","pricing":{"prompt":"0","completion":"0.1"}}]}`), nil)
	if err != nil || len(router) != 1 || router[0].ID != "zero" {
		t.Fatalf("OpenRouter price: %v, %v", router, err)
	}
}

func TestZenNeedsPublishedFreePriceAndChatEndpoint(t *testing.T) {
	docs := "| Big Pickle | big-pickle | `https://opencode.ai/zen/v1/chat/completions` |\n" +
		"| Other Free | other-free | `https://opencode.ai/zen/v1/responses` |\n" +
		"| Big Pickle | Free | Free |\n| Other Free | Free | Free |\n"
	ids := zenFreeIDs(docs)
	if len(ids) != 1 || !ids["big-pickle"] {
		t.Fatalf("Zen IDs: %v", ids)
	}
	models, err := freeModels("opencode_zen", []byte(`{"data":[{"id":"big-pickle"},{"id":"other-free"}]}`), ids)
	if err != nil || len(models) != 1 || models[0].ID != "big-pickle" || !models[0].MayTrain {
		t.Fatalf("Zen models: %v, %v", models, err)
	}
}

func TestFreeCatalogAndToolCycle(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/models":
			_, _ = w.Write([]byte(`{"data":[{"id":"free-model","isFree":true,"supported_parameters":["tools"]}]}`))
		case "/chat/completions":
			var request struct {
				Messages []map[string]any `json:"messages"`
			}
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Error(err)
			}
			if len(request.Messages) == 1 {
				_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","tool_calls":[{"id":"call-1","type":"function","function":{"name":"add_numbers","arguments":"{\"a\":2,\"b\":3}"}}]}}]}`))
			} else {
				_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"The result is 5."}}]}`))
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	source := freeSource{ID: "kilo", Name: "Kilo Gateway", CatalogURL: server.URL + "/models", BaseURL: server.URL}
	listed, err := fetchInstallerFree(context.Background(), server.Client(), []freeSource{source})
	if err != nil || len(listed) != 1 || len(listed[0].Models) != 1 {
		t.Fatalf("catalog: %v, %v", listed, err)
	}
	if err := probeInstallerFree(context.Background(), server.Client(), source, "free-model", ""); err != nil {
		t.Fatal(err)
	}
}

func TestFreeSetupRequiresASelectedModel(t *testing.T) {
	_, _, refused := parseSetupForm(map[string][]string{"kind": {"free"}, "free_provider": {"kilo"}})
	if refused == nil || refused.Field != "free.model" {
		t.Fatalf("missing model was accepted: %v", refused)
	}
	got, _, refused := parseSetupForm(map[string][]string{"kind": {"free"}, "free_provider": {"kilo"}, "free_model": {"stepfun/step-3.7-flash:free"}})
	if refused != nil || got.FreeProvider != "kilo" || !strings.Contains(got.FreeModel, "step-3.7") {
		t.Fatalf("free route was rejected: %+v, %v", got, refused)
	}
}

func TestChangingSetupModelKindClearsOldSelection(t *testing.T) {
	current := map[string]string{"DAEDALUS_LOCAL_MODEL": "local-old", "DAEDALUS_FREE_PROVIDER": "kilo", "DAEDALUS_FREE_MODEL": "free-old"}
	updates := envUpdates(Paths{}, Setup{ModelKind: "cloud"}, current, "", "")
	for _, update := range updates {
		if update.Key == "DAEDALUS_FREE_PROVIDER" || update.Key == "DAEDALUS_FREE_MODEL" {
			if update.Value != "" {
				t.Fatalf("old free selection remained after switching to cloud: %s=%q", update.Key, update.Value)
			}
		}
	}
	updates = envUpdates(Paths{}, Setup{ModelKind: "free", FreeProvider: "kilo", FreeModel: "free-new"}, current, "", "")
	for _, update := range updates {
		if update.Key == "DAEDALUS_LOCAL_MODEL" && update.Value != "" {
			t.Fatalf("old local model remained after choosing Free: %q", update.Value)
		}
	}
}
