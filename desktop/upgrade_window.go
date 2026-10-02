package main

import (
	"context"
	"encoding/json"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// upgradeWindow is what the operator sees while an upgrade the app started runs. The application's
// own window has to close for it — its files are among those replaced — and the upgrade used to run
// with nothing on the screen at all until the app came back, minutes later or not at all. So the
// upgrade serves a small page of its own on loopback and opens it in an application-mode browser
// window: the steps, ticked off as they happen, and at the end either "done" (the page closes
// itself as the app reopens) or what went wrong and that the previous version is back.
//
// The window's browser profile is in the local state folder, never the data folder: the upgrade
// refuses to go on while anything holds a file under the data folder, and the browser would.
type upgradeWindow struct {
	mu       sync.Mutex
	lang     Lang
	to       string
	step     string
	done     []string
	state    string // running, done or failed
	message  string
	lines    []string
	finalSet bool
	seen     bool
	server   *http.Server
}

// upgradeSteps are the steps the page lists, in order.
var upgradeSteps = []string{"download", "verify", "prepare", "stop", "backup", "replace", "start"}

// openUpgradeWindow starts the page and opens it. A machine with no Chromium-family browser gets the
// page in its default browser instead; one where nothing opens still has the log. Never nil.
func openUpgradeWindow(p Paths, lang Lang) *upgradeWindow {
	w, url := serveUpgradeWindow(lang)
	if url == "" {
		return w
	}
	if err := openProgressWindow(p, url); err != nil {
		_ = OpenBrowser(context.Background(), url)
	}
	return w
}

// serveUpgradeWindow starts the page's server and returns its address, empty when it could not.
func serveUpgradeWindow(lang Lang) (*upgradeWindow, string) {
	w := &upgradeWindow{lang: lang, state: "running"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return w, ""
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", w.page)
	mux.HandleFunc("/progress", w.progress)
	mux.Handle("/assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(mustSub()))))
	w.server = &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = w.server.Serve(listener) }()
	return w, "http://" + listener.Addr().String() + "/"
}

// openProgressWindow is OpenAppWindow with a small window and a profile of its own outside the data
// folder.
func openProgressWindow(p Paths, url string) error {
	browser := findChromium(runtime.GOOS, homeDir(), exec.LookPath, runnableFile)
	if browser == "" {
		return os.ErrNotExist
	}
	profile := filepath.Join(p.Local, "upgrade-window")
	cmd := exec.Command(browser, "--app="+url, "--user-data-dir="+profile, "--window-size=540,680", "--no-first-run", "--no-default-browser-check")
	if err := cmd.Start(); err != nil {
		return err
	}
	go func() { _ = cmd.Wait() }()
	return nil
}

// Step marks the start of a step; every step before it is done.
func (w *upgradeWindow) Step(key string) {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.done = w.done[:0]
	for _, step := range upgradeSteps {
		if step == key {
			break
		}
		w.done = append(w.done, step)
	}
	w.step = key
}

// SetTarget names the version being installed, for the page's title.
func (w *upgradeWindow) SetTarget(tag string) {
	if w == nil {
		return
	}
	w.mu.Lock()
	w.to = strings.TrimPrefix(tag, "desktop-v")
	w.mu.Unlock()
}

// Write takes the upgrade's own output, so the page can show its last lines under the steps.
func (w *upgradeWindow) Write(b []byte) (int, error) {
	if w == nil {
		return len(b), nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, line := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			w.lines = append(w.lines, line)
		}
	}
	if len(w.lines) > 6 {
		w.lines = w.lines[len(w.lines)-6:]
	}
	return len(b), nil
}

// Finish records how the upgrade ended and waits, briefly, for the page to have read it: the
// process ends right after, and with it the page's server.
func (w *upgradeWindow) Finish(err error) {
	if w == nil {
		return
	}
	w.mu.Lock()
	if err != nil {
		w.state, w.message = "failed", err.Error()
	} else {
		w.state = "done"
		w.done = append([]string(nil), upgradeSteps...)
		w.step = ""
	}
	w.finalSet = true
	w.mu.Unlock()
	for deadline := time.Now().Add(6 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		w.mu.Lock()
		seen := w.seen
		w.mu.Unlock()
		if seen {
			break
		}
	}
	if w.server != nil {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		_ = w.server.Shutdown(ctx)
		cancel()
	}
}

type upgradeProgress struct {
	To      string   `json:"to"`
	Step    string   `json:"step"`
	Done    []string `json:"done"`
	Steps   []string `json:"steps"`
	State   string   `json:"state"`
	Message string   `json:"message"`
	Lines   []string `json:"lines"`
}

func (w *upgradeWindow) progress(rw http.ResponseWriter, _ *http.Request) {
	w.mu.Lock()
	body := upgradeProgress{To: w.to, Step: w.step, Done: append([]string(nil), w.done...), Steps: upgradeSteps, State: w.state, Message: w.message, Lines: append([]string(nil), w.lines...)}
	if w.finalSet {
		w.seen = true
	}
	w.mu.Unlock()
	rw.Header().Set("Content-Type", "application/json")
	rw.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(rw).Encode(body)
}

func (w *upgradeWindow) page(rw http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(rw, r)
		return
	}
	page, err := fs.ReadFile(uiFiles, "ui/assets/upgrade.html")
	if err != nil {
		http.Error(rw, err.Error(), http.StatusInternalServerError)
		return
	}
	words := map[string]string{}
	for key := range messages[LangEN] {
		if strings.HasPrefix(key, "upgradewin.") {
			words[strings.TrimPrefix(key, "upgradewin.")] = Translate(w.lang, key)
		}
	}
	encoded, _ := json.Marshal(words)
	html := strings.ReplaceAll(string(page), "{{LANG}}", string(w.lang))
	html = strings.ReplaceAll(html, "{{WORDS}}", strings.ReplaceAll(string(encoded), "</", "<\\/"))
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
	rw.Header().Set("Cache-Control", "no-store")
	_, _ = rw.Write([]byte(html))
}
