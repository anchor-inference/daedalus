package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestEndpointOpenAIShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			http.NotFound(w, r)
			return
		}
		fmt.Fprint(w, `{"data":[{"id":"a"},{"id":"b"},{"id":"a"}]}`)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), srv.Client(), srv.URL+"/v1/", "")
	if !got.OK || strings.Join(got.Models, ",") != "a,b" || got.URL != srv.URL+"/v1" {
		t.Fatalf("%+v", got)
	}
}

func TestEndpointModelsShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"models":[{"name":"llama"},{"id":"qwen"}]}`)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), nil, srv.URL, "")
	if !got.OK || strings.Join(got.Models, ",") != "llama,qwen" {
		t.Fatalf("%+v", got)
	}
}

func TestEndpointKeySentOnlyWhenGiven(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Header.Get("Authorization"))
		mu.Unlock()
		fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
	}))
	defer srv.Close()
	ProbeEndpoint(context.Background(), nil, srv.URL, "")
	ProbeEndpoint(context.Background(), nil, srv.URL, "sk-secret")
	if len(seen) != 2 || seen[0] != "" || seen[1] != "Bearer sk-secret" {
		t.Fatalf("%q", seen)
	}
}

func TestEndpointUnauthorizedNeverEchoesKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "bad key "+r.Header.Get("Authorization"), http.StatusUnauthorized)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), nil, srv.URL, "sk-secret")
	if got.OK || !strings.Contains(got.Error, "401") || strings.Contains(got.Error, "sk-secret") {
		t.Fatalf("%+v", got)
	}
}

func TestEndpointFallsBackToV1OnlyOnNotFound(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/v1/models" {
			fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), nil, srv.URL, "")
	if !got.OK || got.URL != srv.URL+"/v1" || len(paths) != 2 {
		t.Fatalf("%+v %v", got, paths)
	}
	// A base that already ends in /v1 is tried once.
	paths = nil
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		http.NotFound(w, r)
	}))
	defer srv2.Close()
	got = ProbeEndpoint(context.Background(), nil, srv2.URL+"/v1", "")
	if got.OK || len(paths) != 1 {
		t.Fatalf("%+v %v", got, paths)
	}
}

func TestEndpointDoesNotFollowRedirects(t *testing.T) {
	hit := false
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hit = true
		fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
	}))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+"/models", http.StatusFound)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), srv.Client(), srv.URL, "sk-secret")
	if got.OK || hit || !strings.Contains(got.Error, "302") {
		t.Fatalf("hit=%v %+v", hit, got)
	}
}

func TestEndpointOversizedBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"data":[{"id":"`+strings.Repeat("x", 3<<20)+`"}]}`)
	}))
	defer srv.Close()
	got := ProbeEndpoint(context.Background(), nil, srv.URL, "")
	if got.OK {
		t.Fatalf("%+v", got)
	}
}

func TestEndpointNoModelsAndCap(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/empty/models" {
			fmt.Fprint(w, `{"data":[]}`)
			return
		}
		var b strings.Builder
		b.WriteString(`{"data":[`)
		for i := 0; i < 700; i++ {
			if i > 0 {
				b.WriteString(",")
			}
			fmt.Fprintf(&b, `{"id":"m%d"}`, i)
		}
		b.WriteString(`]}`)
		fmt.Fprint(w, b.String())
	}))
	defer srv.Close()
	if got := ProbeEndpoint(context.Background(), nil, srv.URL+"/empty", ""); got.OK || got.Error != "no models listed" {
		t.Fatalf("%+v", got)
	}
	if got := ProbeEndpoint(context.Background(), nil, srv.URL, ""); !got.OK || len(got.Models) != 500 || got.Models[0] != "m0" {
		t.Fatalf("%d", len(got.Models))
	}
}

func TestEndpointConnectionRefusedNeverContainsKey(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	got := ProbeEndpoint(context.Background(), nil, url, "sk-secret")
	if got.OK || got.Error == "" || strings.Contains(got.Error, "sk-secret") {
		t.Fatalf("%+v", got)
	}
	if scrubKey("oops sk-secret here", "sk-secret") != "oops *** here" {
		t.Fatal("scrub")
	}
}

func TestNormaliseEndpoint(t *testing.T) {
	for _, bad := range []string{"", "  ", "ftp://host/x", "http://user:pass@host", "http://host?a=1", "http://host/#frag", "http://", "localhost:11434"} {
		if got, err := normaliseEndpoint(bad); err == nil {
			t.Errorf("%q accepted as %q", bad, got)
		}
	}
	got, err := normaliseEndpoint("  http://127.0.0.1:11434/v1//  ")
	if err != nil || got != "http://127.0.0.1:11434/v1" {
		t.Fatalf("%q %v", got, err)
	}
	if got := ProbeEndpoint(context.Background(), nil, "ftp://host", "k"); got.OK || got.Error == "" {
		t.Fatalf("%+v", got)
	}
}
