// Command deskd is the desktop daemon: it captures the operator's screens and sends synthetic input
// for the Daedalus host, within a lease the operator grants, over an authenticated local socket.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/ax"
	"github.com/ascorblack/daedalus/deskd/internal/ax/axfake"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/deskd/internal/native"
	"github.com/ascorblack/daedalus/deskd/internal/rpc"
	"github.com/ascorblack/daedalus/deskd/internal/selftest"
	"github.com/ascorblack/daedalus/deskd/internal/version"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
)

const usage = `usage:
  deskd serve --run-dir <dir> --state-dir <dir> [flags]
  deskd selftest [--keep-display]
  deskd version
  deskd axfake <command>               a fake agent-desktop, which --ax-bin fake runs (tests only)

serve flags:
  --listen unix|tcp:127.0.0.1:<port>   where to listen (default unix: <run-dir>/deskd.sock)
  --backend native|fake                the real desktop, or one in memory (default native)
  --scene <file>                       the fake desktop's displays and windows (JSON)
  --config <file>                      JSON file with the limits and the protected apps
  --operator-token-file <file>         where the operator's token is written (default <state-dir>/operator.token)
  --audit-file <file>                  the metadata audit (default <state-dir>/audit.jsonl; "off" keeps none)
  --log-file <file>, --log-level <lv>  the daemon's own log (default stderr, info)
  --ax-bin <path>|off|fake             agent-desktop, for the accessibility tree (default: beside deskd)

selftest opens a window of its own and drives nothing else; on Linux without a display it starts a
virtual one (Xvfb). It prints a report and exits 0 when every check passed.
`

// EventRingSize and EventRingBytes bound the event log: the daemon's events are small and few.
const (
	eventRingSize  = 4096
	eventRingBytes = 8 << 20
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version", "--version":
		fmt.Printf("deskd %s (protocol %d)\n", version.Version, version.Protocol)
	case "serve":
		if err := serve(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "deskd:", err)
			os.Exit(1)
		}
	case "axfake":
		// The fake accessibility tool, run by the daemon itself as its child under --ax-bin fake.
		os.Exit(axfake.Main(os.Args[2:], os.Stdout))
	case "selftest":
		fs := flag.NewFlagSet("selftest", flag.ContinueOnError)
		verbose := fs.Bool("verbose", false, "log the daemon's own lines too")
		if err := fs.Parse(os.Args[2:]); err != nil {
			os.Exit(2)
		}
		os.Exit(selftest.Main(os.Stdout, *verbose))
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "deskd: unknown command %q\n\n%s", os.Args[1], usage)
		os.Exit(2)
	}
}

type options struct {
	runDir, stateDir, listen, backendName, scene, configFile string
	operatorTokenFile, auditFile, logFile, logLevel          string
	axBin                                                    string
}

func defaultListen() string {
	// The host's asyncio has no unix-socket client on Windows.
	if runtime.GOOS == "windows" {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}

func parse(args []string) (options, error) {
	var o options
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.StringVar(&o.runDir, "run-dir", "", "")
	fs.StringVar(&o.stateDir, "state-dir", "", "")
	fs.StringVar(&o.listen, "listen", defaultListen(), "")
	fs.StringVar(&o.backendName, "backend", "native", "")
	fs.StringVar(&o.scene, "scene", "", "")
	fs.StringVar(&o.configFile, "config", "", "")
	fs.StringVar(&o.operatorTokenFile, "operator-token-file", "", "")
	fs.StringVar(&o.auditFile, "audit-file", "", "")
	fs.StringVar(&o.logFile, "log-file", "", "")
	fs.StringVar(&o.logLevel, "log-level", "info", "")
	fs.StringVar(&o.axBin, "ax-bin", "", "")
	if err := fs.Parse(args); err != nil {
		return o, err
	}
	if fs.NArg() > 0 {
		return o, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if o.runDir == "" || o.stateDir == "" {
		return o, errors.New("--run-dir and --state-dir are required")
	}
	if o.listen != "unix" && !strings.HasPrefix(o.listen, "tcp:127.0.0.1:") {
		// The desktop is the operator's: its socket never leaves the machine.
		return o, fmt.Errorf("--listen must be unix or tcp:127.0.0.1:<port>, not %q", o.listen)
	}
	if o.backendName != "native" && o.backendName != "fake" {
		return o, fmt.Errorf("--backend must be native or fake")
	}
	if o.scene != "" && o.backendName != "fake" {
		return o, errors.New("--scene is for --backend fake")
	}
	if o.operatorTokenFile == "" {
		o.operatorTokenFile = filepath.Join(o.stateDir, "operator.token")
	}
	if filepath.Clean(filepath.Dir(o.operatorTokenFile)) == filepath.Clean(o.runDir) {
		// The host is told the run directory; the operator's token must not be beside its own.
		return o, errors.New("--operator-token-file must not be in the run directory, which the host reads")
	}
	if o.auditFile == "" {
		o.auditFile = filepath.Join(o.stateDir, "audit.jsonl")
	} else if o.auditFile == "off" {
		o.auditFile = ""
	}
	return o, nil
}

func loadConfig(path string) (desk.Config, error) {
	var c desk.Config
	if path == "" {
		return c, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return c, fmt.Errorf("config %s: %w", path, err)
	}
	return c, nil
}

func newLog(path, level string) (*slog.Logger, io.Closer, error) {
	var lv slog.Level
	if err := lv.UnmarshalText([]byte(level)); err != nil {
		return nil, nil, fmt.Errorf("log level %q: %w", level, err)
	}
	var w io.Writer = os.Stderr
	var c io.Closer = io.NopCloser(nil)
	if path != "" {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			return nil, nil, err
		}
		w, c = f, f
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{Level: lv})), c, nil
}

// openAX finds and checks agent-desktop. Every failure leaves the tree unavailable and the rest of
// the daemon as it was; the reason is in info.ax. No value of --ax-bin stops the daemon from
// starting: the launcher passes it whatever it found, and a desktop without a tree is still one.
func openAX(ctx context.Context, o options, fk *fake.Fake) *ax.Tool {
	stateDir := filepath.Join(o.stateDir, "ax")
	switch {
	case o.axBin == "off":
		return ax.Off(ax.ReasonOff)
	case o.axBin == "fake" && o.backendName != "fake":
		return ax.Off(ax.ReasonUnsupported)
	case o.axBin == "fake":
		self, err := os.Executable()
		if err != nil {
			return ax.Off(ax.ReasonMissing)
		}
		scene := filepath.Join(stateDir+"-fake", "scene.json")
		actions := filepath.Join(stateDir+"-fake", "actions.jsonl")
		if err := os.MkdirAll(filepath.Dir(scene), 0o700); err != nil {
			return ax.Off(ax.ReasonFailed)
		}
		// The fake tool reads the fake desktop as it is at each call (a window moved by
		// fake.simulate moves for it too), and its actions are handed back as the fake's inputs.
		var seen int64
		return ax.Open(ctx, ax.Config{Path: self, Prefix: []string{"axfake"}, StateDir: stateDir,
			Env: []string{axfake.SceneEnv + "=" + scene},
			Before: func() error {
				b, err := json.Marshal(fk.AXScene(actions))
				if err != nil {
					return err
				}
				return os.WriteFile(scene, b, 0o600)
			},
			After: func() { seen = collectFakeActions(fk, actions, seen) }})
	case o.backendName == "fake":
		// The fake desktop has no real windows for a real tool to read.
		return ax.Off(ax.ReasonUnsupported)
	}
	path := o.axBin
	if path == "" {
		self, err := os.Executable()
		if err != nil {
			return ax.Off(ax.ReasonOff)
		}
		path = filepath.Join(filepath.Dir(self), ax.BinaryName())
		if _, err := os.Stat(path); err != nil {
			return ax.Off(ax.ReasonOff)
		}
	}
	return ax.Open(ctx, ax.Config{Path: path, StateDir: stateDir})
}

// collectFakeActions hands the fake tool's element actions after offset to the fake desktop, and
// returns the new offset.
func collectFakeActions(fk *fake.Fake, path string, offset int64) int64 {
	f, err := os.Open(path)
	if err != nil {
		return offset
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return offset
	}
	b, err := io.ReadAll(f)
	if err != nil {
		return offset
	}
	for _, line := range bytes.Split(b, []byte("\n")) {
		var l axfake.LogLine
		if json.Unmarshal(line, &l) == nil && l.Action != "" {
			fk.RecordAX(l.Action, l.Ref, l.Text, l.Window)
		}
	}
	return offset + int64(len(b))
}

func serve(args []string) error {
	o, err := parse(args)
	if err != nil {
		return err
	}
	cfg, err := loadConfig(o.configFile)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(o.stateDir, 0o700); err != nil {
		return err
	}
	// The state holds the operator's token and the audit.
	if err := server.RestrictDir(o.stateDir); err != nil {
		return err
	}
	log, logCloser, err := newLog(o.logFile, o.logLevel)
	if err != nil {
		return err
	}
	defer logCloser.Close()
	signal.Ignore(syscall.SIGHUP, syscall.SIGPIPE)

	ctx := context.Background()
	var be backend.Backend
	var fk *fake.Fake
	if o.backendName == "fake" {
		scene := fake.DefaultScene()
		if o.scene != "" {
			if scene, err = fake.LoadScene(o.scene); err != nil {
				return err
			}
		}
		fk = fake.New(scene)
		be = fk
	} else if be, err = native.Open(ctx); err != nil {
		return fmt.Errorf("opening the desktop: %w", err)
	}
	defer be.Close()

	ep, err := server.Prepare(o.runDir, o.listen, "deskd")
	if err != nil {
		if errors.Is(err, server.ErrHeld) {
			return err
		}
		return fmt.Errorf("listening in %s: %w", o.runDir, err)
	}
	defer ep.Release()
	opToken, err := server.NewToken()
	if err != nil {
		return err
	}
	if err := server.WriteSecret(o.operatorTokenFile, []byte(opToken+"\n")); err != nil {
		return fmt.Errorf("writing the operator's token: %w", err)
	}
	defer os.Remove(o.operatorTokenFile)

	auditLog, err := audit.Open(o.auditFile, 0)
	if err != nil {
		return fmt.Errorf("opening the audit: %w", err)
	}
	defer auditLog.Close()

	instance := make([]byte, 8)
	_, _ = rand.Read(instance)
	evlog := events.NewLog(eventRingSize)
	evlog.SetMaxBytes(eventRingBytes)
	d := desk.New(cfg, be, evlog, auditLog, log)
	axTool := openAX(ctx, o, fk)
	d.SetAX(axTool)
	log.Info("accessibility tree", "status", d.AXStatus())
	daemon := &rpc.Daemon{Desk: d, Events: evlog, Log: log, Instance: hex.EncodeToString(instance),
		StartedAt: time.Now().UTC(), Fake: fk}
	srv := server.New(ep.Token, log, nil)
	srv.AddToken(opToken, rpc.RoleOperator)
	daemon.Register(srv)

	stop := make(chan struct{})
	go d.Run(stop)
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT)
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ep.Listener) }()
	p := be.Platform()
	log.Info("deskd serving", "version", version.Version, "protocol", version.Protocol, "instance", daemon.Instance,
		"run_dir", o.runDir, "backend", p.SessionType)

	var serveErr error
	select {
	case s := <-sigs:
		log.Info("deskd stopping", "signal", s.String())
	case serveErr = <-served:
		log.Error("deskd listener failed", "error", fmt.Sprint(serveErr))
	}
	// Whatever the agent holds down comes up before the daemon goes.
	d.Stop("daemon")
	// The tool's helpers and its reference maps, which hold the screen's text, go with the daemon.
	axTool.Close()
	close(stop)
	ep.Release()
	_ = ep.Listener.Close()
	srv.Close()
	log.Info("deskd stopped")
	return serveErr
}
