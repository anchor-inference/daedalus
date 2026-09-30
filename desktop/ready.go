package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// readyTimeout covers a first start on a cold machine: pulling the agent image, or building it,
// then the supervisor's own startup.
const readyTimeout = 3 * time.Minute

// pairingFile is where the server leaves the link that signs the operator in. The state directory is
// a named Docker volume, so the file is read through the container rather than from the host.
const pairingFile = "/srv/state/pairing-url"

// containerPython is the interpreter inside the agent image. The image ships a virtualenv built at
// build time and starts the bot straight from it (`DAEDALUS_BOT_CMD` in deploy/Dockerfile), so a
// command the launcher runs in the container is run the same way. Going through `uv run` would ask
// uv to reconcile that environment against the lock file on every call — a network round trip, and
// an installed second copy of the two checkouts the image deliberately mounts instead.
const containerPython = "/srv/venv/bin/python"

// AppURL is the address of the app on this machine, in the language the launcher is speaking. The
// app resolves its own language from the reader's browser when nobody says otherwise, which on a
// machine whose browser is set to one language and whose operator chose the other is the wrong
// answer — so the launcher says. The app remembers what the parameter named and drops it from the
// address, so this settles the question once rather than on every visit.
func AppURL(port string, lang Lang) string {
	return "http://127.0.0.1:" + port + "/app/?lang=" + string(lang)
}

// WaitReady polls the app until it answers 200. A redirect is followed, so /app answering with the
// index page counts. The last status seen goes into the error: an API that answers 404 means the
// container is up but the app was not built, which is a different problem from nothing listening.
func WaitReady(ctx context.Context, port string, timeout time.Duration) error {
	return waitReady(ctx, port, timeout, 2*time.Second)
}

// WaitReadyNative polls quickly: there is no image to pull and no virtual machine to wake, the whole
// wait is the bot's own boot, and two seconds of poll interval is a third of it.
func WaitReadyNative(ctx context.Context, port string, timeout time.Duration) error {
	return waitReady(ctx, port, timeout, 200*time.Millisecond)
}

// An answer on the app's port proves nothing by itself: a stack a crashed launcher left behind, or
// any other program, answers there just as well. So the launcher says, before it starts a stack,
// what it expects to hear from it, and waitReady accepts only that:
//
//   - native: the launcher gives the supervisor a fresh DAEDALUS_BOOT_ID, the bot inherits it, and
//     the app echoes it on every response as X-Daedalus-Boot. An answer with another id, or from a
//     port that was already taken before this start, is not this stack.
//   - An app older than the header answers without one. For an upgrade's or an update's health
//     check that is a failure, full stop: "the port was free and our supervisor is alive" does not
//     tie the answer to our process — anything may have taken the port after the check. For an
//     ordinary start (legacy), such an answer is taken on those two conditions, as before this
//     check existed; nothing is committed or rolled back on it.
//   - Docker: the containers are compose's, and the answer is accepted as it always was. Docker mode
//     is not upgraded or updated by the launcher (UPDATES.md), which is where this would matter.
//
// With nothing expected on a port, waitReady refuses: it has nothing to recognise an answer by.
type bootExpectation struct {
	id          string
	anyAnswer   bool
	legacy      bool // an ordinary start, which may take an app without the header (see above)
	portWasFree bool
	alive       func() bool
}

const bootHeader = "X-Daedalus-Boot"

var (
	bootMu   sync.Mutex
	expected = map[string]bootExpectation{}
)

func expectBoot(port string, e bootExpectation) {
	bootMu.Lock()
	expected[port] = e
	bootMu.Unlock()
}

func expectation(port string) (bootExpectation, bool) {
	bootMu.Lock()
	defer bootMu.Unlock()
	e, ok := expected[port]
	return e, ok
}

// ours decides whether one answer came from the stack this launcher started.
func (e bootExpectation) ours(response *http.Response) (bool, string) {
	if e.anyAnswer {
		return true, ""
	}
	got := response.Header.Get(bootHeader)
	switch {
	case got == e.id && got != "":
		return true, ""
	case got != "":
		return false, "answered by a stack this launcher did not start (another boot id)"
	case e.legacy && e.portWasFree && e.alive != nil && e.alive():
		return true, ""
	}
	return false, "answered without this start's boot id, so the answer cannot be tied to the stack this launcher started"
}

func waitReady(ctx context.Context, port string, timeout, interval time.Duration) error {
	url := "http://127.0.0.1:" + port + "/app"
	want, ok := expectation(port)
	if !ok {
		return fmt.Errorf("nothing this launcher started is expected on port %s, so an answer there would not be this installation's stack", port)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	deadline := time.Now().Add(timeout)
	last := "no answer"
	for time.Now().Before(deadline) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return err
		}
		resp, err := client.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				mine, why := want.ours(resp)
				if mine {
					return nil
				}
				last = why
			} else {
				last = resp.Status
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
	return fmt.Errorf("the app at %s did not come up within %s (%s)", url, timeout, last)
}

// PairingURL reads the link the server left in the state directory, and only when the file was
// written after the moment the launcher last brought the stack up. A link is good for one use: an
// older file is from an earlier run, its link has most likely been spent, and sending the operator
// to a spent link is worse than sending them to the app's own login screen.
func PairingURL(ctx context.Context, p Paths, telegram bool, after time.Time) string {
	out, err := composeQuiet(ctx, p, telegram, "exec", "-T", "daedalus",
		"sh", "-c", "stat -c %Y "+pairingFile+" && cat "+pairingFile)
	if err != nil {
		return ""
	}
	return pairingFromFile(out, after)
}

// NativePairingURL reads the link the server left in the state directory, which in native mode is a
// file the launcher can simply open. The same freshness rule as the container's: a link written
// before this launcher brought the installation up has most likely been spent, and sending the
// operator to a spent link is worse than sending them to the login screen.
func NativePairingURL(p Paths, after time.Time) string {
	path := filepath.Join(p.State, "pairing-url")
	info, err := os.Stat(path)
	if err != nil {
		return ""
	}
	if !after.IsZero() && info.ModTime().Before(after.Truncate(time.Second)) {
		return ""
	}
	return parsePairingURL(readFile(path))
}

// MintPairing asks the container for a fresh link. This is the only other way the launcher comes by
// one: the server does not put the link anywhere a log reader could pick it up, and a launcher that
// harvested logs would be teaching the operator that a log file is a place to find a credential.
func MintPairing(ctx context.Context, p Paths, telegram bool) (string, error) {
	out, err := composeQuiet(ctx, p, telegram, "exec", "-T", "daedalus",
		containerPython, "-m", "daedalus", "auth", "pair")
	if err != nil {
		return "", err
	}
	return firstPairingURL(out), nil
}

// SupervisorRestart asks the supervisor to apply what the checkout holds. The socket it listens on
// is inside the container — it is not published anywhere the host can reach — so the request goes
// in the way every other one does, through the container's own command line.
//
// The supervisor answers as soon as it has taken the request, not when the change is live: the
// preflight is minutes of work and the bot serves on the old revision throughout it. What comes
// back is the sentence the app shows.
func SupervisorRestart(ctx context.Context, p Paths, telegram bool) (string, error) {
	out, err := composeQuiet(ctx, p, telegram, supervisorRestartArgs()...)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(out), nil
}

// supervisorRestartArgs is the command that carries the request into the container.
func supervisorRestartArgs() []string {
	return []string{"exec", "-T", "daedalus", containerPython, "-m", "daedalus", "self", "restart", "--reason", "the launcher's Apply"}
}

// pairingFromFile reads what stat and cat printed together: the file's modification time as an
// epoch second on the first line, the link on what follows. A zero `after` means this launcher did
// not start the stack and has nothing to measure the file against, so the file is taken as it is.
func pairingFromFile(out string, after time.Time) string {
	head, rest, ok := strings.Cut(strings.TrimSpace(out), "\n")
	if !ok {
		return ""
	}
	written, err := strconv.ParseInt(strings.TrimSpace(head), 10, 64)
	if err != nil {
		return ""
	}
	// stat counts in whole seconds, so the start is rounded down to the same resolution rather than
	// failing a file written in the very second the stack came up.
	if !after.IsZero() && time.Unix(written, 0).Before(after.Truncate(time.Second)) {
		return ""
	}
	return parsePairingURL(rest)
}

// firstPairingURL takes the link off the first line that carries one: the command prints the link
// and nothing else, and anything ahead of it is the container clearing its throat.
func firstPairingURL(out string) string {
	for _, line := range strings.Split(out, "\n") {
		for _, field := range strings.Fields(line) {
			if isHTTPURL(field) {
				return strings.TrimRight(field, ".,)")
			}
		}
	}
	return ""
}

// parsePairingURL takes the last address out of the file's contents. The file holds one line, but a
// truncated write or a shell banner ahead of it should not turn into a URL the browser cannot open.
func parsePairingURL(out string) string {
	found := ""
	for _, line := range strings.Split(out, "\n") {
		for _, field := range strings.Fields(line) {
			if isHTTPURL(field) {
				found = strings.TrimRight(field, ".,)")
			}
		}
	}
	return found
}

func isHTTPURL(value string) bool {
	return strings.HasPrefix(value, "http://") || strings.HasPrefix(value, "https://")
}
