package main

import (
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The web search step: SearXNG and the search APIs' keys go where they belong — the keys to the key
// proxy's file only, the order WebSearch tries to the app's environment — and a run that does not
// answer the step leaves search as it was.

func TestTheSearchOrderPutsSearxngFirstThenEveryKeyThenDuckDuckGo(t *testing.T) {
	cases := []struct {
		searxng  bool
		keys     map[string]string
		backend  string
		fallback string
	}{
		{true, map[string]string{"KEYPROXY_KEY_KEENABLE": "k"}, "searxng", "keenable,duckduckgo"},
		{true, nil, "searxng", "duckduckgo"},
		{false, map[string]string{"KEYPROXY_KEY_TAVILY": "t", "KEYPROXY_KEY_KEENABLE": "k"}, "keenable", "tavily,duckduckgo"},
		{false, map[string]string{"KEYPROXY_KEY_EXA": "  "}, "duckduckgo", ""},
	}
	for _, c := range cases {
		backend, fallback := searchOrder(c.searxng, c.keys)
		if backend != c.backend || strings.Join(fallback, ",") != c.fallback {
			t.Errorf("searchOrder(%v, %v) = %s, %v; want %s, %s", c.searxng, c.keys, backend, fallback, c.backend, c.fallback)
		}
	}
}

func TestTheSearchStepIsParsedAndAKeyWithSpacesIsRefused(t *testing.T) {
	s, _, problem := parseSetupForm(url.Values{"search": {"on"}, "searxng": {"on"}, "search_keenable": {" kn-0123456789 "}, "search_serper": {""}})
	if problem != nil {
		t.Fatal(problem.Err())
	}
	if s.Search == nil || !s.Search.Searxng || s.Search.Keys["keenable"] != "kn-0123456789" || len(s.Search.Keys) != 1 {
		t.Fatalf("the step reads as %+v", s.Search)
	}
	if s, _, _ := parseSetupForm(url.Values{"mode": {"native"}}); s.Search != nil {
		t.Error("a post without the step answered it")
	}
	if _, _, problem := parseSetupForm(url.Values{"search": {"on"}, "search_exa": {"two words"}}); problem == nil || problem.Field != "search.exa" {
		t.Errorf("a key with a space in it was taken: %+v", problem)
	}
	if _, _, problem := parseSetupForm(url.Values{"search": {"maybe"}}); problem == nil {
		t.Error("an answer the step cannot give was taken")
	}
}

func TestTheSearchKeysGoToTheKeyProxyAndTheOrderToTheApp(t *testing.T) {
	paths := setupTempInstall(t)
	answer := &SearchAnswer{Searxng: true, Keys: map[string]string{"keenable": "kn-secret-0123456789", "perplexity": "pplx-0123456789"}}
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one", Search: answer}, ModeNative); err != nil {
		t.Fatal(err)
	}
	env, keys := readFile(paths.Env), readEnv(readFile(paths.KeyproxyEnv))
	if strings.Contains(env, "kn-secret") || strings.Contains(env, "pplx-") {
		t.Fatal("a search key reached the app's environment")
	}
	if keys["KEYPROXY_KEY_KEENABLE"] != "kn-secret-0123456789" || keys["KEYPROXY_KEY_PERPLEXITY"] != "pplx-0123456789" {
		t.Fatalf("the key proxy's file is %v", keys)
	}
	got := readEnv(env)
	if got["DAEDALUS_SEARCH_BACKEND"] != "searxng" || got["DAEDALUS_SEARCH_FALLBACK"] != "keenable,perplexity,duckduckgo" {
		t.Fatalf("the order is %q then %q", got["DAEDALUS_SEARCH_BACKEND"], got["DAEDALUS_SEARCH_FALLBACK"])
	}

	// Again with SearXNG off and nothing typed: the stored keys stay and still count in the order.
	if err := WriteSetup(paths, Setup{Search: &SearchAnswer{Keys: map[string]string{}}}, ModeNative); err != nil {
		t.Fatal(err)
	}
	keys, got = readEnv(readFile(paths.KeyproxyEnv)), readEnv(readFile(paths.Env))
	if keys["KEYPROXY_KEY_KEENABLE"] != "kn-secret-0123456789" {
		t.Error("a re-run with an empty field lost the stored key")
	}
	if got["DAEDALUS_SEARCH_BACKEND"] != "keenable" || got["DAEDALUS_SEARCH_FALLBACK"] != "perplexity,duckduckgo" {
		t.Errorf("the order after SearXNG went is %q then %q", got["DAEDALUS_SEARCH_BACKEND"], got["DAEDALUS_SEARCH_FALLBACK"])
	}

	// A run that skipped the step changes nothing about search.
	if err := WriteSetup(paths, Setup{USDPerDay: "9"}, ModeNative); err != nil {
		t.Fatal(err)
	}
	if again := readEnv(readFile(paths.Env)); again["DAEDALUS_SEARCH_BACKEND"] != "keenable" || again["DAEDALUS_SEARCH_FALLBACK"] != "perplexity,duckduckgo" {
		t.Errorf("a skipped step changed the order to %q then %q", again["DAEDALUS_SEARCH_BACKEND"], again["DAEDALUS_SEARCH_FALLBACK"])
	}
}

func TestADockerInstallationRunsSearxngAsTheStacksProfile(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{Browser: boolPointer(true), Search: &SearchAnswer{Searxng: true}}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if got := readEnv(readFile(paths.Env))["COMPOSE_PROFILES"]; got != "browser,search" {
		t.Fatalf("the profiles are %q", got)
	}
	if err := WriteSetup(paths, Setup{Search: &SearchAnswer{Searxng: false}}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if got := readEnv(readFile(paths.Env))["COMPOSE_PROFILES"]; got != "browser" {
		t.Fatalf("turning SearXNG off left the profiles at %q", got)
	}
}

func TestSearxngIsInstalledAfterTheStartOnlyNativelyAndOnlyOnce(t *testing.T) {
	paths := setupTempInstall(t)
	on := Setup{Search: &SearchAnswer{Searxng: true}}
	if got := wantedExtras(on, true, paths); strings.Join(got, ",") != "searxng" {
		t.Fatalf("a native install asks for %v", got)
	}
	if got := wantedExtras(on, false, paths); len(got) != 0 {
		t.Errorf("a container asks for %v; its SearXNG is the compose profile", got)
	}
	if got := wantedExtras(Setup{Search: &SearchAnswer{}}, true, paths); len(got) != 0 {
		t.Errorf("SearXNG off still asks for %v", got)
	}
	marker := filepath.Join(paths.Runtime, "searxng", "installed.json")
	if err := os.MkdirAll(filepath.Dir(marker), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(marker, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := wantedExtras(on, true, paths); len(got) != 0 {
		t.Errorf("an installed SearXNG is asked for again: %v", got)
	}
	if restartsFor("searxng") || !restartsFor("browser") {
		t.Error("SearXNG is picked up by the running supervisor; only the others need a restart")
	}
}

// The step on the page: a first run opens with SearXNG on and the catalogue with its links, and a
// reconfigure shows a stored key masked and never in full.
func TestTheSearchStepOpensOnTheRecommendationAndMasksStoredKeys(t *testing.T) {
	paths := setupTempInstall(t)
	server := startedServer(t, paths)
	client := &http.Client{Timeout: 5 * time.Second}
	boot := bootOf(t, get(t, client, server.URL()+"setup"))
	if !boot.Search.Searxng {
		t.Error("a first run does not open with SearXNG on")
	}
	if len(boot.Search.Providers) != len(searchKeyVars) || boot.Search.Providers[0].ID != "keenable" || !boot.Search.Providers[0].Recommended {
		t.Fatalf("the catalogue is %+v", boot.Search.Providers)
	}
	for i, provider := range boot.Search.Providers {
		if provider.ID != searchKeyVars[i][0] || !strings.HasPrefix(provider.SignUp, "https://") || !strings.HasPrefix(provider.KeyPage, "https://") {
			t.Errorf("%+v has no sign-up or key page, or is out of the key order", provider)
		}
	}

	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one", Search: &SearchAnswer{Keys: map[string]string{"keenable": "kn-secret-0123456789"}}}, ModeNative); err != nil {
		t.Fatal(err)
	}
	page := get(t, client, server.URL()+"setup")
	if strings.Contains(page, "kn-secret-0123456789") {
		t.Fatal("the page shows a stored search key in full")
	}
	boot = bootOf(t, page)
	if boot.Kept["search.keys.keenable"] != "••••6789" {
		t.Errorf("the stored key is shown as %q", boot.Kept["search.keys.keenable"])
	}
	if boot.Search.Searxng {
		t.Error("a reconfigure without SearXNG installed opens with it on")
	}
}
