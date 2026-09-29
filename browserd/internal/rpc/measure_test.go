//go:build unix

package rpc_test

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/page"
)

// bigPage is a synthetic long page of product cards, in the ways real shops build them: a card a
// script listens on, a link, a button, a heart that is only a span with a pointer cursor, and a
// line of text. n cards make about 13n elements, so n=500 stays under the size above which the
// daemon stops asking Chromium for listeners and n=1500 is past it.
func bigPage(n int) string {
	var b strings.Builder
	b.WriteString(`<!doctype html><title>Big</title><style>.card{border:1px solid #ccc;margin:4px;padding:4px}.heart{cursor:pointer}</style><main><h1>Everything</h1><ul>`)
	for i := range n {
		fmt.Fprintf(&b, `<li><div class="card" data-i="%d"><h3>Item %d</h3><p>A thing of some use, number %d, in <b>stock</b>.</p>`+
			`<span class="heart">Save</span> <a href="/still?i=%d">Details</a> <button>Add</button></div></li>`, i, i, i, i)
	}
	b.WriteString(`</ul></main><script>for (const c of document.querySelectorAll(".card")) c.addEventListener("click", () => {});</script>`)
	return b.String()
}

// TestMeasureSnapshots measures the snapshot on the golden pages, synthetic long pages and, with
// BROWSERD_MEASURE_DIR naming a directory of saved pages, real ones: the time of the first snapshot
// (the world made, the listeners asked for), the median of five after it, the size and the refs.
// It measures, it does not judge: the numbers go into the unit's notes. Opt in with
// BROWSERD_MEASURE=1; the machine's load changes them, so the load is printed beside them.
func TestMeasureSnapshots(t *testing.T) {
	if os.Getenv("BROWSERD_MEASURE") == "" {
		t.Skip("set BROWSERD_MEASURE=1 to measure")
	}
	h := start(t, nil)
	pages := []string{"/site/shop/index.html", "/site/shop/checkout.html", "/site/widgets.html"}
	big := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		n := 500
		fmt.Sscan(r.URL.Query().Get("n"), &n)
		fmt.Fprint(w, bigPage(n))
	}))
	t.Cleanup(big.Close)
	ports := [][2]int{{port(h.site.Listener), port(h.site.Listener)}, {port(big.Listener), port(big.Listener)}}
	urls := []string{}
	for _, p := range pages {
		urls = append(urls, h.site.URL+p)
	}
	urls = append(urls, big.URL+"/?n=500", big.URL+"/?n=1500")
	if dir := os.Getenv("BROWSERD_MEASURE_DIR"); dir != "" {
		saved := httptest.NewServer(http.FileServer(http.Dir(dir)))
		t.Cleanup(saved.Close)
		ports = append(ports, [2]int{port(saved.Listener), port(saved.Listener)})
		names, _ := filepath.Glob(filepath.Join(dir, "*.html"))
		for _, n := range names {
			urls = append(urls, saved.URL+"/"+filepath.Base(n))
		}
	}
	h.must("net.configure", map[string]any{"services_ports": ports}, nil)
	load, _ := os.ReadFile("/proc/loadavg")
	t.Logf("load: %s", strings.TrimSpace(string(load)))
	for i, u := range urls {
		o := h.open(fmt.Sprintf("m%d", i), "project-a", "")
		h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": u, "timeout_ms": 60000}, nil)
		for _, view := range []string{"", "viewport"} {
			params := map[string]any{"tab_id": o.Tab.ID}
			if view != "" {
				params["view"] = view
			}
			var s page.Snapshot
			var times []time.Duration
			for range 6 {
				began := time.Now()
				if err := h.call("page.snapshot", params, &s); err != nil {
					if view != "" {
						break // a daemon without the view mode
					}
					t.Fatal(err)
				}
				times = append(times, time.Since(began))
			}
			if len(times) == 0 {
				continue
			}
			first := times[0]
			rest := slices.Clone(times[1:])
			slices.Sort(rest)
			name := u[strings.LastIndex(u, "/")+1:]
			t.Logf("%-28s view=%-8s first %4dms median %4dms  %6d chars  %4d refs  truncated=%v", name, orPage(view),
				first.Milliseconds(), rest[len(rest)/2].Milliseconds(), len(s.Text), s.Refs, s.Truncated)
		}
		h.must("group.close", map[string]any{"group_id": fmt.Sprintf("m%d", i)}, nil)
	}
}

func orPage(v string) string {
	if v == "" {
		return "page"
	}
	return v
}

func port(l net.Listener) int { return l.Addr().(*net.TCPAddr).Port }
