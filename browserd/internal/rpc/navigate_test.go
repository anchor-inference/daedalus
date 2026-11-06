package rpc_test

import (
	"strings"
	"testing"
	"time"
)

// navigated is a navigation's reply as the host reads it.
type navigated struct {
	URL       string `json:"url"`
	Title     string `json:"title"`
	Error     string `json:"error"`
	Loading   bool   `json:"loading"`
	WaitingOn []struct {
		Host string `json:"host"`
		Type string `json:"type"`
		MS   int64  `json:"ms"`
	} `json:"waiting_on"`
	Timing *struct {
		AnsweredMS int64 `json:"answered_ms"`
		CommitMS   int64 `json:"commit_ms"`
		ParsedMS   int64 `json:"parsed_ms"`
		LoadedMS   int64 `json:"loaded_ms"`
		WaitedMS   int64 `json:"waited_ms"`
	} `json:"timing"`
}

// A navigation ends well inside this, and the ones that used to run out their timeout took 30 s.
const notTheTimeout = 12 * time.Second

func (h *harness) navigate(tab, path string) (navigated, time.Duration) {
	h.t.Helper()
	var r navigated
	began := time.Now()
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + path, "timeout_ms": 30000}, &r)
	return r, time.Since(began)
}

// A frame that never answers holds the page's load for ever, and the page's words were there all
// along: on a classifieds site full of advertising this held the agent for the whole 30 s, several
// calls in a row, on pages it read in half a second once the wait gave up.
func TestAParsedPageIsNotHeldByAFrameThatNeverAnswers(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	r, took := h.navigate(o.Tab.ID, "/held")
	if took > notTheTimeout {
		t.Fatalf("the navigation took %v", took)
	}
	if r.Error != "" || !r.Loading {
		t.Fatalf("a page that is there to read and has not finished: %+v", r)
	}
	if r.Timing == nil || r.Timing.ParsedMS == 0 || r.Timing.LoadedMS != 0 || r.Timing.WaitedMS < r.Timing.ParsedMS {
		t.Fatalf("the time it took: %+v", r.Timing)
	}
	if len(r.WaitingOn) == 0 || r.WaitingOn[0].Host == "" || r.WaitingOn[0].MS <= 0 {
		t.Fatalf("what it still waits on: %+v", r.WaitingOn)
	}
	var text struct {
		Text string `json:"text"`
	}
	h.must("page.text", map[string]any{"tab_id": o.Tab.ID}, &text)
	if !strings.Contains(text.Text, "The words of the page") {
		t.Fatalf("the page was not read: %q", text.Text)
	}
}

// A script that never answers stops the document being parsed, and then there is nothing to read
// yet: the navigation waits out its timeout and says so, as it always did.
func TestAPageThatIsNotParsedStillRunsOutItsTimeout(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	var r navigated
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/held/script", "timeout_ms": 2000}, &r)
	if r.Loading || !strings.Contains(r.Error, "still loading after 2s") {
		t.Fatalf("a page with nothing to read yet: %+v", r)
	}
	if r.Timing == nil || r.Timing.ParsedMS != 0 {
		t.Fatalf("the time it took: %+v", r.Timing)
	}
}

// A page that sends the tab on replaces the document the navigation is waiting for, whose load then
// never comes: every such page cost the whole timeout, with the page long since loaded.
func TestAPageThatMovesOnEndsTheWaitOnWhereItWent(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	for _, path := range []string{"/moves", "/moves/twice"} {
		r, took := h.navigate(o.Tab.ID, path)
		if took > notTheTimeout {
			t.Fatalf("%s took %v", path, took)
		}
		if r.Error != "" || r.Loading || !strings.HasSuffix(r.URL, "/moved") {
			t.Fatalf("%s: %+v", path, r)
		}
	}
}

// The ordinary page: its load is what ends the wait, and the split says so.
func TestAPageThatLoadsSaysWhereTheTimeWent(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	r, _ := h.navigate(o.Tab.ID, "/button")
	if r.Error != "" || r.Loading || len(r.WaitingOn) != 0 {
		t.Fatalf("a page that loaded: %+v", r)
	}
	s := r.Timing
	if s == nil || s.CommitMS == 0 || s.ParsedMS < s.CommitMS || s.LoadedMS < s.ParsedMS || s.WaitedMS < s.LoadedMS {
		t.Fatalf("the time it took: %+v", s)
	}
}

// A reload is held by the same frame as the first visit.
func TestAReloadIsNotHeldByAFrameThatNeverAnswers(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	if r, _ := h.navigate(o.Tab.ID, "/held"); !r.Loading {
		t.Fatalf("the first visit: %+v", r)
	}
	var r navigated
	began := time.Now()
	h.must("page.reload", map[string]any{"tab_id": o.Tab.ID}, &r)
	if took := time.Since(began); took > notTheTimeout {
		t.Fatalf("the reload took %v", took)
	}
	if r.Error != "" || !r.Loading || r.Timing == nil || r.Timing.ParsedMS == 0 {
		t.Fatalf("the reload: %+v", r)
	}
}
