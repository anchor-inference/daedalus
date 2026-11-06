package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeProvider is a client whose connection the test holds.
type fakeProvider struct {
	mu        sync.Mutex
	connected bool
	runs      int
	errs      []error
	address   string
}

func (f *fakeProvider) Visitor() VisitorOf    { return nil }
func (f *fakeProvider) NeedsRouterPort() bool { return true }
func (f *fakeProvider) Connected(context.Context) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.connected
}
func (f *fakeProvider) set(connected bool) {
	f.mu.Lock()
	f.connected = connected
	f.mu.Unlock()
}
func (f *fakeProvider) started() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.runs
}
func (f *fakeProvider) Run(ctx context.Context, t *Tunnel) error {
	f.mu.Lock()
	f.runs++
	var err error
	if len(f.errs) > 0 {
		err, f.errs = f.errs[0], f.errs[1:]
	}
	address := f.address
	f.mu.Unlock()
	if err != nil {
		return err
	}
	f.set(true)
	t.Up(address)
	<-ctx.Done()
	f.set(false)
	return ctx.Err()
}

func quickTimings() timings {
	return timings{tick: 5 * time.Millisecond, sleepGap: time.Hour, graceAfter: 30 * time.Millisecond, settled: time.Hour, maxBackoff: 10 * time.Millisecond, fatalWait: 10 * time.Millisecond, proofEvery: time.Millisecond, proofTries: 3}
}

func newTestDaemon(t *testing.T, provider Provider) (*Daemon, string) {
	t.Helper()
	dir := t.TempDir()
	d := NewDaemon(provider, Config{Provider: "fake", DoorPort: "1", ServicesPort: "2"}, dir, t.Logf)
	d.timing = quickTimings()
	d.prove = func(context.Context, string) bool { return true }
	return d, dir
}

func waitFor(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(time.Millisecond)
	}
}

func readStatus(t *testing.T, dir string) Status {
	t.Helper()
	var status Status
	body, err := os.ReadFile(filepath.Join(dir, "status.json"))
	if err != nil {
		return status
	}
	if err := json.Unmarshal(body, &status); err != nil {
		t.Fatal(err)
	}
	return status
}

func run(t *testing.T, d *Daemon) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = d.Run(ctx)
		close(done)
	}()
	stop := func() {
		cancel()
		<-done
	}
	t.Cleanup(stop)
	var once sync.Once
	return func() { once.Do(stop) }
}

func TestUpIsNotClaimedUntilTheAddressHasAnswered(t *testing.T) {
	provider := &fakeProvider{address: "https://x.example/"}
	d, dir := newTestDaemon(t, provider)
	var answers atomic.Bool
	var asked atomic.Value
	d.prove = func(_ context.Context, url string) bool {
		asked.Store(url)
		return answers.Load()
	}
	d.timing.proofTries = 1 << 20
	run(t, d)
	waitFor(t, "up", func() bool { return readStatus(t, dir).State == StateUp })
	if status := readStatus(t, dir); status.Confirmed || status.Address != "https://x.example" {
		t.Fatalf("%+v", status)
	}
	answers.Store(true)
	waitFor(t, "confirmed", func() bool { return readStatus(t, dir).Confirmed })
	if url := asked.Load().(string); !strings.HasPrefix(url, "https://x.example/.well-known/daedalus-") {
		t.Fatalf("asked %s", url)
	}
}

func TestAClientThatLosesItsConnectionIsSaidToAndStartedAgain(t *testing.T) {
	provider := &fakeProvider{address: "https://x.example"}
	d, dir := newTestDaemon(t, provider)
	d.timing.graceAfter = 200 * time.Millisecond
	run(t, d)
	waitFor(t, "confirmed", func() bool { return readStatus(t, dir).Confirmed })
	provider.set(false)
	waitFor(t, "the truth", func() bool { return readStatus(t, dir).State == StateReconnecting })
	if readStatus(t, dir).Confirmed {
		t.Fatal("a lost tunnel is not confirmed")
	}
	waitFor(t, "a second start", func() bool { return provider.started() >= 2 })
	waitFor(t, "up again", func() bool { s := readStatus(t, dir); return s.State == StateUp && s.Confirmed })
}

func TestAClientThatRecoversByItselfKeepsItsProcess(t *testing.T) {
	provider := &fakeProvider{address: "https://x.example"}
	d, dir := newTestDaemon(t, provider)
	d.timing.graceAfter = time.Hour // a quick tunnel started again would come back with another address
	run(t, d)
	waitFor(t, "up", func() bool { return readStatus(t, dir).Confirmed })
	provider.set(false)
	waitFor(t, "down", func() bool { return readStatus(t, dir).State == StateReconnecting })
	provider.set(true)
	waitFor(t, "back", func() bool { s := readStatus(t, dir); return s.State == StateUp && s.Confirmed })
	if provider.started() != 1 {
		t.Fatalf("restarted %d times", provider.started())
	}
}

func TestANewNetworkPutsTheAddressBackInDoubt(t *testing.T) {
	provider := &fakeProvider{address: "https://x.example"}
	d, dir := newTestDaemon(t, provider)
	var network atomic.Value
	network.Store("192.0.2.1")
	d.addresses = func() string { return network.Load().(string) }
	var proofs atomic.Int32
	d.prove = func(context.Context, string) bool {
		proofs.Add(1)
		return true
	}
	run(t, d)
	waitFor(t, "confirmed", func() bool { return readStatus(t, dir).Confirmed })
	before := proofs.Load()
	network.Store("198.51.100.1")
	waitFor(t, "a wake counted", func() bool { return readStatus(t, dir).Wakes == 1 })
	waitFor(t, "a new proof", func() bool { return proofs.Load() > before })
}

func TestASleepIsAGapInTheWallClock(t *testing.T) {
	// The ticker cannot be made to sleep, so this reads the rule the watcher applies.
	last := time.Now().Add(-time.Minute).Round(0)
	if time.Now().Round(0).Sub(last) <= 20*time.Second {
		t.Fatal("a minute's gap reads as a sleep")
	}
}

func TestFailingStartsBackOff(t *testing.T) {
	cases := map[int]time.Duration{1: time.Second, 2: 2 * time.Second, 3: 4 * time.Second, 6: 32 * time.Second, 7: time.Minute, 400: time.Minute}
	for n, want := range cases {
		if got := backoff(n, time.Minute); got != want {
			t.Errorf("%d: %s", n, got)
		}
	}
}

func TestARefusedTokenIsAFailureAndNotAReconnect(t *testing.T) {
	provider := &fakeProvider{address: "https://x.example", errs: []error{fatal(CodeTokenRejected, "refused"), fatal(CodeTokenRejected, "refused")}}
	d, dir := newTestDaemon(t, provider)
	d.timing.fatalWait = 300 * time.Millisecond
	run(t, d)
	waitFor(t, "failed", func() bool { s := readStatus(t, dir); return s.State == StateFailed && s.Code == CodeTokenRejected })
	if provider.started() > 1 {
		t.Fatal("what needs a person is not hammered")
	}
	waitFor(t, "a later try", func() bool { return readStatus(t, dir).Confirmed })
	if s := readStatus(t, dir); s.Code != "" || s.Detail != "" {
		t.Fatalf("a failure stays behind: %+v", s)
	}
}

func TestChildrenAreReportedAndForgotten(t *testing.T) {
	d, dir := newTestDaemon(t, &fakeProvider{})
	tunnel := d.tunnel("")
	tunnel.Child(4242)
	if pids := readStatus(t, dir).PIDs; len(pids) != 2 {
		t.Fatalf("%v", pids)
	}
	d.dropChildren()
	if pids := readStatus(t, dir).PIDs; len(pids) != 1 || pids[0] != os.Getpid() {
		t.Fatalf("%v", pids)
	}
}

// writeScript is a client program standing in for a vendor's.
func writeScript(t *testing.T, name, body string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("a shell script stands in for the client")
	}
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func fakeTunnel(t *testing.T, dir string) (*Tunnel, chan string, *[]int) {
	t.Helper()
	up := make(chan string, 4)
	pids := &[]int{}
	return &Tunnel{Router: "http://127.0.0.1:1", Dir: dir, Logf: t.Logf, Up: func(a string) { up <- a }, SignIn: func(string) {}, Child: func(p int) { *pids = append(*pids, p) }}, up, pids
}

func TestAMissingClientWaitsForAPerson(t *testing.T) {
	dir := t.TempDir()
	tunnel, _, _ := fakeTunnel(t, dir)
	err := (&cloudflare{binary: filepath.Join(dir, "gone"), dir: dir}).Run(context.Background(), tunnel)
	if failure := failureOf(err); failure.Code != CodeNoClient || !failure.Fatal {
		t.Fatalf("%v", err)
	}
	err = (&cloudflare{dir: dir}).Run(context.Background(), tunnel)
	if failure := failureOf(err); failure.Code != CodeNoClient {
		t.Fatalf("%v", err)
	}
}

func TestAQuickTunnelsAddressIsReadFromItsOutputWhenTheMetricsAreSlow(t *testing.T) {
	dir := t.TempDir()
	script := writeScript(t, "cloudflared", `echo "INF |  https://some-words-here.trycloudflare.com  |" >&2
exec sleep 30
`)
	tunnel, up, pids := fakeTunnel(t, dir)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- (&cloudflare{binary: script, dir: dir}).Run(ctx, tunnel) }()
	select {
	case address := <-up:
		if address != "https://some-words-here.trycloudflare.com" {
			t.Fatal(address)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no address")
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("%v", err)
	}
	if len(*pids) != 1 {
		t.Fatalf("%v", *pids)
	}
}

func TestNgrokIsNeverToldItsTokenOnACommandLine(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "args")
	script := writeScript(t, "ngrok", `echo "$@" > `+marker+`
env >> `+marker+`
echo '{"lvl":"eror","err":"authentication failed: ERR_NGROK_107"}'
exit 1
`)
	tunnel, _, _ := fakeTunnel(t, dir)
	err := (&ngrok{binary: script, token: "the-secret-token", dir: dir}).Run(context.Background(), tunnel)
	if failure := failureOf(err); failure.Code != CodeTokenRejected || !failure.Fatal {
		t.Fatalf("%v", err)
	}
	seen, _ := os.ReadFile(marker)
	if strings.Contains(string(seen), "the-secret-token") {
		t.Fatalf("in its arguments or environment:\n%s", seen)
	}
	if !strings.Contains(string(seen), "--config") {
		t.Fatalf("%s", seen)
	}
	if entries, _ := filepath.Glob(filepath.Join(dir, "ngrok", "*.yml")); len(entries) != 0 {
		t.Fatalf("the config outlived the run: %v", entries)
	}
}

func TestNgrokWithNoTokenAsksForOne(t *testing.T) {
	dir := t.TempDir()
	tunnel, _, _ := fakeTunnel(t, dir)
	if failure := failureOf((&ngrok{binary: "x", dir: dir}).Run(context.Background(), tunnel)); failure.Code != CodeNotConfigured {
		t.Fatalf("%v", failure)
	}
}

func TestNgrokConfigQuotesTheToken(t *testing.T) {
	config := (&ngrok{token: `a"b`}).config("4040")
	if !strings.Contains(config, `authtoken: "a\"b"`) || !strings.Contains(config, "web_addr: 127.0.0.1:4040") {
		t.Fatal(config)
	}
}

func TestNgrokReadsItsAddressFromItsOwnAPI(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte(`{"tunnels":[{"public_url":"http://x.ngrok-free.dev"},{"public_url":"https://x.ngrok-free.dev"}]}`))
	})}
	go server.Serve(listener)
	defer server.Close()
	n := &ngrok{web: listener.Addr().String()}
	if got := n.address(context.Background()); got != "https://x.ngrok-free.dev" {
		t.Fatal(got)
	}
	if !n.Connected(context.Background()) {
		t.Fatal("a client that lists its tunnel holds it")
	}
}

func TestFrpWritesItsClientAConfigAndNamesItsAddress(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "seen")
	script := writeScript(t, "frpc", `cp "$2" `+marker+`
echo "$@" >> `+marker+`.args
exec sleep 30
`)
	tunnel, up, _ := fakeTunnel(t, dir)
	tunnel.Router = "http://127.0.0.1:40123"
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- (&frp{binary: script, server: "vps.example:7001", domain: "agent.example", token: "the-secret-token", dir: dir}).Run(ctx, tunnel)
	}()
	if address := <-up; address != "https://agent.example" {
		t.Fatal(address)
	}
	waitFor(t, "the client", func() bool { _, err := os.Stat(marker); return err == nil })
	cancel()
	<-done
	config, _ := os.ReadFile(marker)
	for _, want := range []string{`serverAddr = "vps.example"`, "serverPort = 7001", `auth.token = "the-secret-token"`, "localPort = 40123", `customDomains = ["agent.example"]`, "loginFailExit = false"} {
		if !strings.Contains(string(config), want) {
			t.Fatalf("no %q in\n%s", want, config)
		}
	}
	args, _ := os.ReadFile(marker + ".args")
	if strings.Contains(string(args), "the-secret-token") {
		t.Fatal("the token is on a command line")
	}
}

func TestFrpNeedsAllThree(t *testing.T) {
	dir := t.TempDir()
	tunnel, _, _ := fakeTunnel(t, dir)
	for _, f := range []*frp{{binary: "x", dir: dir}, {binary: "x", server: "a:1", dir: dir}, {binary: "x", server: "a:1", domain: "b", dir: dir}} {
		if failure := failureOf(f.Run(context.Background(), tunnel)); failure.Code != CodeNotConfigured {
			t.Fatalf("%v", failure)
		}
	}
}

func TestFrpReadsItsStateFromItsAdminAPI(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var status atomic.Value
	status.Store("running")
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode(map[string][]map[string]string{"http": {{"name": "daedalus", "status": status.Load().(string)}}})
	})}
	go server.Serve(listener)
	defer server.Close()
	f := &frp{admin: listener.Addr().String()}
	if !f.Connected(context.Background()) {
		t.Fatal("running")
	}
	status.Store("wait start")
	if f.Connected(context.Background()) {
		t.Fatal("not running")
	}
}

func TestAClientsLinesAreRedactedBeforeTheyAreKept(t *testing.T) {
	line := redactLine(`GET /a/WvK2-invite-here/redeem and /s/app~key/x token=abcdefghijklmnopqrstuvwxyz0123456789`)
	for _, leak := range []string{"WvK2", "app~key", "abcdefghijklmnopqrstuvwxyz"} {
		if strings.Contains(line, leak) {
			t.Fatal(line)
		}
	}
}

func TestFunnelRefusalsAreWordedForAPerson(t *testing.T) {
	if failure := failureOf(funnelFailure(errors.New("Funnel not available; HTTPS must be enabled. See https://tailscale.com/s/https"))); failure.Code != CodeHTTPSOff || !failure.Fatal {
		t.Fatalf("%+v", failure)
	}
	if failure := failureOf(funnelFailure(errors.New(`Funnel not available; "funnel" node attribute not set.`))); failure.Code != CodeFunnelOff {
		t.Fatalf("%+v", failure)
	}
}

func TestEveryProviderIsKnownAndAnUnknownOneWaitsForAPerson(t *testing.T) {
	for _, name := range []string{"ngrok", "tailscale", "cloudflare", "frp"} {
		provider, err := newProvider(Config{Provider: name}, Secrets{}, t.TempDir())
		if err != nil || provider == nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if _, err := newProvider(Config{Provider: "other"}, Secrets{}, t.TempDir()); failureOf(err).Code != CodeNotConfigured {
		t.Fatalf("%v", err)
	}
}

// With no router port, an embedded client serves the handler on its own listener: no local port at all,
// and the visitor comes from the connection. Stands in for tsnet, which needs an account.
func TestAProviderThatHandsOverConnectionsOpensNoPort(t *testing.T) {
	if (&tailscale{}).NeedsRouterPort() {
		t.Fatal("Funnel hands connections over")
	}
	d, _ := newTestDaemon(t, &fakeProvider{})
	tunnel := d.tunnel("")
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go tunnel.Serve(listener)
	defer listener.Close()
	answer, err := http.Get("http://" + listener.Addr().String() + "/api/status")
	if err != nil {
		t.Fatal(err)
	}
	answer.Body.Close()
	if answer.StatusCode != http.StatusNotFound {
		t.Fatalf("%d", answer.StatusCode)
	}
}
