package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeAutostart is start at login on a machine of the test's own.
type fakeAutostart struct {
	on   bool
	fail error
}

func (f *fakeAutostart) Enabled() (bool, error) { return f.on, nil }
func (f *fakeAutostart) Enable() error {
	if f.fail != nil {
		return f.fail
	}
	f.on = true
	return nil
}
func (f *fakeAutostart) Disable() error {
	if f.fail != nil {
		return f.fail
	}
	f.on = false
	return nil
}
func (f *fakeAutostart) Where() string { return "the test's login items" }

// Every server a test makes answers the wizard's questions from here, never from the machine the
// tests run on: the real helpers run the operator's CLIs and write into their login items.
func init() {
	newSetupHelpers = func() setupHelpers {
		return setupHelpers{
			autostart: &fakeAutostart{},
			logins: func(context.Context) []CLILogin {
				return []CLILogin{{ID: "codex", SignedIn: true, How: "file"}, {ID: "claude"}, {ID: "grok"}}
			},
			docker: func(context.Context) bool { return false },
			client: &http.Client{},
		}
	}
}

func startedServer(t *testing.T, paths Paths) *Server {
	t.Helper()
	server := NewServer(NewApp(paths), 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { server.Stop(context.Background()) })
	return server
}

// bootOf reads the wizard's opening state out of the page, the way the page itself does.
func bootOf(t *testing.T, page string) setupBoot {
	t.Helper()
	const open = `<script type="application/json" id="setup-boot">`
	start := strings.Index(page, open)
	if start < 0 {
		t.Fatal("the page carries no boot state")
	}
	rest := page[start+len(open):]
	var boot setupBoot
	if err := json.Unmarshal([]byte(rest[:strings.Index(rest, "</script>")]), &boot); err != nil {
		t.Fatal(err)
	}
	return boot
}

func postSetup(t *testing.T, server *Server, form url.Values) (int, string) {
	t.Helper()
	form.Set("csrf", server.csrf)
	req, err := http.NewRequest(http.MethodPost, server.URL()+"setup", strings.NewReader(form.Encode()))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set(csrfHeader, server.csrf)
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

// The whole round: a first run posts every answer the wizard has, a waiting launcher is released,
// the files hold what the app and the key proxy read, and the page opened again shows the same
// answers back — with every secret masked and none of them in full anywhere in it.
func TestTheWizardsAnswersRoundTrip(t *testing.T) {
	paths := setupTempInstall(t)
	server := startedServer(t, paths)
	login := server.helpers.autostart.(*fakeAutostart)

	released := make(chan error, 1)
	go func() { released <- server.WaitForSetup(context.Background()) }()
	for !server.waiting.Load() {
		time.Sleep(time.Millisecond)
	}
	code, body := postSetup(t, server, url.Values{
		"lang": {"ru"}, "mode": {"native"}, "kind": {"local"}, "handoff": {"1"},
		"deepseek": {"sk-deepseek-0123456789"}, "openai": {"sk-openai-0123456789"}, "anthropic": {"sk-ant-0123456789abcd"},
		"local_url": {"http://127.0.0.1:11434/v1/"}, "local_key": {"local-secret-key-1234"}, "local_model": {"qwen-local"},
		"usd_per_day": {"35"}, "bot_token": {"123456:ABCDEFGHIJKLMNOPQRSTUVWX"}, "owner_id": {"424242"},
		"api_id": {"777"}, "api_hash": {"hash-0123456789abcdef"},
		"login": {"on"}, "browser": {"on"}, "voice": {"local"}, "port": {"18765"},
	})
	if code != http.StatusOK {
		t.Fatalf("the wizard's post answered %d: %s", code, body)
	}
	select {
	case err := <-released:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a first run waiting for the answers was never released")
	}

	env := readEnv(readFile(paths.Env))
	keys := readEnv(readFile(paths.KeyproxyEnv))
	for name, want := range map[string]string{
		"DAEDALUS_LOCAL_MODEL": "qwen-local", "DAEDALUS_VOICE": "local", "USD_PER_DAY": "35",
		"API_PORT": "18765", "DAEDALUS_FIXED_PORTS": "API_PORT", "OWNER_USER_ID": "424242",
	} {
		if env[name] != want {
			t.Errorf(".env %s = %q, want %q", name, env[name], want)
		}
	}
	for name, want := range map[string]string{
		"OPENAI_API_KEY": "sk-openai-0123456789", "ANTHROPIC_API_KEY": "sk-ant-0123456789abcd",
		"KEYPROXY_UPSTREAM_LOCAL": "http://127.0.0.1:11434/v1", "KEYPROXY_KEY_LOCAL": "local-secret-key-1234",
		"KEYPROXY_USD_PER_DAY": "35",
	} {
		if keys[name] != want {
			t.Errorf("keyproxy.env %s = %q, want %q", name, keys[name], want)
		}
	}
	if _, inAppEnv := env["KEYPROXY_KEY_LOCAL"]; inAppEnv {
		t.Error("the endpoint's key reached the app's environment")
	}
	if !login.on {
		t.Error("start at login was asked for and not turned on")
	}
	if !server.HandsOff() {
		t.Error("the wizard asked to open the app itself and the launcher will open it anyway")
	}
	if got := server.after.take(); strings.Join(got, ",") != "browser,speech" {
		t.Errorf("the extras to install after the start are %v", got)
	}

	page := get(t, &http.Client{Timeout: 5 * time.Second}, server.URL()+"setup")
	for _, secret := range []string{"sk-deepseek-0123456789", "sk-openai-0123456789", "sk-ant-0123456789abcd", "local-secret-key-1234", "123456:ABCDEFGHIJKLMNOPQRSTUVWX", "hash-0123456789abcdef"} {
		if strings.Contains(page, secret) {
			t.Fatalf("the page shows a stored secret in full: %s", secret)
		}
	}
	boot := bootOf(t, page)
	if !boot.Reconfigure || boot.Kind != "local" || boot.Local.Model != "qwen-local" || boot.Local.URL != "http://127.0.0.1:11434/v1" {
		t.Errorf("the page opens on %+v", boot)
	}
	if boot.Kept["keys.deepseek"] != "••••6789" || boot.Kept["keys.anthropic"] != "••••abcd" || boot.Kept["tg.token"] != "••••UVWX" || boot.Kept["local.key"] != "••••1234" {
		t.Errorf("the masks are %v", boot.Kept)
	}
	if boot.Limit != "35" || boot.TG.Owner != "424242" || boot.TG.APIID != "777" || boot.Adv.PortsAuto || boot.Adv.Port != "18765" || !boot.Adv.Login || boot.Adv.Voice != "local" {
		t.Errorf("the page does not show the answers back: %+v", boot)
	}
	if boot.Adv.Data != paths.Data || !boot.CLIs["codex"] || boot.CLIs["grok"] {
		t.Errorf("the folder or the logins are wrong: %+v", boot)
	}

	// Run again with nothing typed into the secret fields: everything stored stays.
	code, body = postSetup(t, server, url.Values{"mode": {"native"}, "kind": {"cloud"}, "usd_per_day": {"40"}, "login": {"off"}})
	if code != http.StatusOK {
		t.Fatalf("the second post answered %d: %s", code, body)
	}
	again := CurrentSetup(paths)
	if again.DeepseekKey != "sk-deepseek-0123456789" || again.LocalKey != "local-secret-key-1234" || again.BotToken != "123456:ABCDEFGHIJKLMNOPQRSTUVWX" || again.LocalModel != "qwen-local" || again.USDPerDay != "40" {
		t.Errorf("a re-run without the secrets lost some of them: %+v", again)
	}
	if login.on {
		t.Error("start at login was turned off and stayed on")
	}
}

func TestTheWizardsPostIsCheckedAgain(t *testing.T) {
	for _, bad := range []struct {
		field string
		form  url.Values
	}{
		{"limit", url.Values{"usd_per_day": {"5000"}}},
		{"tg.owner", url.Values{"owner_id": {"12ab"}}},
		{"adv.port", url.Values{"port": {"80"}}},
		{"local.url", url.Values{"kind": {"local"}, "local_url": {"ftp://127.0.0.1/"}, "local_model": {"m"}}},
		{"local.model", url.Values{"kind": {"local"}, "local_url": {"http://127.0.0.1:1/v1"}}},
		{"adv.voice", url.Values{"voice": {"loud"}}},
	} {
		if _, _, refused := parseSetupForm(bad.form); refused == nil || refused.Field != bad.field {
			t.Errorf("%v was accepted or refused for the wrong reason: %+v", bad.form, refused)
		}
	}
	paths := setupTempInstall(t)
	server := startedServer(t, paths)
	code, body := postSetup(t, server, url.Values{"usd_per_day": {"0"}})
	if code != http.StatusBadRequest || !strings.Contains(body, `"field":"limit"`) || paths.Configured() {
		t.Fatalf("a refused answer answered %d %s and configured=%v", code, body, paths.Configured())
	}
}

// The endpoint test asks only the address the operator typed, with the key they typed or, for the
// address already stored, the stored one — and the key never comes back in the answer.
func TestTheEndpointTestAsksTheTypedAddress(t *testing.T) {
	var sawKey string
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawKey = r.Header.Get("Authorization")
		if r.URL.Path != "/v1/models" {
			http.NotFound(w, r)
			return
		}
		fmt.Fprint(w, `{"data":[{"id":"alpha"},{"id":"beta"}]}`)
	}))
	defer endpoint.Close()
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{LocalURL: endpoint.URL + "/v1", LocalKey: "stored-key-123456", LocalModel: "alpha"}, ModeNative); err != nil {
		t.Fatal(err)
	}
	server := startedServer(t, paths)
	ask := func(headers map[string]string, body string) (int, EndpointProbe, string) {
		req, _ := http.NewRequest(http.MethodPost, server.URL()+"api/setup/endpoint", strings.NewReader(body))
		for k, v := range headers {
			req.Header.Set(k, v)
		}
		resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		var probe EndpointProbe
		_ = json.Unmarshal(raw, &probe)
		return resp.StatusCode, probe, string(raw)
	}
	if code, _, _ := ask(nil, `{"url":"`+endpoint.URL+`"}`); code != http.StatusForbidden {
		t.Fatalf("a test without the token answered %d", code)
	}
	token := map[string]string{csrfHeader: server.csrf}
	code, probe, raw := ask(token, `{"url":"`+endpoint.URL+`/v1"}`)
	if code != http.StatusOK || !probe.OK || strings.Join(probe.Models, ",") != "alpha,beta" {
		t.Fatalf("the stored endpoint answered %d %s", code, raw)
	}
	if sawKey != "Bearer stored-key-123456" || strings.Contains(raw, "stored-key-123456") {
		t.Fatalf("the stored key was not used, or came back: %q %s", sawKey, raw)
	}
	// Typed without /v1: the launcher finds it there and says so.
	code, probe, _ = ask(token, `{"url":"`+endpoint.URL+`","key":"typed-key-abcdef"}`)
	if code != http.StatusOK || !probe.OK || probe.URL != endpoint.URL+"/v1" || sawKey != "Bearer typed-key-abcdef" {
		t.Fatalf("the typed endpoint answered %d %+v with %q", code, probe, sawKey)
	}
	if code, _, _ := ask(token, `{"url":"file:///etc/passwd"}`); code != http.StatusBadRequest {
		t.Fatalf("a non-HTTP address answered %d", code)
	}
}

func TestDetectionAndTheLoginSwitchNeedTheToken(t *testing.T) {
	paths := setupTempInstall(t)
	server := startedServer(t, paths)
	if got := postBody(t, server.URL()+"api/setup/detect", nil, `{}`); got != http.StatusForbidden {
		t.Fatalf("detection without the token answered %d", got)
	}
	if got := postBody(t, server.URL()+"api/autostart", nil, `{"on":true}`); got != http.StatusForbidden {
		t.Fatalf("the login switch without the token answered %d", got)
	}
	req, _ := http.NewRequest(http.MethodPost, server.URL()+"api/setup/detect", strings.NewReader(`{}`))
	req.Header.Set(csrfHeader, server.csrf)
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var found struct {
		CLIs   map[string]bool `json:"clis"`
		Docker bool            `json:"docker"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&found)
	resp.Body.Close()
	if !found.CLIs["codex"] || found.CLIs["claude"] || found.Docker {
		t.Fatalf("detection answered %+v", found)
	}
	if got := postBody(t, server.URL()+"api/autostart", map[string]string{csrfHeader: server.csrf}, `{"on":true}`); got != http.StatusOK {
		t.Fatalf("the login switch answered %d", got)
	}
	if on, _ := server.helpers.autostart.Enabled(); !on {
		t.Fatal("the login switch did not turn start at login on")
	}
	server.helpers.autostart.(*fakeAutostart).fail = os.ErrPermission
	if got := postBody(t, server.URL()+"api/autostart", map[string]string{csrfHeader: server.csrf}, `{"on":false}`); got != http.StatusConflict {
		t.Fatalf("a switch the machine refused answered %d", got)
	}
}

// A port the operator fixed stays where they put it even when something else holds it; every
// other door still moves.
func TestAFixedPortIsNotMoved(t *testing.T) {
	probe := portProbe{
		taken: func(port int) bool { return port == 18765 || port == 13201 },
		spare: func() (int, error) { return 40000, nil },
	}
	roles := []portRole{{key: "API_PORT", name: "the app's port"}, {key: "KEYPROXY_PORT", name: "the key proxy's port"}}
	settings := map[string]string{"API_PORT": "18765", "KEYPROXY_PORT": "13201", "DAEDALUS_FIXED_PORTS": "API_PORT"}
	values, moves := planPorts(settings, roles, probe, false)
	if values[0].Value != "18765" {
		t.Fatalf("the fixed port moved to %s", values[0].Value)
	}
	if values[1].Value == "13201" || len(moves) != 1 {
		t.Fatalf("the free-choosing port did not move: %v %v", values, moves)
	}
}

func TestADockerInstallationReachesALoopbackEndpointThroughTheHost(t *testing.T) {
	for typed, want := range map[string]string{
		"http://127.0.0.1:11434/v1":  "http://host.docker.internal:11434/v1",
		"http://localhost:8080/v1":   "http://host.docker.internal:8080/v1",
		"http://192.0.2.10:8000/v1":  "http://192.0.2.10:8000/v1",
		"https://models.example/v1/": "https://models.example/v1/",
	} {
		if got := containerReachable(typed); got != want {
			t.Errorf("%s became %s, want %s", typed, got, want)
		}
		if typed != want && hostReachable(want) != strings.Replace(typed, "localhost", "127.0.0.1", 1) {
			t.Errorf("%s does not come back as typed: %s", want, hostReachable(want))
		}
	}
	paths := setupTempInstall(t)
	if err := os.MkdirAll(filepath.Dir(paths.BotEnv), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := WriteSetup(paths, Setup{LocalURL: "http://127.0.0.1:11434/v1", Browser: boolPointer(true)}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if got := readEnv(readFile(paths.KeyproxyEnv))["KEYPROXY_UPSTREAM_LOCAL"]; got != "http://host.docker.internal:11434/v1" {
		t.Fatalf("the container's proxy is pointed at %s", got)
	}
	if got := readEnv(readFile(paths.Env))["COMPOSE_PROFILES"]; got != "browser" {
		t.Fatalf("the browser image's profile is %q", got)
	}
}

func boolPointer(v bool) *bool { return &v }

func TestAStoredSecretIsMaskedToItsEnd(t *testing.T) {
	for in, want := range map[string]string{"": "", "short": "••••", "sk-0123456789": "••••6789"} {
		if got := mask(in); got != want {
			t.Errorf("mask(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTheWizardHasEveryWordInBothLanguages(t *testing.T) {
	var words map[Lang]map[string]string
	if err := json.Unmarshal([]byte(HeroWordsJSON()), &words); err != nil {
		t.Fatal(err)
	}
	if len(words[LangEN]) < 100 || len(words[LangEN]) != len(words[LangRU]) {
		t.Fatalf("the wizard has %d English and %d Russian words", len(words[LangEN]), len(words[LangRU]))
	}
	for _, key := range []string{"welcome.title", "say.welcome", "runs.missing", "secret.kept", "tip.data", "err.voice"} {
		if words[LangEN][key] == "" || words[LangRU][key] == "" {
			t.Errorf("the wizard has no %q", key)
		}
	}
	// Every key the script asks for is in the table: a missing one shows as its own name.
	script, err := uiFiles.ReadFile("ui/assets/setup/wizard.js")
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range quotedCalls(string(script), "t('") {
		if strings.HasSuffix(key, ".") || strings.Contains(key, "'") {
			continue // built from parts at run time; the parts are checked where they are used
		}
		if _, ok := words[LangEN][key]; !ok {
			t.Errorf("wizard.js asks for %q, which the table does not have", key)
		}
	}
}

// quotedCalls is every literal argument of a call that starts with prefix, as in t('name'), and
// only where prefix is the whole name: set('x') is not a call of t.
func quotedCalls(source, prefix string) []string {
	var out []string
	for at := 0; ; {
		i := strings.Index(source[at:], prefix)
		if i < 0 {
			return out
		}
		start := at + i
		at = start + len(prefix)
		if start > 0 {
			if c := source[start-1]; c == '_' || c == '.' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' {
				continue
			}
		}
		if j := strings.IndexByte(source[at:], '\''); j > 0 {
			out = append(out, source[at:at+j])
		}
	}
}
