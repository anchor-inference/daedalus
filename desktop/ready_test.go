package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestParsePairingURLTakesTheLastAddress(t *testing.T) {
	if got := parsePairingURL(" http://127.0.0.1:8765/app/#pair=abc \n"); got != "http://127.0.0.1:8765/app/#pair=abc" {
		t.Fatalf("got %q", got)
	}
	if got := parsePairingURL("stale\nhttp://127.0.0.1:8765/p/one\nhttp://127.0.0.1:8765/p/two\n"); got != "http://127.0.0.1:8765/p/two" {
		t.Fatalf("got %q", got)
	}
	if got := parsePairingURL("no link here\n"); got != "" {
		t.Fatalf("invented a link: %q", got)
	}
}

// The server writes the link to a file when the stack comes up, and the launcher offers it only
// while it belongs to that start: a link is good for one use, and an older file holds one that has
// most likely been spent already.
func TestPairingIsReadOnlyFromAFileTheLastStartWrote(t *testing.T) {
	started := time.Now()
	fresh := fmt.Sprintf("%d\nhttp://127.0.0.1:8765/api/auth/pair?code=fresh\n", started.Add(2*time.Second).Unix())
	if got := pairingFromFile(fresh, started); got != "http://127.0.0.1:8765/api/auth/pair?code=fresh" {
		t.Fatalf("got %q", got)
	}
	stale := fmt.Sprintf("%d\nhttp://127.0.0.1:8765/api/auth/pair?code=spent\n", started.Add(-time.Hour).Unix())
	if got := pairingFromFile(stale, started); got != "" {
		t.Fatalf("a link from an earlier start was offered: %q", got)
	}
	// Nothing to measure against: the launcher did not start this stack itself.
	if got := pairingFromFile(stale, time.Time{}); got != "http://127.0.0.1:8765/api/auth/pair?code=spent" {
		t.Fatalf("got %q", got)
	}
	if got := pairingFromFile("cat: no such file\n", started); got != "" {
		t.Fatalf("invented a link: %q", got)
	}
}

func TestAMintedLinkIsReadOffTheFirstLine(t *testing.T) {
	out := "http://127.0.0.1:8765/api/auth/pair?code=one\nthis link opens once and expires in 30 minutes\n"
	if got := firstPairingURL(out); got != "http://127.0.0.1:8765/api/auth/pair?code=one" {
		t.Fatalf("got %q", got)
	}
	if got := firstPairingURL("no link here\n"); got != "" {
		t.Fatalf("invented a link: %q", got)
	}
}

func TestWaitReadyPollsUntilTheAppAnswers(t *testing.T) {
	var hits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	// The stack's own answer, as a native start expects it.
	server.Config.Handler = bootAnswer("boot-1", server.Config.Handler)
	expectBoot(portOf(t, server.URL), bootExpectation{id: "boot-1"})
	if err := WaitReady(context.Background(), portOf(t, server.URL), 20*time.Second); err != nil {
		t.Fatal(err)
	}
	if hits.Load() < 3 {
		t.Fatalf("returned after %d answers", hits.Load())
	}
}

func TestWaitReadySaysWhatItLastSaw(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	expectBoot(portOf(t, server.URL), bootExpectation{anyAnswer: true})
	err := WaitReady(context.Background(), portOf(t, server.URL), 3*time.Second)
	if err == nil {
		t.Fatal("a 404 is not ready")
	}
	if !strings.Contains(err.Error(), "404") {
		t.Fatalf("the error hides what answered: %v", err)
	}
}

func TestAppURLIsLoopbackOnly(t *testing.T) {
	if got := AppURL("8765", LangEN); got != "http://127.0.0.1:8765/app/?lang=en" {
		t.Fatalf("got %q", got)
	}
}

// The app is opened in the language the launcher is speaking, not in the browser's: an operator who
// chose Russian in the corner of the launcher gets a Russian app on an English machine.
func TestAppURLCarriesTheLanguage(t *testing.T) {
	if got := AppURL("8765", LangRU); got != "http://127.0.0.1:8765/app/?lang=ru" {
		t.Fatalf("got %q", got)
	}
}

func portOf(t *testing.T, raw string) string {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Port()
}

// Apply goes through the supervisor, which preflights the commit and keeps the running code when the
// checks do not pass. A stop and a start would come back on whatever the checkout holds with nothing
// having looked at it, which is what the button used to do.
func TestApplyAsksTheSupervisorRatherThanRestartingTheContainers(t *testing.T) {
	args := supervisorRestartArgs()
	want := []string{"exec", "-T", "daedalus", containerPython, "-m", "daedalus", "self", "restart", "--reason", "the launcher's Apply"}
	if len(args) != len(want) {
		t.Fatalf("got %v", args)
	}
	for i := range want {
		if args[i] != want[i] {
			t.Fatalf("got %v, want %v", args, want)
		}
	}
}

// bootAnswer is what the app does with DAEDALUS_BOOT_ID: it echoes it on every response.
func bootAnswer(id string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set(bootHeader, id)
		next.ServeHTTP(w, r)
	})
}
