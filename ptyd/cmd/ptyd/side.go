package main

import (
	"log/slog"
	"os"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
	"github.com/ascorblack/daedalus/ptyd/internal/rpc"
	"github.com/ascorblack/daedalus/ptyd/internal/sidechan"
	"github.com/ascorblack/daedalus/ptyd/internal/sidechan/sessions"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// startSide opens the side channels: the hook listener, the launch registry, the file reader, the
// program runner and the dialer. The returned function closes them.
func startSide(cfg *config.Config, evlog *events.Log, environ []string, log *slog.Logger) (*rpc.Side, func(), error) {
	// Hook posts carry bodies up to a megabyte; the log is bounded by their size as well as by
	// their number.
	evlog.SetMaxBytes(config.EventLogBytes)
	exe, err := os.Executable()
	if err != nil {
		return nil, nil, err
	}
	launches, err := hooks.NewRegistry(cfg.StateDir, exe, evlog, log)
	if err != nil {
		return nil, nil, err
	}
	fsys, err := sidechan.NewFS(cfg.Side.FSRoots, cfg.Side.FSDeny, []string{cfg.RunDir, cfg.StateDir}, cfg.Home)
	if err != nil {
		return nil, nil, err
	}
	hsrv, ln, err := launches.Listen(cfg.HooksListen)
	if err != nil {
		return nil, nil, err
	}
	side := &rpc.Side{
		Exec:     sidechan.NewExec(cfg.Side.ExecAllow, environ, cfg.Home, log),
		FS:       fsys,
		Dialer:   sidechan.NewDialer(launches),
		Launches: launches,
		Sessions: sessions.NewService(sessions.NewEnv(cfg.Home, fsys.Refused), sessions.Parsers()),
		Listen:   ln.Addr().String(),
		StateDir: cfg.StateDir,
	}
	log.Info("hook listener", "listen", side.Listen)
	return side, func() {
		launches.Close()
		_ = hsrv.Close()
	}, nil
}
