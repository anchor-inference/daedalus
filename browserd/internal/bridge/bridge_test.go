package bridge

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

const testID = "abcdefghijklmnopabcdefghijklmnop"

func TestNativeFraming(t *testing.T) {
	var b bytes.Buffer
	if err := WriteMessage(&b, []byte(`{"t":"ping","n":1}`)); err != nil {
		t.Fatal(err)
	}
	if got := binary.LittleEndian.Uint32(b.Bytes()[:4]); got != 18 {
		t.Fatalf("length %d, little-endian as Chrome reads it", got)
	}
	m, err := ReadMessage(&b)
	if err != nil || string(m) != `{"t":"ping","n":1}` {
		t.Fatalf("%q %v", m, err)
	}
	if err := WriteMessage(&b, make([]byte, relay.MaxToExtension+1)); !errors.Is(err, relay.ErrTooLarge) || b.Len() != 0 {
		t.Fatalf("a message past Chrome's 1 MB was written: %v, %d bytes", err, b.Len())
	}
	head := make([]byte, 4)
	binary.LittleEndian.PutUint32(head, relay.MaxFromExtension+1)
	if _, err := ReadMessage(bytes.NewReader(head)); !errors.Is(err, relay.ErrTooLarge) {
		t.Fatalf("a length past 64 MiB: %v", err)
	}
	binary.LittleEndian.PutUint32(head, 10)
	if _, err := ReadMessage(bytes.NewReader(append(head, 'x'))); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("a message cut short: %v", err)
	}
	if _, err := ReadMessage(bytes.NewReader(nil)); !errors.Is(err, io.EOF) {
		t.Fatalf("the end of Chrome's pipe: %v", err)
	}
}

func TestOrigin(t *testing.T) {
	if err := checkOrigin("chrome-extension://"+testID+"/", []string{testID}); err != nil {
		t.Fatal(err)
	}
	for _, o := range []string{
		"chrome-extension://" + strings.Repeat("a", 32) + "/", // another extension
		"chrome-extension://short/",
		"https://" + testID + "/",
		"chrome-extension://" + strings.Repeat("z", 32) + "/", // not an extension id: a-p only
	} {
		if checkOrigin(o, []string{testID, strings.Repeat("z", 32)}) == nil {
			t.Errorf("%s carried", o)
		}
	}
	if checkOrigin("chrome-extension://"+testID+"/", nil) == nil {
		t.Error("with no id pinned or given, every origin must be refused")
	}
}

func TestParse(t *testing.T) {
	env := map[string]string{ExtensionIDsEnv: " " + testID + " ,", RunDirEnv: "/run/user-daemon"}
	o, err := parse([]string{"chrome-extension://" + testID + "/", "--parent-window=1234"}, func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if o.RunDir != "/run/user-daemon" || o.Origin != "chrome-extension://"+testID+"/" || checkOrigin(o.Origin, o.Allowed) != nil {
		t.Fatalf("%+v", o)
	}
	// The launcher's wrapper: its flags before the origin, Chrome's after it.
	o, err = parse([]string{"--run-dir", "/flag", "--allow-extension", testID, "chrome-extension://" + testID + "/", "--parent-window=77"},
		func(string) string { return "" })
	if err != nil || o.RunDir != "/flag" || checkOrigin(o.Origin, o.Allowed) != nil {
		t.Fatalf("%+v %v", o, err)
	}
	// The published extension is carried with nothing given.
	o, err = parse([]string{"chrome-extension://lkhhklicnjlplokpadpccndipomikoaj/"}, func(k string) string {
		if k == RunDirEnv {
			return "/r"
		}
		return ""
	})
	if err != nil || checkOrigin(o.Origin, o.Allowed) != nil {
		t.Fatalf("the pinned extension: %+v %v", o, err)
	}
	if _, err := parse([]string{"--run-dir", "/x"}, func(string) string { return "" }); err == nil {
		t.Fatal("no origin")
	}
}

// The default run directory is found as the desktop launcher finds its folders: these keys are the
// launcher's own (desktop/local.go's localKey) for the same folders.
func TestDefaultRunDir(t *testing.T) {
	none := func(string) (string, error) { return "", errors.New("no such folder") }
	for _, c := range []struct{ data, goos, key string }{
		{"/home/someone/.local/share/daedalus/data", "linux", "data-98cdad8f36c7"},
		{"/Users/Someone/Library/Application Support/Daedalus/data", "darwin", "data-c091e860481c"},
	} {
		if got := localKey(c.data, c.goos, none); got != c.key {
			t.Errorf("%s on %s: %s, the launcher's is %s", c.data, c.goos, got, c.key)
		}
	}
	root := t.TempDir()
	data := filepath.Join(root, "data")
	env := map[string]string{"DAEDALUS_LOCAL_ROOT": filepath.Join(root, "local")}
	got, err := DefaultRunDir(data, func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(root, "local", "state", localKey(data, runtime.GOOS, filepath.EvalSymlinks), "browserd-user", "run")
	if got != want {
		t.Fatalf("%s, want %s", got, want)
	}
	env["DAEDALUS_LOCAL_ROOT"] = "relative"
	if _, err := DefaultRunDir(data, func(k string) string { return env[k] }); err == nil {
		t.Fatal("a relative local root")
	}
}

// daemon is a user daemon's socket with the relay method, as rpc serves it.
func daemon(t *testing.T, refuse bool) (string, *relay.Hub) {
	t.Helper()
	run := filepath.Join(t.TempDir(), "run")
	ep, err := server.Prepare(run, "unix", "browserd")
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.DiscardHandler)
	hub := relay.NewHub(relay.Options{Log: log, InstallName: "Home"})
	srv := server.New(ep.Token, log, func() any { return map[string]any{"env": "user"} })
	srv.Handle("relay.open", func(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
		if refuse {
			return nil, wire.Errorf(wire.CodeUnsupported, "this daemon drives browsers of its own and takes no relay")
		}
		s := &sender{c: c}
		s.id = c.OpenChannel(hub.Open(s, relay.TransportNative, ""))
		return map[string]any{"channel": s.id}, nil
	})
	go func() { _ = srv.Serve(ep.Listener) }()
	t.Cleanup(func() {
		hub.Close()
		ep.Release()
		_ = ep.Listener.Close()
		srv.Close()
	})
	return run, hub
}

type sender struct {
	c  *server.Conn
	id uint32
}

func (s *sender) Send(p []byte) error { return s.c.SendChannel(s.id, p) }
func (s *sender) Close()              { s.c.CloseChannel(s.id) }

// chrome is Chrome's end of the native messaging pipes.
type chrome struct {
	in  *io.PipeWriter // the bridge's stdin
	out *io.PipeReader // the bridge's stdout
	run chan error
}

func startBridge(t *testing.T, o Options) *chrome {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	c := &chrome{in: inW, out: outR, run: make(chan error, 1)}
	go func() {
		err := Run(o, inR, outW)
		outW.Close()
		c.run <- err
	}()
	t.Cleanup(func() { inW.Close(); outR.Close() })
	return c
}

func (c *chrome) send(t *testing.T, v any) {
	t.Helper()
	b, _ := json.Marshal(v)
	var buf bytes.Buffer
	_ = binary.Write(&buf, binary.LittleEndian, uint32(len(b)))
	buf.Write(b)
	if _, err := c.in.Write(buf.Bytes()); err != nil {
		t.Fatal(err)
	}
}

func (c *chrome) read(t *testing.T, into any) string {
	t.Helper()
	got := make(chan []byte, 1)
	go func() {
		m, _ := ReadMessage(c.out)
		got <- m
	}()
	select {
	case m := <-got:
		if m == nil {
			t.Fatal("the bridge's stdout ended")
		}
		typ, _ := relay.Type(m)
		if into != nil {
			_ = json.Unmarshal(m, into)
		}
		return typ
	case <-time.After(5 * time.Second):
		t.Fatal("nothing from the bridge")
	}
	return ""
}

func TestBridgeCarriesTheRelay(t *testing.T) {
	run, hub := daemon(t, false)
	c := startBridge(t, Options{Origin: "chrome-extension://" + testID + "/", RunDir: run, Allowed: []string{testID}})
	c.send(t, relay.Hello{T: relay.THello, Proto: relay.Proto, ProfileID: "p", ProfileName: "Home", ChromeVersion: "141.0"})
	var w relay.Welcome
	if typ := c.read(t, &w); typ != relay.TWelcome || w.InstallName != "Home" || w.InstallKind != relay.InstallNative {
		t.Fatalf("%s %+v", typ, w)
	}
	s := hub.Current()
	if s == nil || s.Hello().ProfileName != "Home" {
		t.Fatal("the daemon has no relay")
	}
	// A result far larger than one socket frame (a full-page screenshot) crosses in pieces.
	big := strings.Repeat("B", 5<<20)
	done := make(chan error, 1)
	var r struct {
		Data string `json:"data"`
	}
	go func() { done <- s.Call(context.Background(), relay.OpNotify, relay.NotifyArgs{Text: "hi"}, &r) }()
	var call struct {
		ID int64  `json:"id"`
		Op string `json:"op"`
	}
	if typ := c.read(t, &call); typ != relay.TCall || call.Op != relay.OpNotify {
		t.Fatalf("%s %+v", typ, call)
	}
	value, _ := json.Marshal(map[string]string{"data": big})
	c.send(t, relay.Result{T: relay.TResult, ID: call.ID, OK: true, Value: value})
	if err := <-done; err != nil || r.Data != big {
		t.Fatalf("%v, %d bytes", err, len(r.Data))
	}
	// Chrome closing the pipe ends the relay on the daemon's side, and the bridge exits cleanly.
	c.in.Close()
	select {
	case err := <-c.run:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the bridge outlived Chrome's pipe")
	}
	deadline := time.Now().Add(5 * time.Second)
	for hub.Current() != nil {
		if time.Now().After(deadline) {
			t.Fatal("the daemon kept a relay whose Chrome went")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// The daemon ending the relay (a second Chrome refused) reaches the extension as its bye, and the
// bridge exits.
func TestBridgeDaemonBye(t *testing.T) {
	run, _ := daemon(t, false)
	first := startBridge(t, Options{Origin: "chrome-extension://" + testID + "/", RunDir: run, Allowed: []string{testID}})
	first.send(t, relay.Hello{T: relay.THello, Proto: relay.Proto, ProfileID: "p1", ProfileName: "Home"})
	first.read(t, nil)
	second := startBridge(t, Options{Origin: "chrome-extension://" + testID + "/", RunDir: run, Allowed: []string{testID}})
	second.send(t, relay.Hello{T: relay.THello, Proto: relay.Proto, ProfileID: "p2", ProfileName: "Laptop"})
	var bye relay.Bye
	if typ := second.read(t, &bye); typ != relay.TBye || bye.Reason != relay.ByeAnotherRelay {
		t.Fatalf("%s %+v", typ, bye)
	}
	select {
	case <-second.run:
	case <-time.After(5 * time.Second):
		t.Fatal("the refused bridge did not exit")
	}
}

func TestBridgeWithoutADaemon(t *testing.T) {
	c := startBridge(t, Options{Origin: "chrome-extension://" + testID + "/", RunDir: t.TempDir(), Allowed: []string{testID}})
	var bye relay.Bye
	if typ := c.read(t, &bye); typ != relay.TBye || bye.Reason != ByeNotRunning {
		t.Fatalf("%s %+v", typ, bye)
	}
	if err := <-c.run; err == nil {
		t.Fatal("no error without a daemon")
	}
	run, _ := daemon(t, true)
	c = startBridge(t, Options{Origin: "chrome-extension://" + testID + "/", RunDir: run, Allowed: []string{testID}})
	if typ := c.read(t, &bye); typ != relay.TBye || bye.Reason != ByeUnavailable || !strings.Contains(bye.Message, "takes no relay") {
		t.Fatalf("%s %+v", typ, bye)
	}
}

// An extension that is not one of the pinned ones gets nothing, not even a word.
func TestBridgeRefusesAnOrigin(t *testing.T) {
	run, hub := daemon(t, false)
	var out bytes.Buffer
	err := Run(Options{Origin: "chrome-extension://" + strings.Repeat("b", 32) + "/", RunDir: run, Allowed: []string{testID}}, strings.NewReader(""), &out)
	if err == nil || out.Len() != 0 || hub.Current() != nil {
		t.Fatalf("%v, %d bytes out", err, out.Len())
	}
}

// The bridge, the extension and the launcher each spell the extension's id and the host's name, and
// a difference between any two is a Chrome that silently never starts the bridge. The extension's
// own tests tie its id to its manifest key, so the bridge only has to agree with the extension's
// source; the launcher's tests check the same pair from its side.
func TestTheBridgeNamesWhatTheExtensionDeclares(t *testing.T) {
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "extension", "src", "protocol.ts"))
	if err != nil {
		t.Skipf("no extension beside this tree: %v", err)
	}
	declared := func(name string) string {
		m := regexp.MustCompile(`export const ` + name + ` = "([^"]+)"`).FindSubmatch(src)
		if m == nil {
			t.Fatalf("extension/src/protocol.ts declares no %s", name)
		}
		return string(m[1])
	}
	if id := declared("EXTENSION_ID"); len(PinnedExtensionIDs) == 0 || PinnedExtensionIDs[0] != id {
		t.Fatalf("the bridge pins %v, the extension is %q", PinnedExtensionIDs, id)
	}
	if host := declared("NATIVE_HOST"); HostName != host {
		t.Fatalf("the bridge is host %q, the extension asks for %q", HostName, host)
	}
}
