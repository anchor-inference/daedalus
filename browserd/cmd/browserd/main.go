// Command browserd is the browser daemon: it owns Chromium on the agent's profiles, serves its pages
// to the Daedalus host over an authenticated local socket, and streams live views of them.
package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/record"
	"github.com/ascorblack/daedalus/browserd/internal/rpc"
	"github.com/ascorblack/daedalus/browserd/internal/version"
	"github.com/ascorblack/daedalus/browserd/internal/view"
	"github.com/ascorblack/daedalus/browserd/internal/workflow"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
)

const usage = `usage:
  browserd serve --env <name> --run-dir <dir> --state-dir <dir> [flags]
  browserd version

serve flags:
  --listen unix|tcp:127.0.0.1:<port>   where to listen (default unix: <run-dir>/browserd.sock)
  --chromium <path>                    the Chromium to run (default: found, see the contract)
  --config <file>                      JSON file with limits and the Chromium
  --log-file <file>, --log-level <lv>  the daemon's own log (default stderr, info)
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version", "--version":
		fmt.Printf("browserd %s (protocol %d)\n", version.Version, version.Protocol)
	case "serve":
		if err := serve(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "browserd:", err)
			os.Exit(1)
		}
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "browserd: unknown command %q\n\n%s", os.Args[1], usage)
		os.Exit(2)
	}
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

// tabUpdates is how often one tab's title, address and loading state may be published: a page
// that rewrites its title many times a second ends up correct, not published every time.
var tabUpdates = map[string]events.Policy{"tab.updated": {Window: 250 * time.Millisecond, Coalesce: true}}

// keyframe is a recording's picture of a tab: the page model's screenshot, so every secret field is
// masked in it as in any screenshot, at a quality and width that keep a week of frames small.
func keyframe(model *page.Model) record.Shooter {
	return func(ctx context.Context, t *browser.Tab) ([]byte, int, int, error) {
		shot, err := model.Screenshot(ctx, t, page.ScreenshotParams{TabID: t.ID, MaxWidth: 1280, Format: "jpeg", Quality: 50,
			Origin: &browser.Origin{Actor: browser.ActorOperator}})
		if err != nil {
			return nil, 0, 0, err
		}
		data, err := base64.StdEncoding.DecodeString(shot.Data)
		return data, shot.Width, shot.Height, err
	}
}

func serve(args []string) error {
	cfg, err := config.Parse(args)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return err
	}
	// Profiles hold the agent's logins: nobody else on the machine reads them.
	if err := server.RestrictDir(cfg.StateDir); err != nil {
		return err
	}
	log, logCloser, err := newLog(cfg.LogFile, cfg.LogLevel)
	if err != nil {
		return err
	}
	defer logCloser.Close()
	// A browser exiting must never take the daemon with it, nor the terminal it was started from.
	signal.Ignore(syscall.SIGHUP, syscall.SIGPIPE)

	ep, err := server.Prepare(cfg.RunDir, cfg.Listen, "browserd")
	if err != nil {
		if errors.Is(err, server.ErrHeld) {
			return err
		}
		return fmt.Errorf("listening in %s: %w", cfg.RunDir, err)
	}
	defer ep.Release()

	instance := make([]byte, 8)
	_, _ = rand.Read(instance)
	evlog := events.NewLog(config.EventRingSize)
	evlog.SetMaxBytes(config.EventRingBytes)
	deb := events.NewDebouncer(evlog, tabUpdates)
	var hub *view.Hub
	// The network wall: every browser gets a proxy of its own, at its strictest (the internet only)
	// until the host sends its rules with net.configure. Its egress events are rate-limited by the
	// wall itself.
	wall := netwall.NewBrowsers(netwall.New(netwall.Options{Events: func(e netwall.Egress) { deb.Publish("egress", "", e) }}))
	manager := browser.New(browser.Deps{Config: cfg, Log: log, Events: deb, Wall: wall,
		Busy: func(b *browser.Browser) bool { return hub != nil && hub.Busy(b) }})
	hub = view.New(manager, evlog, log)
	model := page.New(manager, cfg, log)
	hub.HumanInput = model.HumanInput
	hub.Selection = model.Selection
	manager.Listen(hub)
	manager.Listen(model)
	recorder := &record.Recorder{Store: record.Open(filepath.Join(cfg.StateDir, "recordings")), Log: log,
		Shoot:  keyframe(model),
		Groups: manager.Group,
		Limits: func() (int64, time.Duration) {
			l := manager.Limits()
			return l.RecordMaxBytes, time.Duration(l.RecordRetentionMs) * time.Millisecond
		}}
	model.AfterAction = recorder.AfterAction
	steps := &workflow.Recorder{Pages: model, Groups: manager.Group, Publish: manager.Publish, Events: evlog, Log: log}
	hub.Observe = steps.Before
	daemon := &rpc.Daemon{Config: cfg, Instance: hex.EncodeToString(instance), StartedAt: time.Now().UTC(),
		Manager: manager, Hub: hub, Page: model, Events: evlog, Log: log, Net: wall, Record: recorder, Workflow: steps}
	srv := server.New(ep.Token, log, daemon.Hello)
	daemon.Register(srv)

	stop := make(chan struct{})
	go manager.RunMaintenance(stop)
	go manager.RunStats(stop)
	go manager.RunTitles(stop)
	go hub.Run(stop)
	go recorder.Run(stop)
	go steps.Run(stop)

	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT)
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ep.Listener) }()
	found, ok := manager.Chromium()
	log.Info("browserd serving", "env", cfg.Env, "version", version.Version, "protocol", version.Protocol,
		"instance", daemon.Instance, "run_dir", cfg.RunDir, "chromium", found.Path, "chromium_found", ok)

	var serveErr error
	select {
	case s := <-sigs:
		log.Info("browserd stopping", "signal", s.String())
	case serveErr = <-served:
		log.Error("browserd listener failed", "error", fmt.Sprint(serveErr))
	}
	// The endpoint goes first, so the host stops connecting to a daemon on its way out; then every
	// browser gets its grace at once.
	close(stop)
	ep.Release()
	_ = ep.Listener.Close()
	manager.Shutdown()
	srv.Close()
	log.Info("browserd stopped")
	return serveErr
}
