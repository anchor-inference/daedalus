package netwall

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"
)

// pipeBrowser is the least of a CDP client, enough for the wall's tests to drive a real Chromium
// over --remote-debugging-pipe as the daemon does: file descriptor 3 carries commands to the
// browser, 4 carries its answers, each message a JSON object ended by a NUL byte.
type pipeBrowser struct {
	t       *testing.T
	cmd     *exec.Cmd
	out     *os.File
	mu      sync.Mutex
	next    int
	pending map[int]chan cdpReply
	session string
	done    chan struct{}
}

type cdpReply struct {
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// baseSwitches are the daemon's own switches (docs/architecture/browser.md, Which Chromium),
// repeated here so the wall is tested under the browser the daemon actually starts.
var baseSwitches = []string{
	"--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check",
	"--password-store=basic", "--disable-field-trial-config", "--disable-background-networking",
	"--disable-sync", "--disable-breakpad", "--metrics-recording-only", "--no-service-autorun",
	"--disable-client-side-phishing-detection", "--disable-domain-reliability", "--mute-audio",
	"--webrtc-ip-handling-policy=disable_non_proxied_udp",
	"--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,PasswordManagerOnboarding",
}

func startChromium(t *testing.T, binary string, extra ...string) *pipeBrowser {
	t.Helper()
	toChrome, weWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	weRead, fromChrome, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	args := append(append([]string{}, baseSwitches...), "--user-data-dir="+t.TempDir())
	if os.Getenv("BROWSERD_TEST_NO_SANDBOX") == "1" {
		// Only for a machine whose kernel refuses Chromium's sandbox to an unprivileged user and
		// that has no setuid helper; the daemon itself never does this silently.
		args = append(args, "--no-sandbox")
	}
	args = append(args, extra...)
	cmd := exec.Command(binary, args...)
	cmd.ExtraFiles = []*os.File{toChrome, fromChrome}
	cmd.Stderr = os.Stderr
	if os.Getenv("BROWSERD_TEST_CHROMIUM_QUIET") == "1" {
		cmd.Stderr = nil
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	toChrome.Close()
	fromChrome.Close()
	b := &pipeBrowser{t: t, cmd: cmd, out: weWrite, pending: map[int]chan cdpReply{}, done: make(chan struct{})}
	go b.read(bufio.NewReader(weRead))
	t.Cleanup(b.close)
	return b
}

func (b *pipeBrowser) read(r *bufio.Reader) {
	defer close(b.done)
	for {
		raw, err := r.ReadBytes(0)
		if err != nil {
			return
		}
		var msg struct {
			ID int `json:"id"`
			cdpReply
		}
		if json.Unmarshal(raw[:len(raw)-1], &msg) != nil || msg.ID == 0 {
			continue // an event; these tests poll rather than listen
		}
		b.mu.Lock()
		ch := b.pending[msg.ID]
		delete(b.pending, msg.ID)
		b.mu.Unlock()
		if ch != nil {
			ch <- msg.cdpReply
		}
	}
}

func (b *pipeBrowser) close() {
	b.call("Browser.close", nil)
	select {
	case <-b.done:
	case <-time.After(3 * time.Second):
		b.cmd.Process.Kill()
	}
	b.cmd.Wait()
	b.out.Close()
}

// call sends one command, on the attached page when there is one, and waits for its answer.
func (b *pipeBrowser) call(method string, params any) (json.RawMessage, error) {
	b.mu.Lock()
	b.next++
	id := b.next
	ch := make(chan cdpReply, 1)
	b.pending[id] = ch
	msg := map[string]any{"id": id, "method": method}
	if params != nil {
		msg["params"] = params
	}
	if b.session != "" && !strings.HasPrefix(method, "Target.") && !strings.HasPrefix(method, "Browser.") {
		msg["sessionId"] = b.session
	}
	raw, _ := json.Marshal(msg)
	_, err := b.out.Write(append(raw, 0))
	b.mu.Unlock()
	if err != nil {
		return nil, err
	}
	select {
	case r := <-ch:
		if r.Error != nil {
			return nil, fmt.Errorf("%s: %s", method, r.Error.Message)
		}
		return r.Result, nil
	case <-time.After(20 * time.Second):
		return nil, fmt.Errorf("%s: no answer", method)
	case <-b.done:
		return nil, fmt.Errorf("%s: the browser exited", method)
	}
}

func (b *pipeBrowser) must(method string, params any) json.RawMessage {
	b.t.Helper()
	r, err := b.call(method, params)
	if err != nil {
		b.t.Fatal(err)
	}
	return r
}

// openPage makes a tab and attaches to it.
func (b *pipeBrowser) openPage() {
	b.t.Helper()
	var target struct {
		TargetID string `json:"targetId"`
	}
	json.Unmarshal(b.must("Target.createTarget", map[string]any{"url": "about:blank"}), &target)
	var attached struct {
		SessionID string `json:"sessionId"`
	}
	json.Unmarshal(b.must("Target.attachToTarget", map[string]any{"targetId": target.TargetID, "flatten": true}), &attached)
	b.session = attached.SessionID
	b.must("Page.enable", nil)
}

// navigate goes to u and waits for the document to finish, the wall's refusal page included.
func (b *pipeBrowser) navigate(u string) {
	b.t.Helper()
	b.must("Page.navigate", map[string]any{"url": u})
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		var state string
		var href string
		if b.eval("document.readyState", &state) == nil && state == "complete" && b.eval("location.href", &href) == nil && href != "about:blank" {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	b.t.Fatalf("%s did not finish loading", u)
}

// eval runs expression in the page, awaiting a promise, and decodes its value into out.
func (b *pipeBrowser) eval(expression string, out any) error {
	raw, err := b.call("Runtime.evaluate", map[string]any{"expression": expression, "awaitPromise": true, "returnByValue": true})
	if err != nil {
		return err
	}
	var r struct {
		Result struct {
			Value json.RawMessage `json:"value"`
		} `json:"result"`
		ExceptionDetails *struct {
			Text string `json:"text"`
		} `json:"exceptionDetails"`
	}
	if err := json.Unmarshal(raw, &r); err != nil {
		return err
	}
	if r.ExceptionDetails != nil {
		return fmt.Errorf("the page threw: %s", r.ExceptionDetails.Text)
	}
	return json.Unmarshal(r.Result.Value, out)
}

func (b *pipeBrowser) mustEval(expression string, out any) {
	b.t.Helper()
	if err := b.eval(expression, out); err != nil {
		b.t.Fatalf("%s: %v", expression, err)
	}
}
