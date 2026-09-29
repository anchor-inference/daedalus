package rpc_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/record"
	"github.com/ascorblack/daedalus/browserd/internal/rpc"
	"github.com/ascorblack/daedalus/browserd/internal/view"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// needChromium skips a test that drives a real browser where there is none, and fails it where the
// gate says there must be one (BROWSERD_REQUIRE_CHROMIUM=1, set in the gate's image).
func needChromium(t *testing.T) {
	t.Helper()
	if _, ok := chrome.Find(""); ok {
		return
	}
	if os.Getenv("BROWSERD_REQUIRE_CHROMIUM") != "" {
		t.Fatal("no Chromium, and the gate requires one")
	}
	t.Skip("no Chromium here; the gate image has one")
}

// fixture is the local site the tests browse. Nothing leaves the machine: every page is served here
// and no page links outside it.
func fixture(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	page := func(path, title, body string) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprintf(w, "<!doctype html><html><head><title>%s</title></head><body style=\"margin:0;font:16px sans-serif\">%s</body></html>", title, body)
		})
	}
	page("/still", "Still", "<h1>Still</h1><p>"+strings.Repeat("Nothing moves on this page. ", 50)+"</p>")
	page("/anim", "Anim", `<div style="position:fixed;inset:0;background:conic-gradient(red,yellow,lime,aqua,blue,magenta,red);animation:s 2s linear infinite"></div><style>@keyframes s{to{transform:rotate(360deg)}}</style>`)
	page("/button", "Button", `<button id="b" style="position:absolute;left:100px;top:100px;width:200px;height:60px" onclick="document.title='clicked '+event.isTrusted">Press</button>`)
	page("/popup", "Popup", `<a id="a" href="/still" target="_blank" style="position:absolute;left:100px;top:100px;width:200px;height:60px;display:block">open</a>`)
	page("/confirm-writes", "Confirm", `<button onclick="if (confirm('Reset?')) document.getElementById('m').textContent = 'Filters reset.'">Reset</button><p id="m">Four filters.</p>`)
	page("/dialog", "Dialog", `<button id="b" style="position:absolute;left:100px;top:100px;width:200px;height:60px" onclick="document.title=confirm('Leave?')?'yes':'no'">Ask</button>`)
	mux.Handle("/site/", http.StripPrefix("/site/", http.FileServer(http.Dir("testdata/site"))))
	mux.HandleFunc("/file.txt", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.Header().Set("Content-Disposition", `attachment; filename="report.txt"`)
		fmt.Fprint(w, "the quarterly report\n")
	})
	mux.HandleFunc("/private", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("WWW-Authenticate", `Basic realm="fixture"`)
		w.WriteHeader(http.StatusUnauthorized)
	})
	mux.HandleFunc("/secrets", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, secretsPage())
	})
	mux.HandleFunc("/secrets-frame", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<input type="password" value="SECRET-frame-pw"><input autocomplete="cc-number" value="SECRET-frame-cc">`)
	})
	mux.HandleFunc("/link", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!doctype html><title>Link</title><a href="%s" style="display:block;width:200px;height:40px">onward</a>`, html.EscapeString(r.URL.Query().Get("to")))
	})
	mux.HandleFunc("/go", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, r.URL.Query().Get("to"), http.StatusFound)
	})
	// Pages of three sites for the frames tests, told apart by the name they are asked for (the
	// tests' wall sends every name to this fixture).
	xsite := func(path, body string) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, "<!doctype html><html><head><meta charset=\"utf-8\"></head><body style=\"margin:0;font:16px sans-serif\">"+body+"</body></html>")
		})
	}
	xsite("/xsite/outer", `<title>Outer</title><h1>Checkout</h1>`+
		`<iframe src="http://pay.example/xsite/pay" title="Payment" style="width:600px;height:320px;border:0"></iframe>`+
		`<iframe src="http://cdn.shop.example/xsite/widget" title="Widget" style="width:400px;height:80px;border:0"></iframe>`)
	xsite("/xsite/pay", `<title>Pay</title><form>`+
		`<p><label>Name on card <input autocomplete="cc-name" value="SECRET-cc-name" style="width:300px;height:30px"></label></p>`+
		`<p><label>Card <input autocomplete="cc-number" value="SECRET-cc-number" style="width:300px;height:30px"></label></p>`+
		`<p><label>Note <input id="note"></label></p>`+
		`<button type="button" style="width:120px;height:30px" onclick="document.getElementById('st').textContent='paid'">Pay now</button>`+
		`<p id="st">unpaid</p></form>`+
		`<iframe src="http://deep.example/xsite/deep" title="Deep" style="width:300px;height:50px;border:0"></iframe>`)
	xsite("/xsite/deep", `<title>Deep</title><button onclick="this.textContent='deep clicked'">Deep button</button>`)
	// A frame placed as a real page places one: centred, with a border, holding small controls, each
	// page with the browser's own margins.
	whole := func(path, doc string) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, doc)
		})
	}
	whole("/xsite/centred", `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Centred</title>`+
		`<style>body{font-family:sans-serif;max-width:900px;margin:2em auto}iframe{width:100%;height:260px;border:1px solid #ccc}</style></head>`+
		`<body><h1>Contents insurance</h1><p>The best offer is shown by the insurer's own widget.</p>`+
		`<iframe src="http://pay.example/xsite/quote" title="Insurer quote"></iframe></body></html>`)
	whole("/xsite/quote", `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Quote</title><style>body{font-family:sans-serif}</style></head>`+
		`<body><h3>Oakline</h3><p>Contents cover.</p>`+
		`<label><input type="radio" name="term" value="monthly" checked> Pay monthly</label>`+
		`<label><input type="radio" name="term" value="yearly"> Pay yearly</label>`+
		`<p><button id="save">Save</button></p><p id="m"></p>`+
		`<script>document.getElementById('save').onclick = () => { document.getElementById('m').textContent = 'saved ' + document.querySelector('input:checked').value; };</script>`+
		`</body></html>`)
	xsite("/xsite/widget", `<title>Widget</title><button onclick="this.textContent='widget clicked'">Widget button</button>`)
	// A text a pattern of nested repeats takes forever over, for the search's time limit.
	page("/backtrack", "Backtrack", "<p>"+strings.Repeat("a", 40)+"!</p>")
	mux.HandleFunc("/after-login", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "<title>Signed in</title>signed in")
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

type harness struct {
	t       *testing.T
	client  *clienttest.Client
	manager *browser.Manager
	site    *httptest.Server
	stop    chan struct{}
	evlog   *events.Log
}

// start runs the daemon in this process on a temporary run directory, with limits changed by edit.
func start(t *testing.T, edit func(*config.Limits)) *harness {
	return startWith(t, edit, nil)
}

// startWith is start with Chromium switches of the test's own.
func startWith(t *testing.T, edit func(*config.Limits), args []string) *harness {
	t.Helper()
	return startWall(t, edit, args, nil)
}

// startWall is startWith with a wall of the test's own making (a fake resolver, a redirect to the
// fixture), for a test that needs names the wall judges as the internet.
func startWall(t *testing.T, edit func(*config.Limits), args []string, makeWall func(site *httptest.Server, publish func(netwall.Egress)) *netwall.Browsers) *harness {
	t.Helper()
	needChromium(t)
	dir := t.TempDir()
	cfg := &config.Config{Env: "test", RunDir: filepath.Join(dir, "run"), StateDir: filepath.Join(dir, "state"),
		Listen: "unix", Limits: config.DefaultLimits(), Chromium: config.Chromium{Args: args}}
	if edit != nil {
		edit(&cfg.Limits)
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ep, err := server.Prepare(cfg.RunDir, "unix", "browserd")
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	if os.Getenv("BROWSERD_TEST_LOG") != "" {
		log = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
	}
	evlog := events.NewLog(config.EventRingSize)
	deb := events.NewDebouncer(evlog, map[string]events.Policy{"tab.updated": {Window: 250 * time.Millisecond, Coalesce: true}})
	var hub *view.Hub
	// Every test browses through the network wall, as the daemon does, with the fixture's port as
	// the services range: the rest of this machine is refused.
	site := fixture(t)
	publish := func(e netwall.Egress) { deb.Publish("egress", "", e) }
	var wall *netwall.Browsers
	if makeWall != nil {
		wall = makeWall(site, publish)
	} else {
		wall = netwall.NewBrowsers(netwall.New(netwall.Options{Events: publish}))
		sitePort := site.Listener.Addr().(*net.TCPAddr).Port
		if err := wall.Wall.Configure(netwall.Config{ServicesPorts: [][2]int{{sitePort, sitePort}}}); err != nil {
			t.Fatal(err)
		}
	}
	m := browser.New(browser.Deps{Config: cfg, Log: log, Events: deb, Wall: wall, Busy: func(b *browser.Browser) bool { return hub != nil && hub.Busy(b) }})
	hub = view.New(m, evlog, log)
	model := page.New(m, cfg, log)
	hub.HumanInput = model.HumanInput
	m.Listen(hub)
	m.Listen(model)
	recorder := &record.Recorder{Store: record.Open(filepath.Join(cfg.StateDir, "recordings")), Log: log, Groups: m.Group,
		Shoot: func(ctx context.Context, t *browser.Tab) ([]byte, int, int, error) {
			shot, err := model.Screenshot(ctx, t, page.ScreenshotParams{TabID: t.ID, MaxWidth: 1280, Format: "jpeg", Quality: 50})
			if err != nil {
				return nil, 0, 0, err
			}
			data, err := base64.StdEncoding.DecodeString(shot.Data)
			return data, shot.Width, shot.Height, err
		},
		Limits: func() (int64, time.Duration) {
			l := m.Limits()
			return l.RecordMaxBytes, time.Duration(l.RecordRetentionMs) * time.Millisecond
		}}
	model.AfterAction = recorder.AfterAction
	d := &rpc.Daemon{Config: cfg, Instance: "test", StartedAt: time.Now(), Manager: m, Hub: hub, Page: model, Events: evlog, Log: log, Net: wall, Record: recorder}
	srv := server.New(ep.Token, log, d.Hello)
	d.Register(srv)
	h := &harness{t: t, manager: m, site: site, stop: make(chan struct{}), evlog: evlog}
	go hub.Run(h.stop)
	go m.RunTitles(h.stop)
	go recorder.Run(h.stop)
	go func() { _ = srv.Serve(ep.Listener) }()
	c, err := clienttest.Dial(cfg.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	h.client = c
	t.Cleanup(func() {
		c.Close()
		close(h.stop)
		m.Shutdown()
		ep.Release()
		_ = ep.Listener.Close()
		srv.Close()
	})
	return h
}

func (h *harness) call(method string, params, result any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	return h.client.Call(ctx, method, params, result)
}

func (h *harness) must(method string, params, result any) {
	h.t.Helper()
	if err := h.call(method, params, result); err != nil {
		h.t.Fatalf("%s: %v", method, err)
	}
}

// code is the daemon's error code of err, or 0.
func code(err error) int {
	var we *protowire.Error
	if errors.As(err, &we) {
		return we.Code
	}
	return 0
}

type opened struct {
	Group struct {
		ID string `json:"id"`
	} `json:"group"`
	Tab struct {
		ID    string `json:"id"`
		URL   string `json:"url"`
		Title string `json:"title"`
	} `json:"tab"`
	Created bool `json:"created"`
}

func (h *harness) open(group, profile, path string) opened {
	h.t.Helper()
	var o opened
	params := map[string]any{"group_id": group, "profile": profile}
	if path != "" {
		params["url"] = h.site.URL + path
	}
	h.must("browser.open", params, &o)
	return o
}

// view is one live view read straight off the socket's channel.
type viewConn struct {
	h       *harness
	channel uint32
	id      string
	frames  chan wire.Frame
	events  chan map[string]any
}

// attach opens a view and sends its ATTACH. Every frame the channel carries is decoded and sorted
// into frames and events; a test reads what it needs.
func (h *harness) attach(group string, att wire.Attach) *viewConn {
	h.t.Helper()
	var r struct {
		Channel  uint32 `json:"channel"`
		ClientID string `json:"client_id"`
	}
	h.must("view.attach", map[string]any{"group_id": group, "client": map[string]any{"kind": "human", "label": "test"}}, &r)
	v := &viewConn{h: h, channel: r.Channel, id: r.ClientID, frames: make(chan wire.Frame, 1024), events: make(chan map[string]any, 1024)}
	demux(h.client).add(r.Channel, v)
	b, err := wire.EncodeJSON(wire.TypeAttach, att)
	if err != nil {
		h.t.Fatal(err)
	}
	if err := h.client.Send(r.Channel, b); err != nil {
		h.t.Fatal(err)
	}
	return v
}

func (v *viewConn) ack(n uint32) {
	if err := v.h.client.Send(v.channel, wire.EncodeAck(n)); err != nil {
		v.h.t.Fatal(err)
	}
}

func (v *viewConn) input(in map[string]any) {
	b, _ := json.Marshal(in)
	if err := v.h.client.Send(v.channel, append([]byte{wire.TypeInput}, b...)); err != nil {
		v.h.t.Fatal(err)
	}
}

// frame waits for the next frame.
func (v *viewConn) frame(timeout time.Duration) (wire.Frame, bool) {
	select {
	case f := <-v.frames:
		return f, true
	case <-time.After(timeout):
		return wire.Frame{}, false
	}
}

// waitEvent waits for an event of type typ that passes ok.
func (v *viewConn) waitEvent(typ string, timeout time.Duration, ok func(map[string]any) bool) map[string]any {
	v.h.t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case e := <-v.events:
			if e["type"] == typ && (ok == nil || ok(e)) {
				return e
			}
		case <-deadline:
			v.h.t.Fatalf("no %s event within %s", typ, timeout)
			return nil
		}
	}
}

// demuxer reads the client's channel frames once and hands each channel's to its view.
type demuxer struct {
	views chan registration
}

type registration struct {
	channel uint32
	v       *viewConn
}

var demuxers = map[*clienttest.Client]*demuxer{}

func demux(c *clienttest.Client) *demuxer {
	if d := demuxers[c]; d != nil {
		return d
	}
	d := &demuxer{views: make(chan registration, 16)}
	demuxers[c] = d
	go func() {
		views := map[uint32]*viewConn{}
		var early []protowire.Frame
		for {
			select {
			case r := <-d.views:
				views[r.channel] = r.v
				rest := early[:0]
				for _, f := range early {
					if f.Channel == r.channel {
						deliver(r.v, f.Payload)
					} else {
						rest = append(rest, f)
					}
				}
				early = rest
			case f, ok := <-c.Frames:
				if !ok {
					return
				}
				if v := views[f.Channel]; v != nil {
					deliver(v, f.Payload)
				} else {
					early = append(early, f)
				}
			case <-c.Closed():
				return
			}
		}
	}()
	return d
}

func (d *demuxer) add(channel uint32, v *viewConn) { d.views <- registration{channel, v} }

func deliver(v *viewConn, payload []byte) {
	if len(payload) == 0 {
		close(v.events)
		return
	}
	f, err := wire.Decode(payload)
	if err != nil {
		return
	}
	switch f.Type {
	case wire.TypeFrame:
		select {
		case v.frames <- f:
		default:
		}
	case wire.TypeEvent:
		var e map[string]any
		if json.Unmarshal(f.JSON, &e) == nil {
			select {
			case v.events <- e:
			default:
			}
		}
	}
}

// secretFields are the field kinds whose values an agent never reads, each with a value it must not
// see. The spellings vary as pages vary them.
var secretFields = []string{
	`<input type="password" value="%s">`,
	`<input type="PASSWORD" value="%s">`,
	`<input autocomplete="current-password" value="%s">`,
	`<input autocomplete="section-login Current-Password" value="%s">`,
	`<input autocomplete="new-password" value="%s">`,
	`<input autocomplete="one-time-code" value="%s">`,
	`<input autocomplete="cc-number" value="%s">`,
	`<input autocomplete="cc-exp" value="%s">`,
	`<input autocomplete="cc-csc" value="%s">`,
	`<input autocomplete="billing cc-name" value="%s">`,
	`<textarea autocomplete="one-time-code">%s</textarea>`,
	`<label>Pin <input type="password" value="%s"></label>`,
}

func secretsPage() string {
	var b strings.Builder
	b.WriteString("<!doctype html><title>Secrets</title><main><h1>Fields</h1><form>")
	for i, f := range secretFields {
		fmt.Fprintf(&b, "<p>"+f+"</p>", fmt.Sprintf("SECRET-%d", i))
	}
	b.WriteString(`<button type="button" onclick="document.getElementById('x').textContent='changed'">Change</button><p id="x">same</p></form>`)
	b.WriteString(`<div id="host"></div><iframe src="/secrets-frame" title="payment"></iframe></main><script>
document.getElementById("host").attachShadow({mode: "open"}).innerHTML = '<input type="password" value="SECRET-shadow">';
</script>`)
	return b.String()
}
