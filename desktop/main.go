// Command daedalus-desktop turns a folder into a running Daedalus. It clones the two repositories
// with Docker (git is not expected on the host), asks for the few values the stack needs on a local
// page, writes the same env files a server install uses, and runs docker compose against the
// repository's own compose file. Docker is the only thing it expects to find installed, and it is
// never installed for the operator.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// followFile is `tail -f` for one file, for the launcher's own logs command: the platforms this
// runs on do not all have tail, and shelling out to one for twenty lines of Go is not worth the
// dependency.
func followFile(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("no log yet at %s: %w", path, err)
	}
	defer file.Close()
	for {
		if _, err := io.Copy(os.Stdout, file); err != nil {
			return err
		}
		time.Sleep(time.Second)
	}
}

// version is stamped at build time; a plain `go build` leaves it as it is.
var version = "dev"

const usage = `daedalus-desktop — the engine of the Daedalus desktop application

The application (Daedalus.exe, Daedalus.app, daedalus) starts this program itself; run it from a
terminal for the commands below. With no command and the application beside it, it opens the
application.

usage: daedalus-desktop [flags] [command]

commands:
  (none)      set up if needed, start the stack and open the app
  setup       ask the setup questions again and rewrite the configuration
  start       start the stack and open the app
  stop        stop the containers (they stay until started again)
  status      what is configured, what is running
  logs [-f]   the stack's logs
  update      move both checkouts to what is published and restart, once the data is
              protected — on Linux with ext4 switched to a copy while the data from before
              is kept whole, elsewhere a verified backup; a failed start puts the data
              back. Native mode only for now: Docker mode is refused (desktop/UPDATES.md)
  update status [-v]  the copies of the data folder kept by its switches, and anything that
              needs you: a write that may have been lost or may have missed the live data, a
              switch or an update that did not finish (exits non-zero then); -v also lists the
              processes the last switch could not inspect
  update resolve [--apply]  settle a switch that did not finish: says what is where, and with
              --apply files the tree that is not live for you, never touching the live one
  check-update  say whether a newer launcher release is published (installs nothing)
  upgrade     install a newer launcher release: asks first, stops the stack, protects the
              data as update does, replaces the launcher, updates and starts
              the stack, and puts the data and the launcher back if any of that fails.
              --yes skips the question; --rollback undoes an upgrade that did not finish.
              Native mode only for now: Docker mode is refused (desktop/UPDATES.md)
  open        open the app in the browser
  pair        print a fresh pairing link for signing in to the app
  uninstall   remove the containers, networks and volumes (Docker), or the downloaded
              runtime and this machine's local state (native); --keep-data keeps all of it,
              --remove-data deletes the data folder too
  import DIR  move the data folder of an earlier installation (one kept beside an older
              launcher) into the per-user folder this one uses; run it with Daedalus closed
  install X   native mode only: fetch a runtime extra — "node" for the skills that
              shell out to npx and for rebuilding the app, "browser" for the Chromium
              the agent's browser and the browser skills drive, "speech" for the engine that runs a
              speech model on this machine. None of the three is part of a first run,
              and Settings → Components in the app installs them without a terminal.
  restart     native mode only: stop the agent and start it again, so a component
              installed since it started is in force.

A daedalus:// link may be given instead of a command — daedalus://open/<session-id> opens that
conversation. A launcher that is already running is brought to the front and handed the link; a
second one never starts.

flags:
  --data DIR  where the checkouts, the keys and the environment live (default: the
              per-user folder — %LOCALAPPDATA%\Daedalus\data, ~/Library/Application
              Support/Daedalus/data, ~/.local/share/daedalus/data — or an older
              installation's data/ beside this executable until a start moves it there)
  --mode M    docker or native. Docker puts the agent in a container; native runs it on
              this machine out of a portable runtime the launcher downloads — lighter and
              faster, with no container boundary. Asked once on the first run and
              remembered; DAEDALUS_MODE does the same for a shell that sets it up once.
  --port N    the port the launcher's own page listens on (default: 8770)
  --setup     ask the setup questions even though the configuration exists
  --keep-data uninstall: keep the volumes and the data folder
  --remove-data  uninstall: delete the data folder as well — the conversations, the keys,
              the checkouts; what the desktop application's uninstaller runs when asked to
  -f          logs: follow
  --version   print the version and exit
  --yes       upgrade: do not ask (the data is still protected, and the rollback still happens)
  --rollback  upgrade: put back the launcher and the data an unfinished upgrade replaced
`

func main() {
	if runFixtureStack() {
		return
	}
	if err := run(os.Args[1:]); err != nil {
		// Under the shell nobody reads standard error: the window says why instead.
		shell.emit(shellEvent{Event: "fatal", Message: err.Error(), Log: shellLog()})
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}

type options struct {
	command  string
	link     string
	data     string
	extra    string
	mode     Mode
	port     int
	setup    bool
	keepData bool
	follow   bool
	help     bool
	version  bool

	// upgrade's flags; --finish is what the old launcher runs the new one with.
	yes      bool
	finish   bool
	rollback bool

	// The bridge an installer runs for an installation whose launcher predates upgrade.
	bridge  bool
	root    string
	archive string
	sums    string

	// apply makes `update resolve` act instead of only saying what it would do.
	apply bool
	// verbose makes `update status` list what it otherwise only counts.
	verbose bool

	// shell: started by the desktop application's shell, which shows everything (shell.go).
	shell bool
	// removeData makes uninstall delete the data folder as well; only the uninstaller asks for it.
	removeData bool
	// fromApp: an upgrade the application started and then closed for (shell.go). Its output goes
	// to upgrade.log, and the application is opened again when it ends, however it ended.
	fromApp bool
}

func run(argv []string) error {
	// The release workflow's step, not an operator's command: it builds the seed a release carries
	// (seed_make.go) and touches no installation.
	if len(argv) > 0 && argv[0] == "make-seed" {
		return makeSeedCommand(context.Background(), argv[1:])
	}
	opts, err := parseArgs(argv)
	if err != nil {
		return err
	}
	if opts.help {
		fmt.Print(usage)
		return nil
	}
	if opts.version {
		fmt.Println(version)
		return nil
	}
	if opts.shell {
		if opts.command != "" && opts.command != "start" {
			return fmt.Errorf("--shell starts the application; %s is a command for a terminal", opts.command)
		}
		shell = &shellLink{out: os.Stdout}
	} else if handOverToShell(opts) {
		return nil
	}
	paths, err := NewPaths(opts.data)
	if err != nil {
		return err
	}
	// Both signals, not only Ctrl+C. A native installation is stopped by the launcher going away,
	// so a SIGTERM from a service manager, a shutdown or a `kill` has to reach the same code path
	// that a Ctrl+C does — otherwise the launcher dies and leaves an agent running behind it.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	// An ordinary start of an installation an older launcher kept beside itself moves its data to
	// the per-user folder, once (relocate.go). Only a start does: every other command works on the
	// folder where it is, so nothing typed in a terminal moves anything as a side effect.
	var moved []string
	note := func(format string, args ...any) { moved = append(moved, fmt.Sprintf(format, args...)) }
	if opts.data == "" && (opts.command == "" || opts.command == "start") {
		if target, ok := relocatableFromHere(paths.Data); ok {
			if relocated, err := relocateData(ctx, paths.Data, target, note); err != nil {
				note("the data folder stays at %s: %v", paths.Data, err)
			} else {
				paths = relocated
			}
		}
	}
	if shell != nil {
		var quit context.CancelFunc
		ctx, quit = context.WithCancel(ctx)
		defer quit()
		if file, err := openShellLog(paths); err == nil {
			os.Stdout, os.Stderr = file, file
			shell.log = file.Name()
		}
		shellQuit = quit
	}
	if opts.fromApp {
		if opts.command != "upgrade" {
			return errors.New("--from-app is for the upgrade the application starts")
		}
		if file, err := openLocalLog(paths, "upgrade.log"); err == nil {
			os.Stdout, os.Stderr = file, file
			fmt.Printf("\n%s upgrade started by the application\n", time.Now().UTC().Format(time.RFC3339))
		}
		defer applicationReopener()()
	}
	app := NewApp(paths)
	for _, line := range moved {
		app.log("%s", line)
	}
	mode, err := ResolveMode(paths, opts.mode)
	if err != nil {
		return err
	}
	app.SetMode(mode)
	if opts.mode != ModeUnset && StoredMode(paths) == ModeUnset {
		// A mode named on the command line of a first run is the choice, not an override for one
		// run: the launcher would otherwise ask the question again on the next start.
		if err := StoreMode(paths, opts.mode); err != nil {
			return err
		}
	}

	switch opts.command {
	case "upgrade":
		return UpgradeCommand(ctx, app, opts)
	case "check-update":
		return CheckUpdateCommand(ctx, paths)
	case "update":
		// Reading the switches' records and settling one that stopped are what an interrupted
		// state needs most; neither waits for it to be cleared.
		if opts.extra != "" {
			return switchReportCommand(paths, opts)
		}
		if err := InterruptedUpgrade(paths); err != nil {
			return err
		}
	case "status", "logs":
	default:
		// Starting, stopping or updating on top of an upgrade that neither finished nor was undone
		// would run one launcher's stack on data the other may have migrated.
		if err := InterruptedUpgrade(paths); err != nil {
			return err
		}
	}

	switch opts.command {
	case "", "start":
		return startCommand(ctx, app, opts)
	case "setup":
		opts.setup = true
		return setupCommand(ctx, app, opts)
	case "stop":
		return app.Stop(ctx)
	case "status":
		app.PrintStatus(ctx)
		return nil
	case "logs":
		if app.Native() {
			return printNativeLogs(paths, opts.follow)
		}
		if err := CheckDocker(ctx); err != nil {
			return err
		}
		if err := WriteOverride(paths); err != nil {
			return err
		}
		args := []string{"logs", "--tail", "200"}
		if opts.follow {
			args = append(args, "--follow")
		}
		return composeStream(ctx, paths, true, args...)
	case "update":
		return app.Update(ctx)
	case "open":
		if FocusRunning(ctx, paths, opts.link) {
			fmt.Println("the launcher already running here was brought to the front")
			return nil
		}
		url, err := app.Open(ctx)
		if err != nil {
			fmt.Println("open this yourself:", url)
			return nil
		}
		fmt.Println("opened", url)
		return nil
	case "pair":
		url, err := app.Pair(ctx)
		if err != nil {
			return err
		}
		fmt.Println(url)
		return nil
	case "install":
		if opts.extra == "" {
			return errors.New("install takes the name of a runtime extra: node, browser or speech")
		}
		return app.InstallExtra(ctx, opts.extra)
	case "restart":
		return app.Restart(ctx)
	case "uninstall":
		if opts.removeData && opts.keepData {
			return errors.New("--keep-data and --remove-data say opposite things")
		}
		return app.Uninstall(ctx, opts.keepData, opts.removeData)
	case "import":
		if opts.extra == "" {
			return errors.New("import takes the data folder of an earlier installation")
		}
		return importCommand(ctx, app, opts)
	default:
		return fmt.Errorf("no such command: %s\n\n%s", opts.command, usage)
	}
}

// startCommand is the whole first run: the page for the questions when there are questions to ask,
// then the stack, then the app. What shows it is decided once, by what the machine can do — the
// launcher's own window, a browser in application mode, or a tab — and everything after that is the
// same whichever it turned out to be. Closing it leaves the containers running.
func startCommand(ctx context.Context, app *App, opts options) error {
	// A second launch is not a second installation. Two launchers reconciling one compose project,
	// two windows on one app and two answers on one port are all the same mistake, so the second
	// hands its link to the first, asks it to come to the front, and stops.
	if FocusRunning(ctx, app.paths, opts.link) {
		if shell != nil {
			// Under the shell, a launcher already running on this installation is one started from a
			// terminal or by an older installation's shortcut; its window is not this one, and two
			// windows on one installation are what the single start exists to prevent.
			return fmt.Errorf("Daedalus is already running on %s, started outside this window; close it there first", app.paths.Data)
		}
		fmt.Println("Daedalus is already running here; it has been brought to the front.")
		return nil
	}
	if err := app.paths.EnsureDirs(); err != nil {
		return err
	}
	// This launcher runs the stack from now until it exits, and nothing else may change the
	// installation meanwhile — an upgrade, an update from a terminal, a second launcher whose page
	// could not be found.
	lock, err := AcquireLock(app.paths, "launcher")
	if err != nil {
		return err
	}
	defer lock.Release()
	app.lock = lock
	// A data folder from before the runtime moved out of it: carried over and moved aside once,
	// before anything reads the old records or starts a child.
	if err := migrateLegacyRuntime(ctx, app.paths, app.log); err != nil {
		return fmt.Errorf("moving the runtime out of the data folder: %w", err)
	}
	// Links are registered with the desktop on a first start when there is no application to do it:
	// the installers and the shell register daedalus:// for the shell, and a launcher started under
	// it or beside it must not point the links back at itself. Nothing depends on it working.
	if shell == nil {
		if exe, err := os.Executable(); err == nil && shellExecutable(exe) == "" {
			if err := RegisterScheme(app.paths); err != nil {
				fmt.Fprintln(os.Stderr, "daedalus:// links are not registered with this desktop:", err.Error())
			}
		}
	}
	server := NewServer(app, opts.port)
	if err := server.Start(); err != nil && opts.port == defaultPort {
		// The usual port is taken — another installation's launcher, most often. Nothing finds this
		// page by its port (a second start reads launcher.json, the shell is told the address), so
		// any free one serves as well.
		fmt.Fprintln(os.Stderr, err.Error())
		server = NewServer(app, 0)
		err = server.Start()
		if err != nil {
			fmt.Fprintln(os.Stderr, err.Error())
			server = nil
		} else {
			app.log("the launcher's page port %d is taken by another program; the page is at %s instead", defaultPort, server.URL())
		}
	} else if err != nil {
		// A page that cannot listen is not a reason to refuse to start the stack.
		fmt.Fprintln(os.Stderr, err.Error())
		server = nil
	}
	if server == nil {
		if shell != nil {
			return errors.New("the launcher's page could not listen on any port of this machine")
		}
		if !app.paths.Configured() || opts.setup {
			return errors.New("the setup page needs a free port: pass --port")
		}
		// No page to show and nothing to keep open: the stack is started, the app is opened, and
		// the terminal is the only place a failure can be read, so it is returned.
		if err := app.Start(ctx); err != nil {
			return err
		}
		url := app.OpenURL(ctx)
		fmt.Println("opening", url)
		_ = OpenBrowser(ctx, url)
		return nil
	}
	defer server.Stop(context.Background())
	// The surface is made after the page is listening, because the first thing it shows is that
	// page: the status, the buttons, and on a first start the questions.
	announceLastUpgrade(app)
	surface := OpenSurface(app.paths, server.URL(), shell)
	server.OnFocus(surface.Focus)
	if shell != nil {
		app.show = func(url string) { surface.Show(ctx, url) }
		go shell.listen(os.Stdin, func(link string) {
			target := ""
			if IsDeepLink(link) {
				target = DeepLinkTarget(link, AppURL(APIPort(app.paths), app.Lang()))
			}
			surface.Focus(ctx, target)
		}, shellQuit)
	}
	go bringUp(ctx, app, server, surface, opts)
	surface.Run(ctx)
	// Native mode does not leave an agent behind a closed launcher: it is a process of this one's,
	// with no restart policy and nothing to show that it is there. Docker mode does, because a
	// container is visible in `docker ps` and comes back with the machine.
	app.StopOnQuit()
	return nil
}

// printNativeLogs shows the supervisor's log, which in native mode is a file the launcher owns
// rather than something docker holds.
func printNativeLogs(paths Paths, follow bool) error {
	path := filepath.Join(paths.RuntimeLogs, "supervisor.log")
	if !follow {
		body, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("no log yet at %s: %w", path, err)
		}
		os.Stdout.Write(body)
		return nil
	}
	return followFile(path)
}

// bringUp is the work, off the thread the window needs: the questions, the stack, and then the app
// in whatever is showing it. It reports nothing back — every failure it meets is already on the
// launcher's page, which is what the operator is looking at.
func bringUp(ctx context.Context, app *App, server *Server, surface *Surface, opts options) {
	// A newer launcher is announced from the start, and only announced: installing it is the
	// operator's (upgrade.go). From the start and not once the stack is up, because a stack that
	// does not come up is the installation a newer release may be what fixes. The app shows it too,
	// in its own notification centre (daedalus/extensions/launcher_updates.py).
	show := desktopNotifier(ctx, app, surface)
	go app.CheckReleases(ctx, show)
	// showing records that the launcher's page is already in front of the operator, so it is not
	// opened a second time in another tab. A window is already showing it.
	showing := surface.Windowed()
	if !app.paths.Configured() || opts.setup {
		fmt.Println("Set Daedalus up at", server.URL())
		if !showing {
			_ = OpenBrowser(ctx, server.URL())
			showing = true
		}
		if err := server.WaitForSetup(ctx); err != nil {
			return
		}
	}
	if !showing && Bundled() {
		// Started from Finder the browser is the only window there is, and a first start pulls
		// several gigabytes before the app itself answers. The launcher's page is where that
		// progress is, so it is opened before the work rather than after it.
		_ = OpenBrowser(ctx, server.URL())
		showing = true
	}
	if err := app.Start(ctx); err != nil {
		// Double-clicked from Finder there is no terminal to read and no shell to try again in, and
		// the commonest failure by far — Docker not installed, or installed and not started — is one
		// the operator fixes in a minute and then wants a button for. App.Start has already recorded
		// what happened for the page to show, so the launcher stays on that page.
		fmt.Fprintln(os.Stderr, err.Error())
		if !showing {
			_ = OpenBrowser(ctx, server.URL())
		}
		fmt.Printf("The launcher is at %s — it says what went wrong, and starts the stack once that is fixed.\n", server.URL())
		return
	}
	url := app.OpenURL(ctx)
	if opts.link != "" {
		// Opened by following a link, what the operator asked for is the thing at the end of it,
		// not the app's front page.
		url = DeepLinkTarget(opts.link, AppURL(APIPort(app.paths), app.Lang()))
	}
	fmt.Println("opening", url)
	surface.Show(ctx, url)
	if !surface.Windowed() {
		after := "the stack keeps running"
		if app.Native() {
			after = "the agent stops with it, and a run in flight resumes at the next start"
		}
		fmt.Printf("The launcher is at %s — leave it running for the buttons, or close it with Ctrl+C: %s.\n", server.URL(), after)
	}
	watch(ctx, app, show)
}

// desktopNotifier is how the launcher tells the desktop that the installation has something for
// the operator. A machine with no way to show a notification says so once and is not asked again.
// A click the launcher is told about (Linux) brings the surface forward on the thing the
// notification is about.
func desktopNotifier(ctx context.Context, app *App, surface *Surface) func(Notification) error {
	var mu sync.Mutex
	off := false
	open := func(link string) { surface.Focus(ctx, link) }
	show := func(n Notification) error {
		if shell != nil {
			// The shell shows it as the application itself, and a click comes back to its window.
			shell.emit(shellEvent{Event: "notify", Title: n.Title, Body: n.Body, URL: notificationTarget(app, n)})
			return nil
		}
		mu.Lock()
		defer mu.Unlock()
		if off {
			return errors.New("desktop notifications are off")
		}
		if err := Notify(n, open); err != nil {
			off = true
			app.log("desktop notifications are off: %v", err)
			return err
		}
		return nil
	}
	return show
}

// watch passes on what the app's router marks for the desktop, for as long as the launcher runs.
func watch(ctx context.Context, app *App, show func(Notification) error) {
	app.Watch(ctx, func(n Notification) { _ = show(n) })
}

// setupCommand asks the questions and stops there, for an operator who wants to change a key
// without restarting anything.
func setupCommand(ctx context.Context, app *App, opts options) error {
	server := NewServer(app, opts.port)
	if err := server.Start(); err != nil {
		return err
	}
	defer server.Stop(context.Background())
	fmt.Println("Set Daedalus up at", server.URL())
	_ = OpenBrowser(ctx, server.URL()+"setup")
	if err := server.WaitForSetup(ctx); err != nil {
		return err
	}
	fmt.Println("Written. Run `daedalus-desktop` to start the stack with it.")
	return nil
}

// parseArgs takes the command out of the argument list wherever it sits, so `logs -f` and
// `-f logs` both work. The flags are few and long-form, so this stays smaller than flag.FlagSet
// juggling one set per command.
func parseArgs(argv []string) (options, error) {
	opts := options{port: defaultPort}
	for i := 0; i < len(argv); i++ {
		arg := argv[i]
		value := ""
		if name, rest, ok := strings.Cut(arg, "="); ok {
			arg, value = name, rest
		}
		next := func() (string, error) {
			if value != "" {
				return value, nil
			}
			if i+1 >= len(argv) {
				return "", fmt.Errorf("%s needs a value", arg)
			}
			i++
			return argv[i], nil
		}
		switch arg {
		case "-h", "--help", "help":
			opts.help = true
		case "--version":
			opts.version = true
		case "--setup":
			opts.setup = true
		case "--keep-data":
			opts.keepData = true
		case "--remove-data":
			opts.removeData = true
		case "--shell":
			opts.shell = true
		case "--from-app":
			opts.fromApp = true
		case "--apply":
			opts.apply = true
		case "-v", "--verbose":
			opts.verbose = true
		case "-f", "--follow":
			opts.follow = true
		case "--yes", "-y":
			opts.yes = true
		case "--finish":
			opts.finish = true
		case "--rollback":
			opts.rollback = true
		case "--bridge":
			opts.bridge = true
		case "--root", "--archive", "--sums":
			value, err := next()
			if err != nil {
				return opts, err
			}
			switch arg {
			case "--root":
				opts.root = value
			case "--archive":
				opts.archive = value
			default:
				opts.sums = value
			}

		case "--data":
			data, err := next()
			if err != nil {
				return opts, err
			}
			opts.data = data
		case "--mode":
			value, err := next()
			if err != nil {
				return opts, err
			}
			mode, err := ParseMode(value)
			if err != nil {
				return opts, err
			}
			opts.mode = mode
		case "--port":
			port, err := next()
			if err != nil {
				return opts, err
			}
			// Atoi and not Sscanf: Sscanf reads "8770abc" as 8770 and reports no error, and a
			// mistyped port that silently becomes another one is worse than a refusal.
			number, err := strconv.Atoi(strings.TrimSpace(port))
			if err != nil || number < 0 || number > 65535 {
				return opts, fmt.Errorf("--port takes a number, not %q", port)
			}
			opts.port = number
		default:
			if strings.HasPrefix(arg, "-") {
				return opts, fmt.Errorf("no such flag: %s\n\n%s", arg, usage)
			}
			// A link is what the operating system passes when the operator follows one, and it
			// arrives in the place a command would. It is not a command: it says what to show.
			if IsDeepLink(arg) {
				opts.link = arg
				continue
			}
			if opts.command == "update" && opts.extra == "" && (arg == "status" || arg == "resolve") {
				// What the data folder's switches left behind, and settling one that stopped.
				opts.extra = arg
				continue
			}
			if (opts.command == "install" || opts.command == "import") && opts.extra == "" {
				// The one command that takes an argument of its own: which extra to fetch.
				opts.extra = arg
				continue
			}
			if opts.command != "" {
				return opts, fmt.Errorf("one command at a time: %s and %s", opts.command, arg)
			}
			opts.command = arg
		}
	}
	return opts, nil
}
