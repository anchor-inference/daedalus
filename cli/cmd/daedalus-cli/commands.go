package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/app"
	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/config"
	"github.com/ascorblack/daedalus/cli/internal/device"
	"github.com/ascorblack/daedalus/cli/internal/exec"
)

func newFlags(cmd string) (*flag.FlagSet, *globals) {
	fs := flag.NewFlagSet(name()+" "+cmd, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	g := &globals{}
	g.register(fs)
	return fs, g
}

func fail(stderr io.Writer, err error) int {
	fmt.Fprintf(stderr, "%s: %v\n", name(), err)
	return app.ExitCodeFor(err)
}

// cmdLogin pairs this machine: a code and a QR code to approve in the app or in Telegram, then the
// token into the keychain.
func cmdLogin(args []string, stdout, stderr io.Writer) int {
	fs, g := newFlags("login")
	deviceName := fs.String("name", "", "what to call this machine (default: its host name)")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s login: %v\n", name(), err)
		return app.ExitUsage
	}
	if fs.NArg() > 0 {
		g.server = fs.Arg(0)
	}
	cfg, err := config.Load()
	if err != nil {
		return fail(stderr, err)
	}
	sel, err := cfg.Select(g.profile, g.server)
	if err != nil {
		return fail(stderr, err)
	}
	if sel.Profile.Server == "" {
		fmt.Fprintf(stderr, "%s login: name the server, e.g. %s login http://<host>:<port>\n", name(), name())
		return app.ExitUsage
	}
	dir, err := config.Dir()
	if err != nil {
		return fail(stderr, err)
	}
	store, err := auth.OpenStore(dir)
	if err != nil {
		return fail(stderr, err)
	}
	ctx, cancel := signalContext()
	defer cancel()
	c := client.New(sel.Profile.Server, "")
	c.UserAgent = "daedalus-cli/" + version
	ch, err := auth.Start(ctx, c, auth.ThisDevice(*deviceName, version))
	if err != nil {
		return fail(stderr, fmt.Errorf("start pairing with %s: %w", sel.Profile.Server, err))
	}
	color := os.Getenv("NO_COLOR") == "" && os.Getenv("TERM") != "dumb"
	if code, err := auth.QR(ch.VerificationURIComplete, color); err == nil {
		fmt.Fprint(stdout, code)
	}
	fmt.Fprintf(stdout, "\nApprove this terminal in Daedalus: scan the code, open %s,\nor press Allow in Telegram. Check that the app shows the same code:\n\n    %s\n\nWaiting for approval (the code expires in %d minutes)…\n",
		ch.VerificationURIComplete, ch.UserCode, max(1, ch.ExpiresIn/60))
	approved, err := auth.Wait(ctx, c, ch)
	if err != nil {
		code := fail(stderr, err)
		if errors.Is(err, auth.ErrDenied) || errors.Is(err, auth.ErrExpired) {
			return app.ExitAuth
		}
		return code
	}
	if err := app.SaveLogin(store, cfg, sel.Name, sel.Profile.Server, approved); err != nil {
		return fail(stderr, fmt.Errorf("approved, but the token could not be stored: %w", err))
	}
	fmt.Fprintf(stdout, "Paired as %q with %s. The token is in the %s.\n", approved.Device.Name, sel.Profile.Server, store.Backend())
	return 0
}

// cmdLogout forgets the token. The device stays listed on the server until it is revoked there;
// logout says so instead of pretending the token is gone everywhere.
func cmdLogout(args []string, stdout, stderr io.Writer) int {
	fs, g := newFlags("logout")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s logout: %v\n", name(), err)
		return app.ExitUsage
	}
	cfg, err := config.Load()
	if err != nil {
		return fail(stderr, err)
	}
	sel, err := cfg.Select(g.profile, g.server)
	if err != nil {
		return fail(stderr, err)
	}
	dir, err := config.Dir()
	if err != nil {
		return fail(stderr, err)
	}
	store, err := auth.OpenStore(dir)
	if err != nil {
		return fail(stderr, err)
	}
	if err := store.Delete(sel.Name); err != nil && !errors.Is(err, auth.ErrNotFound) {
		return fail(stderr, err)
	}
	p := cfg.Profiles[sel.Name]
	p.DeviceID, p.DeviceName = "", ""
	if sel.Known {
		cfg.Profiles[sel.Name] = p
		if err := cfg.Save(); err != nil {
			return fail(stderr, err)
		}
	}
	fmt.Fprintf(stdout, "Logged out of %s. The device stays in the app's Settings → Devices until you revoke it there.\n", orNone(p.Server))
	return 0
}

func orNone(s string) string {
	if s == "" {
		return "(no server)"
	}
	return s
}

// cmdStatus says where this terminal is connected and as what.
func cmdStatus(args []string, stdout, stderr io.Writer) int {
	fs, g := newFlags("status")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s status: %v\n", name(), err)
		return app.ExitUsage
	}
	e, err := setup(*g, false)
	if err != nil {
		return fail(stderr, err)
	}
	w := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintf(w, "Profile\t%s\n", e.sel.Name)
	fmt.Fprintf(w, "Server\t%s\n", e.sel.Profile.Server)
	fmt.Fprintf(w, "Client\tdaedalus-cli %s (%s/%s)\n", version, runtime.GOOS, runtime.GOARCH)
	fmt.Fprintf(w, "Guard\t%s\n", e.guard)
	if e.token == "" {
		fmt.Fprintf(w, "Credential\tnone: run %s login\n", name())
		w.Flush()
		return app.ExitAuth
	}
	fmt.Fprintf(w, "Credential\t%s\n", e.source)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if !client.IsDeviceToken(e.token) {
		fmt.Fprintf(w, "Device\tnone (the static API token cannot run tools here)\n")
		w.Flush()
		return 0
	}
	me, err := e.client.Me(ctx)
	if err != nil {
		fmt.Fprintf(w, "Server answers\t%v\n", err)
		w.Flush()
		return app.ExitCodeFor(err)
	}
	fmt.Fprintf(w, "Device\t%s (%s)\n", me.Device.Name, me.Device.ID)
	fmt.Fprintf(w, "Server version\t%s, contract %d\n", me.Server.Version, me.Server.Contract)
	fmt.Fprintf(w, "Connected\t%v\n", me.Device.Connected)
	fmt.Fprintf(w, "Sessions\t%d\n", len(me.Sessions))
	w.Flush()
	return 0
}

// cmdSessions lists recent conversations.
func cmdSessions(args []string, stdout, stderr io.Writer) int {
	fs, g := newFlags("sessions")
	limit := fs.Int("n", 20, "how many")
	here := fs.Bool("here", false, "only this folder's")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s sessions: %v\n", name(), err)
		return app.ExitUsage
	}
	e, err := setup(*g, true)
	if err != nil {
		return fail(stderr, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	rows, err := listSessions(ctx, e, *limit)
	if err != nil {
		return fail(stderr, err)
	}
	w := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tWHEN\tKIND\tSTATUS\tTITLE")
	for _, r := range rows {
		if *here && !r.Here {
			continue
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", r.ID, ago(r.LastMessageAt), r.Kind, r.Status, r.Title)
	}
	w.Flush()
	return 0
}

// listSessions is the resume list for a device and the plain session list for the API token.
func listSessions(ctx context.Context, e *env, limit int) ([]client.ResumeRow, error) {
	if client.IsDeviceToken(e.token) {
		return e.client.ResumeSessions(ctx, e.cwd, "", limit)
	}
	rows, err := e.client.Sessions(ctx, limit)
	if err != nil {
		return nil, err
	}
	out := make([]client.ResumeRow, 0, len(rows))
	for _, r := range rows {
		out = append(out, client.ResumeRow{ID: r.ID, Title: r.Title, Kind: r.Kind, Status: r.Status, LastMessageAt: r.LastMessageAt})
	}
	return out, nil
}

func ago(iso string) string {
	t, err := time.Parse(time.RFC3339Nano, iso)
	if err != nil {
		return "-"
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "now"
	case d < time.Hour:
		return strconv.Itoa(int(d.Minutes())) + "m ago"
	case d < 48*time.Hour:
		return strconv.Itoa(int(d.Hours())) + "h ago"
	}
	return strconv.Itoa(int(d.Hours()/24)) + "d ago"
}

// cmdResume opens a conversation, picking one from a numbered list when none is named.
func cmdResume(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs, g := newFlags("resume")
	plain := fs.Bool("plain", false, "line mode")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s resume: %v\n", name(), err)
		return app.ExitUsage
	}
	e, err := setup(*g, true)
	if err != nil {
		return fail(stderr, err)
	}
	ctx, cancel := signalContext()
	defer cancel()
	id := fs.Arg(0)
	if id == "" {
		rows, err := listSessions(ctx, e, 15)
		if err != nil {
			return fail(stderr, err)
		}
		if len(rows) == 0 {
			fmt.Fprintln(stderr, "no conversations to resume")
			return app.ExitFailed
		}
		for i, r := range rows {
			fmt.Fprintf(stdout, "%3d  %-8s %-9s %s\n", i+1, ago(r.LastMessageAt), r.Kind, r.Title)
		}
		fmt.Fprint(stdout, "\nResume which? ")
		line, _ := bufio.NewReader(stdin).ReadString('\n')
		n, err := strconv.Atoi(strings.TrimSpace(line))
		if err != nil || n < 1 || n > len(rows) {
			fmt.Fprintln(stderr, "nothing chosen")
			return app.ExitUsage
		}
		id = rows[n-1].ID
	}
	b, _, err := e.backend(ctx, true)
	if err != nil {
		return fail(stderr, err)
	}
	defer b.Close()
	if err := runScreen(ctx, b, e, startRequest(id, false, ""), *plain || !isTerminal(stdin), stdin, stdout); err != nil {
		return fail(stderr, err)
	}
	return 0
}

// cmdDoctor checks each link of the chain in order and says where it breaks.
func cmdDoctor(args []string, stdout, stderr io.Writer) int {
	fs, g := newFlags("doctor")
	if err := fs.Parse(permute(fs, args)); err != nil {
		fmt.Fprintf(stderr, "%s doctor: %v\n", name(), err)
		return app.ExitUsage
	}
	failed := false
	check := func(label string, err error, ok string) {
		if err != nil {
			failed = true
			fmt.Fprintf(stdout, "✗ %-22s %v\n", label, err)
			return
		}
		fmt.Fprintf(stdout, "✓ %-22s %s\n", label, ok)
	}
	fmt.Fprintf(stdout, "  %-22s daedalus-cli %s (%s/%s)\n", "client", version, runtime.GOOS, runtime.GOARCH)
	dir, err := config.Dir()
	check("configuration", err, dir)
	e, err := setup(*g, false)
	if err != nil {
		check("profile", err, "")
		return app.ExitFailed
	}
	check("profile", nil, e.sel.Name+" → "+e.sel.Profile.Server)
	sealed := ""
	if !e.store.Sealed() {
		sealed = " (not sealed: any process of yours can read it)"
	}
	check("credential store", nil, e.store.Backend()+sealed)
	if e.token == "" {
		check("credential", app.ErrNotPaired, "")
		return app.ExitAuth
	}
	check("credential", nil, "from "+e.source)
	executor, err := exec.New(e.log)
	if err == nil {
		err = executor.CheckRoot(e.cwd)
	}
	check("this folder", err, e.cwd+" can hold a session's tools")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if !client.IsDeviceToken(e.token) {
		_, err := e.client.Sessions(ctx, 1)
		check("server", err, "answers the API token")
		fmt.Fprintln(stdout, "  the API token cannot open the device connection: tools cannot run here")
		if failed {
			return app.ExitFailed
		}
		return 0
	}
	me, err := e.client.Me(ctx)
	check("server", err, fmt.Sprintf("version %s, device %s", me.Server.Version, me.Device.Name))
	if err != nil {
		return app.ExitCodeFor(err)
	}
	if me.Server.Contract != device.Protocol {
		check("contract", fmt.Errorf("the server speaks contract %d, this client %d", me.Server.Contract, device.Protocol), "")
	} else {
		check("contract", nil, strconv.Itoa(me.Server.Contract))
	}
	if me.Device.Connected {
		// Opening a second connection would replace the running client's.
		check("device connection", nil, "up (held by a running daedalus-cli)")
	} else {
		linkCtx, stop := context.WithTimeout(ctx, 10*time.Second)
		welcomed := make(chan device.Welcome, 1)
		link := device.New(device.Options{Server: e.client.Base(), Token: e.token, ClientVersion: version, Executor: executor,
			OnWelcome: func(w device.Welcome) {
				select {
				case welcomed <- w:
				default:
				}
				stop()
			}, Log: e.log})
		runErr := link.Run(linkCtx)
		stop()
		select {
		case w := <-welcomed:
			check("device connection", nil, fmt.Sprintf("welcomed, %d session(s) bound", len(w.Bound)))
		default:
			if runErr == nil {
				runErr = errors.New("no welcome within 10 s")
			}
			check("device connection", runErr, "")
		}
	}
	if failed {
		return app.ExitFailed
	}
	return 0
}
