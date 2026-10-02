package main

import (
	"context"
	"crypto/subtle"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io/fs"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

//go:embed ui
var uiFiles embed.FS

var uiTemplates = template.Must(template.ParseFS(uiFiles, "ui/*.html"))

// Server is the launcher's own page: the questions on a first run, and the status with the same
// buttons the command line offers afterwards. It listens on the loopback address only — it starts
// and stops containers, and nothing outside this machine has any business doing that.
type Server struct {
	app  *App
	port int

	// csrf is minted once per process and has to come back with anything that changes something.
	// Loopback is not a boundary: every page the operator opens can reach this port too.
	csrf    string
	csrfErr error

	http *http.Server

	// focus is what a second launch asks this one to do: come to the front, at the link it was
	// opened with. It is set by whoever owns the window; without one it opens the app in a browser.
	focus func(ctx context.Context, url string)

	once    sync.Once
	saved   chan struct{} // closed when the setup form has been written
	waiting atomic.Bool   // something is blocked in WaitForSetup, so a post starts the stack there

	helpers setupHelpers
	after   afterSetup
}

const defaultPort = 8770

// csrfHeader carries the token on an action. A custom header cannot be set on a cross-site form
// post and turns a fetch into a preflighted request, so a page that is not this one cannot send it
// at all — the token is what stops a page that somehow can.
const csrfHeader = "X-Daedalus-Desktop"

func NewServer(app *App, port int) *Server {
	token, err := randomSecret()
	return &Server{app: app, port: port, saved: make(chan struct{}), csrf: token, csrfErr: err, helpers: newSetupHelpers()}
}

// URL is where the operator reaches the launcher page.
func (s *Server) URL() string { return fmt.Sprintf("http://127.0.0.1:%d/", s.port) }

// Start begins serving. The listener is opened before returning, so a caller that opens a browser
// straight afterwards finds the page there.
func (s *Server) Start() error {
	if s.csrfErr != nil {
		return fmt.Errorf("the launcher page cannot tell its own requests apart: %w", s.csrfErr)
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", s.port))
	if err != nil {
		return fmt.Errorf("the launcher page cannot listen on port %d: %w (another launcher may be running; --port picks another)", s.port, err)
	}
	// Port 0 asks the operating system for a free port; the page's own address has to be the one
	// it actually got.
	if addr, ok := listener.Addr().(*net.TCPAddr); ok {
		s.port = addr.Port
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/setup", s.handleSetup)
	mux.HandleFunc("/progress", s.handleProgress)
	mux.HandleFunc("/status", s.handleStatusPage)
	mux.HandleFunc("/api/lang", s.handleLang)
	mux.HandleFunc("/api/setup/detect", s.handleSetupDetect)
	mux.HandleFunc("/api/setup/endpoint", s.handleSetupEndpoint)
	mux.HandleFunc("/api/autostart", s.handleAutostart)
	mux.HandleFunc("/api/status", s.handleStatus)
	mux.HandleFunc("/api/action/", s.handleAction)
	mux.HandleFunc("/api/jobs/", s.handleJob)
	mux.HandleFunc("/focus", s.handleFocus)
	mux.Handle("/assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(mustSub()))))
	s.http = &http.Server{Handler: s.sameOrigin(mux), ReadHeaderTimeout: 10 * time.Second}
	// The installation now has an owner, and a second launch reads this to find it.
	if err := WriteInstance(s.app.paths, s.port, s.csrf); err != nil {
		fmt.Println("a second launch will not find this one:", err)
	}
	go func() {
		if err := s.http.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Println("the launcher page stopped:", err)
		}
	}()
	return nil
}

func (s *Server) Stop(ctx context.Context) {
	RemoveInstance(s.app.paths)
	if s.http != nil {
		_ = s.http.Shutdown(ctx)
	}
}

// OnFocus records what to do when a second launch asks for the front.
func (s *Server) OnFocus(focus func(ctx context.Context, url string)) { s.focus = focus }

// Port is the port the page ended up on, which is not the one that was asked for when that one was
// taken or when zero asked the operating system to choose.
func (s *Server) Port() int { return s.port }

// Token is the secret this process minted. It is handed to a second launch through the file in the
// data folder and never leaves the machine.
func (s *Server) Token() string { return s.csrf }

// handleFocus is the whole of the single-instance protocol: a second launch of the launcher posts
// here instead of starting anything, with the deep link it was opened with when it has one, and
// exits. It answers 200 only when it is really us — the token is per process, and the file that
// carries it is written by this process and readable only by its owner.
func (s *Server) handleFocus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post to focus the launcher", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	var body struct {
		URL string `json:"url"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	target := ""
	if IsDeepLink(body.URL) {
		target = DeepLinkTarget(body.URL, AppURL(APIPort(s.app.paths), s.app.Lang()))
	}
	if s.focus != nil {
		// The asking launcher is waiting for an answer, and showing a window is the running
		// launcher's own business.
		go s.focus(context.Background(), target)
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"focused":true}`))
}

// WaitForSetup blocks until the form has been submitted, or the context ends.
func (s *Server) WaitForSetup(ctx context.Context) error {
	s.waiting.Store(true)
	defer s.waiting.Store(false)
	select {
	case <-s.saved:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// sameOrigin refuses anything that changes state and did not come from this page. A form post and a
// bodyless fetch both travel cross-site without a preflight, so any page the operator happens to
// open could otherwise rewrite the provider keys and the Telegram identity the whole installation
// trusts, and then restart the stack under them. Three things have to agree: the browser's own
// account of where the request came from, the Origin it names when it names one, and the address
// the request was aimed at — the last of which is what keeps a hostname resolved to 127.0.0.1 from
// standing in for the loopback address.
func (s *Server) sameOrigin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet || r.Method == http.MethodHead {
			next.ServeHTTP(w, r)
			return
		}
		site := r.Header.Get("Sec-Fetch-Site")
		if site != "" && site != "same-origin" && site != "none" {
			refuse(w)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && !s.ownAddress(strings.TrimPrefix(origin, "http://")) {
			refuse(w)
			return
		}
		if !s.ownAddress(r.Host) {
			refuse(w)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ownAddress reports whether a host:port names this very page. Both spellings of the loopback
// address are accepted because a browser uses whichever one the operator typed.
func (s *Server) ownAddress(hostPort string) bool {
	return hostPort == fmt.Sprintf("127.0.0.1:%d", s.port) || hostPort == fmt.Sprintf("localhost:%d", s.port)
}

// hasToken reports whether a request carries the token this process minted. The comparison is
// constant time out of habit rather than out of need: the token never leaves the machine.
func (s *Server) hasToken(value string) bool {
	return subtle.ConstantTimeCompare([]byte(value), []byte(s.csrf)) == 1
}

func refuse(w http.ResponseWriter) {
	http.Error(w, "the launcher answers its own page only", http.StatusForbidden)
}

type pageData struct {
	Setup         Setup
	Status        Status
	DockerMissing bool
	LauncherURL   string
	CSRF          string
	// Mode is what the installation runs in, and Suggested is what the first run offers before the
	// operator has said. They differ only on a first run: afterwards the suggestion is the choice.
	Mode      string
	Suggested string
	Native    bool

	// Lang is the language this page is written in, and Messages is the same table the script on
	// the page reads, so a line the browser writes matches the ones Go wrote around it.
	Lang     Lang
	Messages template.JS
	// Steps is the start, drawn out, with the stage each one belongs to. The page renders it from
	// here rather than from the script, so it reads correctly before anything has answered.
	Steps []stepLine

	// Boot and Words are the wizard's: what it opens with, and its lines in both languages.
	Boot  template.JS
	Words template.JS
	// Autostart is whether the installation starts at login, for the status page's switch.
	Autostart bool
}

// stepLine is one row of the progress page's list.
type stepLine struct {
	Stage string
	Label string
	Done  bool
	Now   bool
}

// T is how a template asks for a line. Every sentence on every page comes through here.
func (d pageData) T(key string) string { return Translate(d.Lang, key) }

// handleIndex sends the operator to whichever of the three pages this installation is at: the
// questions when there are questions, the progress while it is being brought up or has never been
// brought up, and the status once it is running.
func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	if !s.app.paths.Configured() {
		http.Redirect(w, r, "/setup", http.StatusSeeOther)
		return
	}
	status := s.app.Status(r.Context())
	if status.Busy != "" || !status.Repos {
		s.render(w, r, "progress.html", status)
		return
	}
	s.render(w, r, "status.html", status)
}

// handleProgress is the waiting page on its own address. The page decides for itself when the wait
// is over and leaves for the status page; asking for it directly shows where a start has got to.
func (s *Server) handleProgress(w http.ResponseWriter, r *http.Request) {
	s.render(w, r, "progress.html", s.app.Status(r.Context()))
}

// handleStatusPage is the status page on its own address, which is where the progress page sends
// the operator when the installation is up.
func (s *Server) handleStatusPage(w http.ResponseWriter, r *http.Request) {
	s.render(w, r, "status.html", s.app.Status(r.Context()))
}

// handleLang records the language the operator picked in the corner of any launcher page. It is a
// change to the installation, so it carries the token like every other one, and it is written
// rather than kept in the session: the next start opens in the same language.
func (s *Server) handleLang(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post a language", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	var body struct {
		Lang string `json:"lang"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	if err := StoreLang(s.app.paths, ParseLang(body.Lang)); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"saved":true}`))
}

// handleSetup serves the wizard and takes its answers. The page posts with fetch and the token in
// the header as well as in the form, and is answered in JSON: it stays on its own last step, which
// follows the start from the status, instead of being sent somewhere by a redirect.
func (s *Server) handleSetup(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.render(w, r, "setup.html", s.app.Status(r.Context()))
		return
	}
	if err := r.ParseForm(); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if !s.hasToken(r.PostFormValue("csrf")) {
		refuse(w)
		return
	}
	setup, answers, refused := parseSetupForm(r.PostForm)
	if refused != nil {
		writeJSON(w, http.StatusBadRequest, refused)
		return
	}
	if lang := r.PostFormValue("lang"); lang != "" {
		// The language travels with the form as well as with the switch, so a first run that
		// was answered in Russian and never touched the switch is remembered as Russian.
		if err := StoreLang(s.app.paths, ParseLang(lang)); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
	}
	// The mode is stored beside the configuration rather than in it: it decides how the
	// launcher starts things, which is the launcher's business and not the agent's.
	if mode, err := ParseMode(r.PostFormValue("mode")); err == nil && mode != ModeUnset {
		if err := StoreMode(s.app.paths, mode); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.app.SetMode(mode)
	}
	if err := writeSetup(s.app.paths, setup, s.app.Mode(), s.app.log); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.app.log("configuration written to %s", s.app.paths.Env)
	s.applyLogin(answers["login"])
	s.after.set(wantedExtras(setup, s.app.Native(), s.app.paths), r.PostFormValue("handoff") == "1")
	if s.waiting.Load() {
		s.once.Do(func() { close(s.saved) })
	} else if r.PostFormValue("start") == "1" {
		// Changed from a running launcher: nothing is waiting for the answers, so the start that
		// puts them in force is this one's to make.
		go s.reconfigure(context.Background())
	}
	writeJSON(w, http.StatusOK, map[string]bool{"saved": true})
}

// clearedFields turns the ticked "remove" boxes into the set WriteSetup reads. An untouched field
// means "leave what is there alone", so emptying a value has to be asked for.
func clearedFields(values []string) map[string]bool {
	out := map[string]bool{}
	for _, name := range values {
		out[name] = true
	}
	return out
}

func (s *Server) render(w http.ResponseWriter, r *http.Request, name string, status Status) {
	lang := LangFor(s.app.paths, r.Header.Get("Accept-Language"))
	data := pageData{
		Setup:       CurrentSetup(s.app.paths),
		Status:      status,
		LauncherURL: s.URL(),
		CSRF:        s.csrf,
		Mode:        status.Mode,
		Native:      s.app.Native(),
		Lang:        lang,
		Messages:    template.JS(MessagesJSON(lang)),
		Steps:       stepLines(lang, status),
	}
	data.Suggested = status.Mode
	if data.Suggested == "" {
		data.Suggested = string(SuggestMode(r.Context()))
	}
	// Docker's absence is only news to an installation that means to use it.
	data.DockerMissing = status.Docker == "" && !s.app.Native()
	switch name {
	case "setup.html":
		data.Boot = bootJSON(s.setupBoot(r.Context(), lang, status, data.Suggested))
		data.Words = template.JS(HeroWordsJSON())
	case "status.html":
		data.Autostart, _ = s.helpers.autostart.Enabled()
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := uiTemplates.ExecuteTemplate(w, name, data); err != nil {
		fmt.Println("the page could not be rendered:", err)
	}
}

// stepLines turns the stage list into the rows the progress page draws, with everything before the
// current stage already ticked. A start that has not begun has no current stage and no ticks.
func stepLines(lang Lang, status Status) []stepLine {
	current := -1
	for i, stage := range status.Steps {
		if stage == status.Stage {
			current = i
		}
	}
	lines := make([]stepLine, 0, len(status.Steps))
	for i, stage := range status.Steps {
		lines = append(lines, stepLine{
			Stage: stage,
			Label: Translate(lang, stageKey(Stage(stage))),
			Done:  current >= 0 && i < current,
			Now:   i == current,
		})
	}
	return lines
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	status := s.app.Status(r.Context())
	// Whether Docker is missing, not what to say about it: the sentence is on the page, in the
	// page's own language.
	body := struct {
		Status
		DockerMissing bool `json:"docker_missing"`
	}{Status: status, DockerMissing: status.Docker == "" && !s.app.Native()}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(body)
}

// handleAction runs one of the launcher's actions in the background and answers immediately: the
// page polls the status for what happened, and a start that builds an image takes minutes.
func (s *Server) handleAction(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post an action", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	action := strings.TrimPrefix(r.URL.Path, "/api/action/")
	// The action outlives the request: the browser's context ends when the answer is written.
	ctx := context.Background()
	// The id of the claim, for the actions that make one before answering. Empty for the rest: the
	// page beside them reads the status, and the status is enough for a button it is looking at.
	job := ""
	switch action {
	case "start":
		go s.startFromPage(ctx, s.app.Start, s.app.OpenURL)
	case "stop":
		go func() { _ = s.app.Stop(ctx) }()
	case "update":
		go func() { _ = s.app.Update(ctx) }()
	case "apply":
		go func() { _ = s.app.Apply(ctx) }()
	case "open":
		go func() { _, _ = s.app.Open(ctx) }()
	case "check-upgrade":
		// The app's "check for updates": answered once the check has, so the status read right
		// after it already says what is out. Its failure is in the status, not the answer.
		check, cancel := context.WithTimeout(ctx, 45*time.Second)
		_, _ = s.app.CheckUpgrade(check)
		cancel()
	case "download":
		// The first half of an upgrade from the app: the release is fetched and checked while
		// the app stays in use (upgrade_download.go). The status reports how far it has got.
		if err := s.app.StartDownload(ctx); err != nil {
			refuseAction(w, err)
			return
		}
	case "upgrade":
		// Installing a newer release replaces this launcher, so it cannot run inside it: the shell
		// closes the launcher and runs `upgrade` itself, showing what it says (shell.go). Without a
		// shell the terminal is the only place it can run, and the page names the command instead.
		s.app.mu.Lock()
		offer := s.app.offer
		s.app.mu.Unlock()
		switch {
		case shell == nil:
			refuseAction(w, errors.New("close the launcher and run the upgrade command in a terminal"))
			return
		case offer == nil:
			refuseAction(w, errors.New("there is no newer release to install"))
			return
		case s.app.Mode() == ModeDocker:
			refuseAction(w, errDockerNotCovered)
			return
		case offer.Package != "":
			refuseAction(w, errors.New("this copy is installed by a package; install the new release's file instead"))
			return
		}
		shell.emit(shellEvent{Event: "upgrade", Data: s.app.paths.Data})
	case "extra/node", "extra/browser", "extra/speech":
		// The optional pieces of the runtime, fetched when something needs them rather than on
		// every install. Native only: in Docker mode the browser comes with the :browser image.
		// The app asks for these over the loopback bridge with this same token, which is why the
		// list is here and not only on the page: a component installed from Settings and one
		// installed from the launcher's window have to be the same thing happening.
		//
		// The launcher is claimed here rather than inside the goroutine, and the id of the claim
		// goes back in the answer. Both halves matter to the app: a launcher already busy with
		// something else has to be a refusal the app can show, and an action the app cannot see
		// from outside has to be one it can ask about by name afterwards.
		name := strings.TrimPrefix(action, "extra/")
		id, err := s.app.StartExtra(name)
		if err != nil {
			refuseAction(w, err)
			return
		}
		job = id
		go func() { _ = s.app.RunExtra(ctx, id, name) }()
	case "restart":
		// Node and the browser are found through the environment the supervisor was started with,
		// so a process cannot pick them up by itself: something above it has to start it again.
		// That is this. The app offers the button; the launcher is what can honour it.
		id, err := s.app.StartRestart()
		if err != nil {
			refuseAction(w, err)
			return
		}
		job = id
		go func() { _ = s.app.RunRestart(ctx, id) }()
	default:
		if name, ok := strings.CutPrefix(action, "remove-copy/"); ok {
			// A kept copy of the data, removed under the fence (kept.go). Behind the same token
			// as every other action: it deletes a whole copy of the operator's data.
			go func() { _ = s.app.RemoveKeptCopy(ctx, name) }()
			break
		}
		http.Error(w, "no such action", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusAccepted)
	_ = json.NewEncoder(w).Encode(map[string]any{"started": true, "job": job})
}

// startFromPage is the page's Start button, which ends where a first start does: in the app. It
// used to end on the launcher's status page with a link — after a start that had timed out and was
// tried again, the operator was left looking at the launcher, the pairing link the app had just
// written was never opened, and the app, when they found it, asked them to sign in.
func (s *Server) startFromPage(ctx context.Context, start func(context.Context) error, open func(context.Context) string) {
	if start(ctx) != nil || s.focus == nil {
		return
	}
	s.focus(ctx, open(ctx))
}

// handleJob answers what became of one action. A read, like the status beside it: it says whether
// an action is running, done or failed and carries the failure's own sentence, and nothing in it
// is a secret. It is the whole of what the app's bridge waits on — without it "the launcher is not
// busy" is the same answer before the work starts and after it ends, and the app that asked cannot
// tell those apart.
func (s *Server) handleJob(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/api/jobs/")
	job, ok := s.app.JobStatus(id)
	w.Header().Set("Content-Type", "application/json")
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "no such job"})
		return
	}
	_ = json.NewEncoder(w).Encode(job)
}

// refuseAction says why an action was not started, in the two ways it can fail before it begins: a
// launcher already taking another one, which the caller retries after, and an action this shape of
// installation does not have, which it never will.
func refuseAction(w http.ResponseWriter, err error) {
	if errors.Is(err, ErrNotNative) {
		http.Error(w, err.Error(), http.StatusNotImplemented)
		return
	}
	http.Error(w, err.Error(), http.StatusConflict)
}

// mustSub exposes only the stylesheet and the script, not the templates next to them.
func mustSub() fs.FS {
	sub, err := fs.Sub(uiFiles, "ui/assets")
	if err != nil {
		panic(err)
	}
	return sub
}
