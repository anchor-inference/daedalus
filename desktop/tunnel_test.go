package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func tunnelRig(t *testing.T) (*tunnels, Paths) {
	t.Helper()
	root := t.TempDir()
	p := Paths{Local: filepath.Join(root, "local"), Runtime: filepath.Join(root, "rt"), RuntimeLogs: filepath.Join(root, "logs"), RuntimeStamps: filepath.Join(root, "stamps"), Env: filepath.Join(root, ".env")}
	if err := os.MkdirAll(p.Local, 0o700); err != nil {
		t.Fatal(err)
	}
	tn := newTunnels(p, t.Logf)
	tn.find = func() string { return "" }
	tn.fetch = func(context.Context, download) error { t.Fatal("nothing should be fetched"); return nil }
	t.Setenv("PATH", filepath.Join(root, "no-tools-here"))
	return tn, p
}

func on() *bool             { v := true; return &v }
func off() *bool            { v := false; return &v }
func text(s string) *string { return &s }

func TestTheAddressIsOffUntilTheOperatorSaysOtherwise(t *testing.T) {
	tn, _ := tunnelRig(t)
	view := tn.View()
	if view.Enabled || view.State != "off" || view.Address != "" {
		t.Fatalf("%+v", view)
	}
	tn.Start(context.Background())
	if tn.process != nil {
		t.Fatal("a start raised a tunnel nobody asked for")
	}
}

func TestATokenGoesInAndNeverComesBack(t *testing.T) {
	tn, p := tunnelRig(t)
	secret := "2abcTHEtokenITSELF_defgh"
	if err := tn.Change(context.Background(), tunnelChange{Provider: text(tunnelNgrok), NgrokToken: text(secret)}); err != nil {
		t.Fatal(err)
	}
	view := tn.View()
	if !view.Providers[tunnelNgrok].Token {
		t.Fatal("the settings must be able to say a token is kept")
	}
	body, _ := json.Marshal(view)
	if strings.Contains(string(body), secret) {
		t.Fatalf("the view holds it: %s", body)
	}
	choice, _ := os.ReadFile(tunnelChoicePath(p))
	if strings.Contains(string(choice), secret) {
		t.Fatalf("the choice file holds it: %s", choice)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(filepath.Join(tunnelDir(p), "secrets.json"))
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%v %v", info, err)
		}
	}
	if strings.Contains(strings.Join(tunnelEnv(), "\n"), secret) {
		t.Fatal("the environment holds it")
	}
	if err := tn.Change(context.Background(), tunnelChange{Enabled: off()}); err != nil {
		t.Fatal(err)
	}
	if !tn.View().Providers[tunnelNgrok].Token {
		t.Fatal("a change that names no token leaves the token")
	}
	if err := tn.Change(context.Background(), tunnelChange{NgrokToken: text("")}); err != nil {
		t.Fatal(err)
	}
	if tn.View().Providers[tunnelNgrok].Token {
		t.Fatal("an empty token forgets it")
	}
}

func TestWhatIsNotAChangeIsRefused(t *testing.T) {
	tn, _ := tunnelRig(t)
	for name, change := range map[string]tunnelChange{
		"provider": {Provider: text("relay")},
		"token":    {NgrokToken: text("two words")},
		"newline":  {FrpToken: text("a\nb")},
		"domain":   {FrpDomain: text("a b.example")},
		"server":   {FrpServer: text("vps.example:notaport")},
	} {
		if err := tn.Change(context.Background(), change); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	if err := tn.Change(context.Background(), tunnelChange{FrpServer: text("vps.example:7000"), FrpDomain: text("agent.example.com")}); err != nil {
		t.Fatal(err)
	}
	view := tn.View().Providers[tunnelFrp]
	if view.Server != "vps.example:7000" || view.Domain != "agent.example.com" {
		t.Fatalf("%+v", view)
	}
}

func TestABuildWithNoTunneldSaysSoAndNothingElseSuffers(t *testing.T) {
	tn, _ := tunnelRig(t)
	if err := tn.Change(context.Background(), tunnelChange{Enabled: on(), Provider: text(tunnelCloudflare)}); err != nil {
		t.Fatal(err)
	}
	view := tn.View()
	if view.Tunneld || view.State != "failed" || view.Code != "no_tunneld" {
		t.Fatalf("%+v", view)
	}
}

func fakeTunneld(t *testing.T, status string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("a shell script stands in for tunneld")
	}
	script := filepath.Join(t.TempDir(), "tunneld")
	body := "#!/bin/sh\n/usr/bin/printf '%s' '" + status + "' > \"$2/status.json\"\nexec /bin/sleep 30\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return script
}

func waitForView(t *testing.T, tn *tunnels, what string, ok func(tunnelView) bool) tunnelView {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		view := tn.View()
		if ok(view) {
			return view
		}
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s: %+v", what, view)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestATunnelIsRaisedLeftOnAndClosedWithTheInstallation(t *testing.T) {
	tn, p := tunnelRig(t)
	script := fakeTunneld(t, `{"state":"up","address":"https://x.example","confirmed":true,"pids":[1,2],"wakes":3}`)
	tn.find = func() string { return script }
	ctx := context.Background()
	tn.Start(ctx)
	if err := tn.Change(ctx, tunnelChange{Enabled: on(), Provider: text(tunnelTailscale)}); err != nil {
		t.Fatal(err)
	}
	view := waitForView(t, tn, "up", func(v tunnelView) bool { return v.State == "up" })
	if view.Address != "https://x.example" || !view.Confirmed || len(view.PIDs) != 2 || view.Wakes != 3 {
		t.Fatalf("%+v", view)
	}
	var config tunnelConfig
	body, _ := os.ReadFile(filepath.Join(tunnelDir(p), "config.json"))
	if err := json.Unmarshal(body, &config); err != nil || config.DoorPort != "8764" || config.ServicesPort != "8766" {
		t.Fatalf("%s %v", body, err)
	}
	if strings.Contains(string(body), "8765") {
		t.Fatalf("the app's port has no business in here: %s", body)
	}
	tn.Stop(ctx)
	// Left on: the next launcher brings it back. Stopped: nothing answers, and the old status is gone.
	if !readTunnelChoice(p).Enabled {
		t.Fatal("closing the app is not turning the address off")
	}
	if _, err := os.Stat(filepath.Join(tunnelDir(p), "status.json")); err == nil {
		t.Fatal("a stale status would be read as the truth")
	}
	again := newTunnels(p, t.Logf)
	again.find = func() string { return script }
	again.Start(ctx)
	waitForView(t, again, "up again", func(v tunnelView) bool { return v.State == "up" })
	again.Stop(ctx)
}

func TestAStatusFileOutlivingItsProcessIsNotBelieved(t *testing.T) {
	tn, p := tunnelRig(t)
	if err := writeTunnelJSON(tunnelChoicePath(p), tunnelChoice{Enabled: true, Provider: tunnelCloudflare}); err != nil {
		t.Fatal(err)
	}
	if err := writeTunnelJSON(filepath.Join(tunnelDir(p), "status.json"), tunnelStatus{State: "up", Address: "https://stale.example", Confirmed: true}); err != nil {
		t.Fatal(err)
	}
	if view := tn.View(); view.State == "up" || view.Address != "" {
		t.Fatalf("%+v", view)
	}
}

func TestAClientIsFetchedOnlyForTheProviderThatNeedsIt(t *testing.T) {
	tn, p := tunnelRig(t)
	fetched := make(chan string, 2)
	tn.fetch = func(_ context.Context, d download) error {
		fetched <- d.name
		dir := tunnelToolsDir(p)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dir, exeName(d.name)), nil, 0o755)
	}
	tn.find = func() string { return fakeTunneld(t, `{"state":"starting"}`) }
	ctx := context.Background()
	if err := tn.Change(ctx, tunnelChange{Enabled: on(), Provider: text(tunnelCloudflare)}); err != nil {
		t.Fatal(err)
	}
	if got := <-fetched; got != "cloudflared" {
		t.Fatal(got)
	}
	waitForView(t, tn, "started", func(v tunnelView) bool { return v.State == "starting" && tn.process != nil })
	tn.Stop(ctx)
}

func TestNgrokIsNeverFetched(t *testing.T) {
	if _, ok := tunnelDownloadFor(tunnelNgrok); ok {
		t.Fatal("its terms ask written consent before its client is handed out")
	}
	if tunnelClient(Paths{}, "nothing") != "" {
		t.Fatal("no such client")
	}
}

func TestEveryPinnedClientIsWholeAndHasAHash(t *testing.T) {
	for _, table := range []map[string]download{cloudflaredDownloads, frpDownloads} {
		for _, platform := range []string{"linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64"} {
			d, ok := table[platform]
			if !ok || len(d.sha256) != 64 || d.size == 0 || len(d.only) != 1 || !strings.HasPrefix(d.url, "https://github.com/") {
				t.Fatalf("%s: %+v", platform, d)
			}
		}
	}
}

func TestAPlainBinaryIsWrittenUnderItsOneName(t *testing.T) {
	dir := t.TempDir()
	d := download{name: "x", kind: "binary", only: []string{"tool"}}
	if err := unpackIntoReporting(d, []byte("#!"), dir, nil); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(filepath.Join(dir, "tool")); err != nil || (runtime.GOOS != "windows" && info.Mode().Perm()&0o100 == 0) {
		t.Fatalf("%v %v", info, err)
	}
	if err := unpackIntoReporting(download{kind: "binary"}, nil, dir, nil); err == nil {
		t.Fatal("no name, nothing to write")
	}
}

func TestHeldStdinEndsWhenTheProcessIsGone(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("a shell script")
	}
	dir := t.TempDir()
	marker := filepath.Join(dir, "ended")
	child := &Process{Name: "c", Argv: []string{"/bin/sh", "-c", "cat > /dev/null; touch " + marker}, Dir: dir, HoldStdin: true, Log: t.Logf}
	ctx := context.Background()
	child.Start(ctx)
	time.Sleep(100 * time.Millisecond)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the child must not see an end while this process holds the pipe")
	}
	child.Stop(ctx)
}
