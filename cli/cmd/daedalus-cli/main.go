// daedalus-cli is a terminal client for a Daedalus server: the agent and its memory stay on the
// server, and the tools of a conversation started here run on this machine, in this folder. The
// install also names it `dae`; both names are the same binary.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/app"
	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/config"
	"github.com/ascorblack/daedalus/cli/internal/device"
	"github.com/ascorblack/daedalus/cli/internal/exec"
	"github.com/ascorblack/daedalus/cli/internal/guard"
)

// version is set at build time with -ldflags "-X main.version=<v>".
var version = "0.1.0-dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// globals are the flags every command takes.
type globals struct {
	server  string
	profile string
	token   string
	guard   string
}

func (g *globals) register(fs *flag.FlagSet) {
	fs.StringVar(&g.server, "server", "", "the server's address (default: the profile's)")
	fs.StringVar(&g.profile, "profile", "", "the profile to use (default: the configuration's default)")
	fs.StringVar(&g.token, "token", "", "use the server's API token instead of this device's (development; never stored)")
	fs.StringVar(&g.guard, "guard", "", "the local guard to start in: ask, auto or plan")
}

func name() string {
	n := filepath.Base(os.Args[0])
	n = strings.TrimSuffix(n, ".exe")
	if n == "" || n == "." {
		return "daedalus-cli"
	}
	return n
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) > 0 {
		switch args[0] {
		case "login":
			return cmdLogin(args[1:], stdout, stderr)
		case "logout":
			return cmdLogout(args[1:], stdout, stderr)
		case "status":
			return cmdStatus(args[1:], stdout, stderr)
		case "sessions":
			return cmdSessions(args[1:], stdout, stderr)
		case "resume":
			return cmdResume(args[1:], stdin, stdout, stderr)
		case "doctor":
			return cmdDoctor(args[1:], stdout, stderr)
		case "version":
			fmt.Fprintf(stdout, "daedalus-cli %s\n", version)
			return 0
		case "help":
			usage(stdout)
			return 0
		}
	}
	return cmdMain(args, stdin, stdout, stderr)
}

func usage(w io.Writer) {
	n := name()
	fmt.Fprintf(w, `%[1]s: a terminal for your Daedalus server. The agent runs on the server; its tools run here.

  %[1]s [prompt]                  open a conversation in this folder (the prompt is its first message)
  %[1]s -p "prompt"               run one turn and print the answer (headless)
  %[1]s -c                        continue the latest conversation of this folder
  %[1]s -r <session>              resume a conversation
  %[1]s login <server>            pair this machine with a server
  %[1]s logout                    forget this machine's token
  %[1]s status                    where this terminal is connected
  %[1]s sessions                  list recent conversations
  %[1]s resume [session]          resume a conversation (pick one when none is named)
  %[1]s doctor                    check the configuration, the credential and the connection
  %[1]s version

Flags of a conversation:
  -p, --print                 headless: run one turn of the prompt and exit
  --output-format FORMAT      text (default), json or stream-json, with -p
  --verbose                   with -p: tool calls on stderr (text) or thinking (stream-json)
  --timeout DURATION          with -p: stop the turn after this long (exit 124)
  --guard LEVEL               ask, auto (default) or plan; Shift+Tab cycles it
  --permission-mode LEVEL     the same as --guard
  --plain                     line mode instead of the full screen
  --server URL, --profile NAME, --token TOKEN

Exit codes of -p: 0 done, 1 the turn failed, 2 usage, 3 not paired or token refused,
4 server unreachable, 5 waiting for an approval or an answer, 124 timeout, 130 interrupted.
`, n)
}

// env is everything a command that talks to the server needs.
type env struct {
	cfg      *config.File
	sel      config.Selected
	store    auth.Store
	token    string
	source   string
	client   *client.Client
	log      *slog.Logger
	cwd      string
	guard    guard.Level
	executor *exec.Executor
}

func setup(g globals, needToken bool) (*env, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, err
	}
	sel, err := cfg.Select(g.profile, g.server)
	if err != nil {
		return nil, err
	}
	if sel.Profile.Server == "" {
		return nil, fmt.Errorf("no server is configured: run %s login <server>", name())
	}
	dir, err := config.Dir()
	if err != nil {
		return nil, err
	}
	store, err := auth.OpenStore(dir)
	if err != nil {
		return nil, err
	}
	e := &env{cfg: cfg, sel: sel, store: store, log: logger()}
	switch {
	case g.token != "":
		e.token, e.source = g.token, "the --token flag"
	default:
		e.token, e.source, err = auth.Token(store, sel.Name)
		if err != nil && !errors.Is(err, auth.ErrNotFound) {
			return nil, err
		}
	}
	if needToken && e.token == "" {
		return nil, app.ErrNotPaired
	}
	e.client = client.New(sel.Profile.Server, e.token)
	e.client.UserAgent = "daedalus-cli/" + version
	if e.cwd, err = os.Getwd(); err != nil {
		return nil, err
	}
	levelName := g.guard
	if levelName == "" {
		levelName = sel.Profile.Guard
	}
	if e.guard, err = guard.ParseLevel(levelName); err != nil {
		return nil, err
	}
	return e, nil
}

// logger writes to DAEDALUS_CLI_LOG when it names a file; otherwise nowhere, since stderr belongs to
// the screen.
func logger() *slog.Logger {
	if path := os.Getenv("DAEDALUS_CLI_LOG"); path != "" {
		if f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600); err == nil {
			return slog.New(slog.NewTextHandler(f, &slog.HandlerOptions{Level: slog.LevelDebug}))
		}
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// backend builds the backend and, for a device token, starts the device connection.
func (e *env) backend(ctx context.Context, interactive bool) (*app.Backend, *guard.Guard, error) {
	executor, err := exec.New(e.log)
	if err != nil {
		return nil, nil, err
	}
	e.executor = executor
	g := guard.New(e.guard, nil)
	b := app.New(app.Options{Client: e.client, Executor: executor, Guard: g, Cwd: e.cwd, Version: version,
		Profile: e.sel.Name, Config: e.cfg, Store: e.store, StoreBackend: e.source, Interactive: interactive})
	if client.IsDeviceToken(e.token) {
		link := device.New(device.Options{Server: e.client.Base(), Token: e.token, Client: b.ClientID(), ClientVersion: version,
			Executor: executor, Guard: g, Sessions: b.ShownSessions, OnStatus: b.DeviceStatus, Log: e.log})
		b.AttachLink(link)
		ended := make(chan error, 1)
		go func() { ended <- link.Run(ctx) }()
		if err := waitWelcome(ctx, link, ended, 10*time.Second); err != nil {
			b.Close()
			return nil, nil, err
		}
	}
	return b, g, nil
}

// waitWelcome waits for the device connection's first welcome, so a session's first tool call does
// not race the connection. A server that does not answer in time is not fatal: the turn pauses until
// the link is up, which is the contract.
func waitWelcome(ctx context.Context, link *device.Link, ended <-chan error, limit time.Duration) error {
	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		if link.Welcome() != nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-ended:
			// The link gave up for good (a refused token, another client of this device).
			if err == nil {
				return ctx.Err()
			}
			if errors.Is(err, device.ErrUnauthorized) || errors.Is(err, device.ErrForbidden) {
				return fmt.Errorf("%w: %v", client.ErrUnauthorized, err)
			}
			return err
		case <-time.After(50 * time.Millisecond):
		}
	}
	return nil
}

// permute moves flags before positionals so `daedalus-cli "fix it" -p` parses as Claude Code's does.
func permute(fs *flag.FlagSet, args []string) []string {
	var flags, rest []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			rest = append(rest, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(a, "-") || a == "-" {
			rest = append(rest, a)
			continue
		}
		flags = append(flags, a)
		nameOnly := strings.TrimLeft(a, "-")
		if strings.Contains(nameOnly, "=") {
			continue
		}
		f := fs.Lookup(nameOnly)
		if f == nil {
			continue
		}
		if bf, ok := f.Value.(interface{ IsBoolFlag() bool }); ok && bf.IsBoolFlag() {
			continue
		}
		if i+1 < len(args) {
			flags = append(flags, args[i+1])
			i++
		}
	}
	return append(flags, rest...)
}

func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

func cmdMain(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet(name(), flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var g globals
	g.register(fs)
	var print, verbose, plain, cont, showVersion bool
	var format, resume, permission string
	var timeout time.Duration
	fs.BoolVar(&print, "p", false, "")
	fs.BoolVar(&print, "print", false, "")
	fs.BoolVar(&verbose, "verbose", false, "")
	fs.BoolVar(&plain, "plain", false, "")
	fs.BoolVar(&cont, "c", false, "")
	fs.BoolVar(&cont, "continue", false, "")
	fs.BoolVar(&showVersion, "version", false, "")
	fs.StringVar(&format, "output-format", "text", "")
	fs.StringVar(&resume, "r", "", "")
	fs.StringVar(&resume, "resume", "", "")
	fs.StringVar(&permission, "permission-mode", "", "")
	fs.DurationVar(&timeout, "timeout", 0, "")
	help := fs.Bool("h", false, "")
	fs.BoolVar(help, "help", false, "")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s: %v\n\n", name(), err)
		usage(stderr)
		return app.ExitUsage
	}
	if *help {
		usage(stdout)
		return 0
	}
	if showVersion {
		fmt.Fprintf(stdout, "daedalus-cli %s\n", version)
		return 0
	}
	if permission != "" && g.guard == "" {
		g.guard = permission
	}
	outFormat, err := app.ParseFormat(format)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", name(), err)
		return app.ExitUsage
	}
	prompt := strings.Join(fs.Args(), " ")
	if print && !isTerminal(stdin) {
		// Piped input is the prompt, or context for it, as `cat log | daedalus-cli -p "why?"`.
		piped, _ := io.ReadAll(io.LimitReader(stdin, 4<<20))
		if text := strings.TrimSpace(string(piped)); text != "" {
			if prompt == "" {
				prompt = text
			} else {
				prompt = prompt + "\n\n" + text
			}
		}
	}
	if print && strings.TrimSpace(prompt) == "" {
		fmt.Fprintf(stderr, "%s: -p needs a prompt, as an argument or on stdin\n", name())
		return app.ExitUsage
	}

	e, err := setup(g, true)
	if errors.Is(err, app.ErrNotPaired) && !print && isTerminal(stdin) && isTerminalWriter(stdout) {
		// A first start on a machine that knows its server but has no token pairs right here, with
		// the QR code in the terminal, and then opens the conversation.
		if code := cmdLogin(loginArgs(g), stdout, stderr); code != 0 {
			return code
		}
		e, err = setup(g, true)
	}
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", name(), err)
		if errors.Is(err, app.ErrNotPaired) {
			return app.ExitAuth
		}
		return app.ExitUsage
	}
	ctx, cancel := signalContext()
	defer cancel()
	if print && timeout > 0 {
		var cancelTimeout context.CancelFunc
		ctx, cancelTimeout = context.WithTimeout(ctx, timeout)
		defer cancelTimeout()
	}
	b, _, err := e.backend(ctx, !print)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", name(), err)
		return app.ExitCodeFor(err)
	}
	defer b.Close()

	if print {
		h := &app.Headless{Backend: b, Format: outFormat, Stdout: stdout, Stderr: stderr, Verbose: verbose, Resume: resume, Continue: cont}
		return h.Run(ctx, prompt)
	}
	req := startRequest(resume, cont, prompt)
	if err := runScreen(ctx, b, e, req, plain || !isTerminal(stdin) || !isTerminalWriter(stdout), stdin, stdout); err != nil {
		fmt.Fprintf(stderr, "%s: %v\n", name(), err)
		return app.ExitCodeFor(err)
	}
	return 0
}

// loginArgs carries the conversation's server and profile flags over to login.
func loginArgs(g globals) []string {
	var args []string
	if g.profile != "" {
		args = append(args, "--profile", g.profile)
	}
	if g.server != "" {
		args = append(args, g.server)
	}
	return args
}

func isTerminalWriter(w io.Writer) bool {
	f, ok := w.(*os.File)
	return ok && isTerminalFile(f)
}

func isTerminal(r io.Reader) bool {
	f, ok := r.(*os.File)
	return ok && isTerminalFile(f)
}

// isTerminalFile reports a character device: a terminal, as far as a static binary without a
// terminal library can tell.
func isTerminalFile(f *os.File) bool {
	st, err := f.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}
