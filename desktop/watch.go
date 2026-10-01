package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// The launcher listens to the app's event stream so that the operator does not have to keep the
// window in front of them. It does not decide what is worth a desktop notification: the app's
// notification router does, knowing what the operator is looking at, their quiet hours and what
// already went to their phone, and it marks each notification it wants on the desktop with
// deliver.desktop. The launcher shows exactly those. Connecting with kind=launcher is also what
// tells the router a desktop is listening at all; while none is, it marks nothing for one.
//
// An earlier version polled /api/status with an Authorization: Bearer header, which the app has
// never accepted, so it announced nothing at all. The app's own header is X-Daedalus-Token.
const (
	// watchBackoffMin and watchBackoffMax bound the wait between connection attempts. The app
	// restarts in seconds and the launcher should be back as soon as it is; one that is down for
	// longer is asked twice a minute, not hammered.
	watchBackoffMin = time.Second
	watchBackoffMax = 30 * time.Second
	// watchIdle is how long a connection may say nothing before it is taken for dead. The app sends
	// a keepalive comment every 15 seconds, so three missed ones mean the other end is gone without
	// having closed — a container that was killed, a machine that slept.
	watchIdle = 45 * time.Second
	// watchStale is the age past which a notification is news from while the launcher was away: a
	// replay after a reconnect. A pile of those is one line, not a pile of notifications.
	watchStale = 2 * time.Minute
	// watchFoldQuiet is how long the replay has to stay quiet before its one line is shown. The replay
	// arrives as one burst, so this only has to outlast the gaps inside it.
	watchFoldQuiet = time.Second
	// watchRemembered is how many events are remembered to refuse a second showing of the same one,
	// which a reconnect whose cursor lagged behind what was shown would otherwise produce.
	watchRemembered = 256
	// watchBodyMax is how much of a body reaches the desktop. The notification centres cut far
	// sooner, and a helper's argument list is no place for a transcript.
	watchBodyMax = 300
)

// tokenScript reads the token the app minted for its own API out of the state database, through the
// container, exactly as the pairing link is read: the token is generated on a first start and kept
// in the database, so this is the only place it can be had from. It is used for loopback requests
// and never written anywhere — not to the log, not to the data folder.
const tokenScript = "import json, sqlite3;" +
	"from daedalus.config import Settings;" +
	"row = sqlite3.connect(str(Settings().db_path)).execute(\"select value from kv where key = 'api_token'\").fetchone();" +
	"print(json.loads(row[0]) if row else '')"

// APIToken asks the container for it. An empty answer means the launcher cannot read the API, which
// is not a failure: it means this installation gets no desktop notifications, and the launcher says
// so once.
func APIToken(ctx context.Context, p Paths, telegram bool) string {
	out, err := composeQuiet(ctx, p, telegram, "exec", "-T", "daedalus",
		containerPython, "-c", tokenScript)
	if err != nil {
		return ""
	}
	return firstLine(out)
}

// apiToken reads the token the app minted for itself, whichever mode this is: through the container
// in one, and by running the same one-liner in the runtime's own environment in the other.
func (a *App) apiToken(ctx context.Context) string {
	if a.Native() {
		out, err := a.native.RunPython(ctx, "-c", tokenScript)
		if err != nil {
			return ""
		}
		return firstLine(out)
	}
	return APIToken(ctx, a.paths, a.Telegram())
}

// firstLine is the first line of a command's output that carries anything. What is read this way is
// one value — a token, a link — and what comes before it is the environment clearing its throat.
func firstLine(out string) string {
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if value := strings.TrimSpace(line); value != "" {
			return value
		}
	}
	return ""
}

// Watch shows, for as long as ctx lives, the notifications the app marks for the desktop. It returns
// at once when the app's token cannot be read, after saying so: such an installation gets no desktop
// notifications, and nothing else changes.
func (a *App) Watch(ctx context.Context, show func(Notification)) {
	token := a.apiToken(ctx)
	if token == "" {
		a.log("desktop notifications are off: the launcher could not read the app's API token")
		return
	}
	w := &watcher{
		base:   "http://127.0.0.1:" + APIPort(a.paths),
		rebase: func() string { return "http://127.0.0.1:" + APIPort(a.paths) },
		token:  token,
		client: fmt.Sprintf("launcher-%d", os.Getpid()),
		lang:   a.Lang(),
		show:   show,
		log:    a.log,
	}
	w.run(ctx)
}

// watcher is one listener on /api/events. Everything it holds is touched from the goroutine that
// runs it: frames are read on another goroutine and handed over on a channel, so the fold timer, the
// cursor and the memory of what was shown need no lock.
type watcher struct {
	base string
	// rebase reads the address again before each connection: a restart from the launcher's page
	// may have moved the app to another port (ports.go), and a watcher that kept the first one
	// would knock on a door nobody answers for as long as the launcher runs. Nil keeps base.
	rebase func() string
	token  string
	client string
	lang   Lang
	show   func(Notification)
	log    func(format string, args ...any)

	// now and backoff are replaced by the tests.
	now     func() time.Time
	backoff time.Duration

	last  string        // the id of the last event read, sent back as Last-Event-ID
	skew  time.Duration // the launcher's clock minus the app's, from the hello frame
	shown recentIDs
	away  int // stale notifications not yet folded into their line
}

// run connects, reads until the connection ends, and connects again after a backoff that starts at a
// second and doubles to thirty. A connection that got as far as the app's hello resets the backoff:
// the app was there, so the next attempt is worth making soon.
func (w *watcher) run(ctx context.Context) {
	if w.now == nil {
		w.now = time.Now
	}
	wait := w.backoff
	if wait == 0 {
		wait = watchBackoffMin
	}
	reported := ""
	for {
		if w.rebase != nil {
			w.base = w.rebase()
		}
		greeted, err := w.connect(ctx)
		if ctx.Err() != nil {
			return
		}
		if greeted {
			wait = w.backoff
			if wait == 0 {
				wait = watchBackoffMin
			}
			reported = ""
		}
		// The same failure is logged once, not every thirty seconds for as long as the app is down.
		if err != nil && err.Error() != reported {
			reported = err.Error()
			w.log("desktop notifications: %v; trying again", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		if wait *= 2; wait > watchBackoffMax {
			wait = watchBackoffMax
		}
	}
}

// frame is one server-sent event as parseFrames hands it over.
type frame struct{ event, id, data string }

// connect holds one connection. It reports whether the app said hello, and why it ended.
func (w *watcher) connect(ctx context.Context) (bool, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	address := w.base + "/api/events?types=notify&kind=launcher&client=" + w.client
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return false, err
	}
	request.Header.Set("X-Daedalus-Token", w.token)
	request.Header.Set("Accept", "text/event-stream")
	if w.last != "" {
		request.Header.Set("Last-Event-ID", w.last)
	}
	// No client timeout: the response is meant to last for hours. Silence is watched below instead.
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return false, fmt.Errorf("the app answered %s", response.Status)
	}

	frames := make(chan frame)
	heard := make(chan struct{}, 1)
	ended := make(chan error, 1)
	go func() {
		ended <- parseFrames(activity{response.Body, heard}, func(event, id, data string) {
			select {
			case frames <- frame{event, id, data}:
			case <-ctx.Done():
			}
		})
	}()

	idle := time.NewTimer(watchIdle)
	defer idle.Stop()
	fold := time.NewTimer(time.Hour)
	fold.Stop()
	defer fold.Stop()
	defer w.flushAway()
	greeted := false
	for {
		select {
		case <-ctx.Done():
			return greeted, ctx.Err()
		case <-heard:
			idle.Reset(watchIdle)
		case <-idle.C:
			return greeted, errors.New("the app went quiet")
		case <-fold.C:
			w.flushAway()
		case err := <-ended:
			if err == nil {
				err = errors.New("the app closed the stream")
			}
			return greeted, err
		case f := <-frames:
			switch f.event {
			case "hello":
				greeted = true
				w.hello(f.data)
			case "resync":
				w.resync(f.data)
			case "notify":
				if f.id != "" {
					w.last = f.id
				}
				if w.notify(f.data) {
					fold.Reset(watchFoldQuiet)
				}
			}
		}
	}
}

// hello reads the app's clock, so that "older than two minutes" is measured on one clock. The app
// may run in a virtual machine whose clock drifts from the desktop's.
func (w *watcher) hello(data string) {
	var hello struct {
		ServerTime string `json:"server_time"`
	}
	if json.Unmarshal([]byte(data), &hello) != nil {
		return
	}
	if at, err := time.Parse(time.RFC3339Nano, hello.ServerTime); err == nil {
		w.skew = w.now().Sub(at)
	}
}

// resync is the app saying the cursor was too old, or ahead of it (a new database): the stream
// carries on live from its head, and what fell in between is in the app's notification centre.
func (w *watcher) resync(data string) {
	var resync struct {
		Reason string `json:"reason"`
		Head   int64  `json:"head"`
	}
	if json.Unmarshal([]byte(data), &resync) != nil {
		return
	}
	if resync.Head > 0 {
		w.last = strconv.FormatInt(resync.Head, 10)
	}
	w.log("desktop notifications: the app could not replay what was missed (%s)", resync.Reason)
}

// notifyEvent is the part of a notify event the launcher reads.
type notifyEvent struct {
	Seq     int64  `json:"seq"`
	At      string `json:"at"`
	Payload struct {
		Notification struct {
			ID        int64  `json:"id"`
			Title     string `json:"title"`
			Body      string `json:"body"`
			Link      string `json:"link"`
			SessionID string `json:"session_id"`
		} `json:"notification"`
		Deliver struct {
			Desktop bool `json:"desktop"`
		} `json:"deliver"`
	} `json:"payload"`
}

// notify handles one notify event. It reports whether the event was folded into the away line, so
// the caller can wait for the rest of the replay before showing it.
func (w *watcher) notify(data string) bool {
	var event notifyEvent
	if json.Unmarshal([]byte(data), &event) != nil {
		return false
	}
	if !event.Payload.Deliver.Desktop {
		return false
	}
	if event.Seq != 0 && !w.shown.add(event.Seq) {
		return false
	}
	if at, err := time.Parse(time.RFC3339Nano, event.At); err == nil && w.now().Sub(at)-w.skew > watchStale {
		w.away++
		return true
	}
	w.flushAway()
	n := event.Payload.Notification
	w.show(Notification{
		Title:   strings.TrimSpace(n.Title),
		Body:    clip(strings.TrimSpace(n.Body), watchBodyMax),
		Link:    w.link(n.Link, n.SessionID),
		Session: sessionOf(n.Link, n.SessionID),
		Action:  Translate(w.lang, "watch.open"),
	})
	return false
}

// flushAway shows the one line that stands for everything that arrived while the launcher was away.
func (w *watcher) flushAway() {
	if w.away == 0 {
		return
	}
	count := w.away
	w.away = 0
	w.show(Notification{
		Title:  "Daedalus",
		Body:   awayLine(w.lang, count),
		Link:   w.base + "/app/inbox",
		Action: Translate(w.lang, "watch.open"),
	})
}

// link turns the app-relative path a notification carries into an address on this machine. Only a
// path inside the app is taken: the text came over the network, and a click must not become a way
// to open anything else. A notification about a session with no link of its own opens the session.
func (w *watcher) link(path, session string) string {
	if path == "" && session != "" && isIdentifier(session) {
		path = "/app/agents/" + session
	}
	if !strings.HasPrefix(path, "/app/") || strings.Contains(path, "//") || strings.Contains(path, "\\") || strings.Contains(path, "..") {
		return ""
	}
	return w.base + path
}

// sessionOf names the session when the link is the session itself, which is what the Windows toast
// can open by the daedalus:// scheme. Any other link (a task on the board) is not a session link.
func sessionOf(path, session string) string {
	if session == "" || !isIdentifier(session) {
		return ""
	}
	if path == "" || path == "/app/agents/"+session {
		return session
	}
	return ""
}

// awayLine says how many notifications arrived while the launcher was away. Russian has three plural
// forms (1 and 21, 2–4 and 22–24, the rest, with 11–14 among the rest); English has two.
func awayLine(lang Lang, count int) string {
	key := "watch.away.many"
	switch {
	case lang == LangRU && count%10 == 1 && count%100 != 11:
		key = "watch.away.one"
	case lang == LangRU && count%10 >= 2 && count%10 <= 4 && (count%100 < 12 || count%100 > 14):
		key = "watch.away.few"
	case lang != LangRU && count == 1:
		key = "watch.away.one"
	}
	return fmt.Sprintf(Translate(lang, key), count)
}

// clip shortens text to at most max characters, on a character boundary.
func clip(text string, max int) string {
	runes := []rune(text)
	if len(runes) <= max {
		return text
	}
	return strings.TrimSpace(string(runes[:max-1])) + "…"
}

// recentIDs remembers the last watchRemembered event ids, oldest forgotten first.
type recentIDs struct {
	order []int64
	set   map[int64]bool
}

// add remembers an id and reports whether it was new.
func (r *recentIDs) add(id int64) bool {
	if r.set == nil {
		r.set = map[int64]bool{}
	}
	if r.set[id] {
		return false
	}
	r.set[id] = true
	r.order = append(r.order, id)
	if len(r.order) > watchRemembered {
		delete(r.set, r.order[0])
		r.order = r.order[1:]
	}
	return true
}

// activity passes a body through and says, without ever blocking, that something arrived: the
// keepalive comments that parseFrames swallows are what keeps an idle connection believed.
type activity struct {
	r     io.Reader
	heard chan<- struct{}
}

func (a activity) Read(p []byte) (int, error) {
	n, err := a.r.Read(p)
	if n > 0 {
		select {
		case a.heard <- struct{}{}:
		default:
		}
	}
	return n, err
}

// parseFrames reads a server-sent event stream and calls onFrame for each event in it, as the
// specification reads it: fields up to a blank line, "data" lines joined with a newline, a comment
// line (a leading ':') ignored, a single space after the colon dropped, and an event with no data
// not dispatched. The id is the one the frame itself carries, empty when it carries none; the caller
// keeps its own cursor. It returns nil at the end of the stream and the reader's error otherwise.
func parseFrames(r io.Reader, onFrame func(event, id, data string)) error {
	scanner := bufio.NewScanner(r)
	// The app caps an event's payload at 64 KiB; a line is allowed well past that so a frame is never
	// split into a wrong one by the scanner's own limit.
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	var event, id string
	var data []string
	for scanner.Scan() {
		line := strings.TrimSuffix(scanner.Text(), "\r")
		if line == "" {
			if len(data) > 0 {
				name := event
				if name == "" {
					name = "message"
				}
				onFrame(name, id, strings.Join(data, "\n"))
			}
			event, id, data = "", "", nil
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		field, value, _ := strings.Cut(line, ":")
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			event = value
		case "data":
			data = append(data, value)
		case "id":
			if !strings.ContainsRune(value, 0) {
				id = value
			}
		}
	}
	return scanner.Err()
}
