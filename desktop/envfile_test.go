package main

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

func TestMergeEnvReplacesInPlaceAndKeepsTheRest(t *testing.T) {
	existing := "# a comment\nUSD_PER_DAY=5\n\nMY_OWN=keep-me\nexport SEARXNG_SECRET=old\n"
	got := mergeEnv(existing, []envVar{
		{"USD_PER_DAY", "20"},
		{"SEARXNG_SECRET", "new"},
		{"API_PORT", "8765"},
	})
	want := "# a comment\nUSD_PER_DAY=20\n\nMY_OWN=keep-me\nSEARXNG_SECRET=new\nAPI_PORT=8765\n"
	if got != want {
		t.Fatalf("merged file is\n%q\nwant\n%q", got, want)
	}
}

func TestMergeEnvDoesNotMatchAKeyThatMerelyStartsTheSame(t *testing.T) {
	got := mergeEnv("OPENAI_API_KEY_OLD=x\n", []envVar{{"OPENAI_API_KEY", "new"}})
	if !strings.Contains(got, "OPENAI_API_KEY_OLD=x") || !strings.Contains(got, "OPENAI_API_KEY=new") {
		t.Fatalf("a prefix match ate a line: %q", got)
	}
}

func TestReadEnvUnquotesAndSkipsComments(t *testing.T) {
	env := readEnv("# c\nA=\"one\"\nB='two'\n export C=three \nbroken\n")
	for key, want := range map[string]string{"A": "one", "B": "two", "C": "three"} {
		if env[key] != want {
			t.Fatalf("%s = %q, want %q", key, env[key], want)
		}
	}
	if _, ok := env["broken"]; ok {
		t.Fatal("a line without = became a variable")
	}
}

func TestCleanStripsWhatAPastedKeyDragsAlong(t *testing.T) {
	if got := clean("  sk-abc\r\n"); got != "sk-abc" {
		t.Fatalf("clean = %q", got)
	}
}

func TestEnvUpdatesPointAtTheDataFolderAndLoopback(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	env := map[string]string{}
	for _, v := range envUpdates(paths, Setup{}, map[string]string{}, "secret", "/home/someone") {
		env[v.Key] = v.Value
	}
	if env["DAEDALUS_COMPOSE_PROJECT_DIR"] != paths.Bot || env["DAEDALUS_COMPOSE_FILE"] != paths.Compose || env["DAEDALUS_CORE_PROJECT_DIR"] != paths.Core {
		t.Fatalf("the rebuilder's paths are not the data folder's: %v", env)
	}
	if !filepath.IsAbs(env["DAEDALUS_COMPOSE_PROJECT_DIR"]) {
		t.Fatal("the rebuilder resolves its paths on the host: they must be absolute")
	}
	if env["SERVICES_PUBLIC_HOST"] != "127.0.0.1" {
		t.Fatalf("SERVICES_PUBLIC_HOST = %q", env["SERVICES_PUBLIC_HOST"])
	}
	if env["MINIAPP_PUBLIC_URL"] != "" {
		t.Fatalf("a desktop install has no public address: %q", env["MINIAPP_PUBLIC_URL"])
	}
	if env["USD_PER_DAY"] != "20" {
		t.Fatalf("the default cap is 20, got %q", env["USD_PER_DAY"])
	}
	if env["DAEDALUS_SECRETS_FILE"] != paths.KeyproxyEnv || env["DAEDALUS_SSH_DIR"] != paths.SSH {
		t.Fatalf("the secrets are not where the launcher put them: %v", env)
	}
	if env["DAEDALUS_HARNESS_HOME"] != "/home/someone" {
		t.Fatalf("the CLI logins are mounted from the home directory, got %q", env["DAEDALUS_HARNESS_HOME"])
	}
}

func TestWriteSetupKeepsProviderKeysOutOfTheCheckout(t *testing.T) {
	paths := setupTempInstall(t)
	setup := Setup{DeepseekKey: "sk-deepseek", OpenrouterKey: "sk-router", ProviderKeys: map[string]string{"kimi_coding": "sk-kimi"}, BotToken: "123:abc", OwnerID: "42", USDPerDay: "7"}
	if err := WriteSetup(paths, setup, ModeDocker); err != nil {
		t.Fatal(err)
	}
	env := readFile(paths.Env)
	if strings.Contains(env, "sk-deepseek") || strings.Contains(env, "sk-router") || strings.Contains(env, "sk-kimi") {
		t.Fatal("a provider key reached .env, which is mounted into the agent container")
	}
	keys := readEnv(readFile(paths.KeyproxyEnv))
	if keys["DEEPSEEK_API_KEY"] != "sk-deepseek" || keys["KIMI_CODING_API_KEY"] != "sk-kimi" || keys["KEYPROXY_USD_PER_DAY"] != "7" {
		t.Fatalf("the key proxy's file is wrong: %v", keys)
	}
	if info, err := os.Stat(paths.KeyproxyEnv); err != nil {
		t.Fatal(err)
	} else if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		// Windows has no permission bits for Go to report: every writable file reads 0666 there.
		t.Fatalf("the secrets file is %v, want 0600", info.Mode().Perm())
	}
	if readFile(paths.BotEnv) != env {
		t.Fatal("the checkout's .env is not the one compose reads for the container")
	}
}

func TestWriteSetupKeepsTheSearxngSecretAndUntypedKeys(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	first := readEnv(readFile(paths.Env))["SEARXNG_SECRET"]
	if len(first) < 32 {
		t.Fatalf("the SearXNG secret is too short to be one: %q", first)
	}
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-one"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if second := readEnv(readFile(paths.Env))["SEARXNG_SECRET"]; second != first {
		t.Fatal("re-running setup rotated the SearXNG secret")
	}
	if got := CurrentSetup(paths); got.DeepseekKey != "sk-one" {
		t.Fatalf("the setup page would open with an empty key field: %+v", got)
	}
}

// Setup is re-run to change one value, and a form that carries nothing for the rest — a browser
// that did not fill a password field back in — must leave them as they are.
func TestWriteSetupLeavesAloneWhatTheFormDidNotCarry(t *testing.T) {
	paths := setupTempInstall(t)
	full := Setup{DeepseekKey: "sk-deepseek", OpenrouterKey: "sk-router", BotToken: "123:abc", OwnerID: "42", APIID: "7", APIHash: "hash", USDPerDay: "9"}
	if err := WriteSetup(paths, full, ModeDocker); err != nil {
		t.Fatal(err)
	}
	// A public address is not on the setup page at all; passkeys are enrolled against that host.
	if err := os.WriteFile(paths.Env, []byte(readFile(paths.Env)+"MINIAPP_PUBLIC_URL=https://example.test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := WriteSetup(paths, Setup{USDPerDay: "9"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	got := CurrentSetup(paths)
	if !reflect.DeepEqual(got, full) {
		t.Fatalf("an empty form rewrote the configuration: %+v, want %+v", got, full)
	}
	if url := readEnv(readFile(paths.Env))["MINIAPP_PUBLIC_URL"]; url != "https://example.test" {
		t.Fatalf("the public address the operator set by hand is now %q", url)
	}
	if cap := readEnv(readFile(paths.KeyproxyEnv))["KEYPROXY_USD_PER_DAY"]; cap != "9" {
		t.Fatalf("the key proxy's cap is %q", cap)
	}
}

func TestWriteSetupEmptiesOnlyWhatWasAskedFor(t *testing.T) {
	paths := setupTempInstall(t)
	if err := WriteSetup(paths, Setup{DeepseekKey: "sk-deepseek", OpenrouterKey: "sk-router", BotToken: "123:abc", OwnerID: "42"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if err := WriteSetup(paths, Setup{Clear: map[string]bool{"deepseek": true, "bot_token": true}}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	got := CurrentSetup(paths)
	if got.DeepseekKey != "" || got.BotToken != "" {
		t.Fatalf("a ticked field was not emptied: %+v", got)
	}
	if got.OpenrouterKey != "sk-router" || got.OwnerID != "42" {
		t.Fatalf("clearing one field emptied another: %+v", got)
	}
	if (Setup{BotToken: got.BotToken}).Telegram() {
		t.Fatal("clearing the bot token must turn Telegram off")
	}
}

// A desktop install keeps the default cap only until the operator names one, and never rewrites
// their figure with it afterwards.
func TestTheDailyCapFallsBackToWhatIsInForce(t *testing.T) {
	if got := dailyCap("", ""); got != "20" {
		t.Fatalf("a fresh install starts at %q", got)
	}
	if got := dailyCap("", "35"); got != "35" {
		t.Fatalf("the cap in force became %q", got)
	}
	if got := dailyCap(" 12 ", "35"); got != "12" {
		t.Fatalf("the cap the form carried became %q", got)
	}
}

func TestTelegramIsOffWithoutAToken(t *testing.T) {
	if (Setup{}).Telegram() {
		t.Fatal("no token, no Telegram")
	}
	if !(Setup{BotToken: "123:abc"}).Telegram() {
		t.Fatal("a token turns Telegram on")
	}
}

// setupTempInstall lays out an empty install with a checkout directory, which is what a cloned
// repository would give the launcher.
func setupTempInstall(t *testing.T) Paths {
	t.Helper()
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(paths.Bot, "deploy"), 0o755); err != nil {
		t.Fatal(err)
	}
	return paths
}

// The bot token is full control of one of the two front doors. In Docker mode the copy in the
// checkout is what compose interpolates; natively there is no compose, and the checkout is a
// directory the agent works in freely — so the copy put the token in the one place the rules open.
func TestNativeModeDoesNotCopyTheTokenIntoTheCheckout(t *testing.T) {
	paths := setupTempInstall(t)
	if err := os.MkdirAll(paths.Bot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := WriteSetup(paths, Setup{BotToken: "123:abc", OwnerID: "42"}, ModeNative); err != nil {
		t.Fatal(err)
	}
	if readFile(paths.BotEnv) != "" {
		t.Fatalf("the bot token reached the checkout: %q", readFile(paths.BotEnv))
	}
	if !strings.Contains(readFile(paths.Env), "123:abc") {
		t.Fatal("the launcher's own env file did not get the token")
	}
	// Docker still gets it: compose reads ../.env as the agent container's environment.
	if err := WriteSetup(paths, Setup{BotToken: "123:abc", OwnerID: "42"}, ModeDocker); err != nil {
		t.Fatal(err)
	}
	if readFile(paths.BotEnv) != readFile(paths.Env) {
		t.Fatal("the checkout's .env is not the one compose reads for the container")
	}
}
