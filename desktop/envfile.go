package main

import (
	"crypto/rand"
	"encoding/hex"
	"net/url"
	"os"
	"runtime"
	"strings"
)

// envVar is one line of an env file. The launcher keeps the order fixed so a rewritten file reads
// the same way every time and a diff shows only what changed.
type envVar struct {
	Key   string
	Value string
}

// Setup holds the answers the setup page collects. Everything is optional except that the stack is
// only useful with either a provider key or a CLI login on the host.
//
// An answer that arrives empty means "leave what is on disk alone". Setup is run again to change one
// value, and a form that carries nothing for the others — a browser that did not fill a password
// field back in, or a page that is not the launcher's own — must not wipe them. Clear holds the
// fields the operator ticked to empty on purpose, which is the only way a value is removed.
type Setup struct {
	DeepseekKey   string
	OpenrouterKey string
	OpencodeKey   string
	ProviderKeys  map[string]string
	BotToken      string
	OwnerID       string
	APIID         string
	APIHash       string
	USDPerDay     string

	// The local or OpenAI-compatible endpoint. The address and its key go to the key proxy as an
	// upstream of their own, like every other credential, and the app is told only the model: its
	// provider entry points at the proxy (daedalus/config.py), so the agent never holds the key.
	LocalURL     string
	LocalKey     string
	LocalModel   string
	FreeProvider string
	FreeModel    string
	ModelKind    string

	// Voice is off, local or cloud; the app reads it at start (daedalus/config.py), and local is
	// the speech extra the launcher installs after the start.
	Voice string
	// Browser asks for the headless browser: the browser extra natively, the browser image's
	// compose profile in a container. Nil leaves what is there alone.
	Browser *bool
	// AppPort fixes the app's port when set; empty hands it back to the automatic choice.
	// FixPort says the form had an answer for it at all.
	AppPort string
	FixPort bool

	Clear map[string]bool
}

// Telegram reports whether the Telegram side of the stack should run. Without a token the bot is
// unreachable from Telegram and the local Bot API server has nothing to serve, so it stays down and
// the operator uses the browser app.
func (s Setup) Telegram() bool { return strings.TrimSpace(s.BotToken) != "" }

// mergeEnv rewrites the values of the given keys in an existing env file and appends the ones it
// does not find. Comments, blank lines and keys the operator added by hand are left alone: the file
// is theirs to edit after the first run.
func mergeEnv(existing string, updates []envVar) string {
	lines := []string{}
	if existing != "" {
		lines = strings.Split(strings.TrimSuffix(existing, "\n"), "\n")
	}
	for _, v := range updates {
		replaced := false
		for i, line := range lines {
			trimmed := strings.TrimLeft(line, " \t")
			if strings.HasPrefix(trimmed, v.Key+"=") || strings.HasPrefix(trimmed, "export "+v.Key+"=") {
				lines[i] = v.Key + "=" + v.Value
				replaced = true
				break
			}
		}
		if !replaced {
			lines = append(lines, v.Key+"="+v.Value)
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

// readEnv parses an env file into a map. Comments and malformed lines are skipped; a quoted value
// is unquoted, because a value written by hand often is.
func readEnv(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		value = strings.TrimSpace(value)
		if len(value) >= 2 && (value[0] == '"' || value[0] == '\'') && value[len(value)-1] == value[0] {
			value = value[1 : len(value)-1]
		}
		out[strings.TrimSpace(key)] = value
	}
	return out
}

// clean strips what a pasted value drags along. A newline inside a value would silently turn the
// rest of the key into a second variable, so it is removed rather than escaped.
func clean(value string) string {
	value = strings.ReplaceAll(value, "\r", "")
	value = strings.ReplaceAll(value, "\n", "")
	return strings.TrimSpace(value)
}

// answered builds the reader that decides a key's new value: what the form carried, or — when it
// carried nothing and the operator did not ask for the field to be emptied — whatever a previous
// run already wrote. The key is always written, so compose never has to interpolate a name that is
// not in the file.
func answered(s Setup, current map[string]string) func(key, field, value string) string {
	return func(key, field, value string) string {
		if value = clean(value); value != "" || s.Clear[field] {
			return value
		}
		return current[key]
	}
}

// dailyCap is the spending cap to write: what the form carried, else what is already in force, else
// the figure a fresh install starts with.
func dailyCap(given, existing string) string {
	if given = clean(given); given != "" {
		return given
	}
	if existing != "" {
		return existing
	}
	return "20"
}

// envUpdates is everything the launcher sets in <data>/.env. The three DAEDALUS_* paths are
// absolute because the rebuilder container resolves them on the host, not inside a project
// directory; SERVICES_PUBLIC_HOST is the loopback address because a desktop install serves the
// operator's own machine. `current` is what a previous run left in the file.
func envUpdates(p Paths, s Setup, current map[string]string, searxngSecret, home string) []envVar {
	answer := answered(s, current)
	localModel := answer("DAEDALUS_LOCAL_MODEL", "local_model", s.LocalModel)
	freeProvider := answer("DAEDALUS_FREE_PROVIDER", "free_provider", s.FreeProvider)
	freeModel := answer("DAEDALUS_FREE_MODEL", "free_model", s.FreeModel)
	if s.ModelKind == "free" {
		localModel = ""
	}
	if s.ModelKind != "" && s.ModelKind != "free" {
		freeProvider, freeModel = "", ""
	}
	return []envVar{
		{"TELEGRAM_BOT_TOKEN", answer("TELEGRAM_BOT_TOKEN", "bot_token", s.BotToken)},
		{"OWNER_USER_ID", answer("OWNER_USER_ID", "owner_id", s.OwnerID)},
		{"TELEGRAM_API_ID", answer("TELEGRAM_API_ID", "api_id", s.APIID)},
		{"TELEGRAM_API_HASH", answer("TELEGRAM_API_HASH", "api_hash", s.APIHash)},
		{"USD_PER_DAY", dailyCap(s.USDPerDay, current["USD_PER_DAY"])},
		{"DAEDALUS_LOCAL_MODEL", localModel},
		{"DAEDALUS_FREE_PROVIDER", freeProvider},
		{"DAEDALUS_FREE_MODEL", freeModel},
		{"DAEDALUS_VOICE", firstSet(clean(s.Voice), firstSet(current["DAEDALUS_VOICE"], "off"))},
		{"SEARXNG_SECRET", searxngSecret},
		// The public address is not on the setup page: an operator who set one by hand keeps it,
		// and passkeys are enrolled against that host, so blanking it would invalidate them.
		{"MINIAPP_PUBLIC_URL", current["MINIAPP_PUBLIC_URL"]},
		// The ports are written once and then kept: rewriting them on every save would take back a
		// change made by hand in the file. A first setup chooses free ones (WriteSetup), and a start
		// moves one only when another program has taken it since (ports.go).
		{"API_PORT", firstSet(current["API_PORT"], "8765")},
		{"SERVICES_PORT_RANGE", firstSet(current["SERVICES_PORT_RANGE"], "8100-8119")},
		{"SERVICES_PUBLIC_HOST", firstSet(current["SERVICES_PUBLIC_HOST"], "127.0.0.1")},
		{"KEYPROXY_PORT", firstSet(current["KEYPROXY_PORT"], "3201")},
		{"DAEDALUS_COMPOSE_PROJECT_DIR", p.Bot},
		{"DAEDALUS_COMPOSE_FILE", p.Compose},
		{"DAEDALUS_CORE_PROJECT_DIR", p.Core},
		{"DAEDALUS_SECRETS_FILE", p.KeyproxyEnv},
		{"DAEDALUS_SSH_DIR", p.SSH},
		{"DAEDALUS_HARNESS_HOME", home},
	}
}

// withValues is updates with each of values in place of the entry of the same key, and appended
// when there is none.
func withValues(updates, values []envVar) []envVar {
	out := append([]envVar(nil), updates...)
	for _, value := range values {
		found := false
		for i := range out {
			if out[i].Key == value.Key {
				out[i].Value, found = value.Value, true
			}
		}
		if !found {
			out = append(out, value)
		}
	}
	return out
}

// firstSet is the first value that was written down: what a previous run left, else the default a
// fresh installation starts with.
func firstSet(existing, fallback string) string {
	if strings.TrimSpace(existing) != "" {
		return strings.TrimSpace(existing)
	}
	return fallback
}

// providerKeyVars are the provider keys beyond the first three, by the provider id the app and the
// setup form know them as, with the variable the key proxy reads. API and subscription plans of one
// vendor are separate entries: they are billed and routed differently upstream.
var providerKeyVars = [][2]string{
	{"opencode_zen", "KEYPROXY_KEY_OPENCODE_ZEN"},
	{"openai", "OPENAI_API_KEY"},
	{"anthropic", "ANTHROPIC_API_KEY"},
	{"zai", "ZAI_API_KEY"},
	{"zai_coding", "ZAI_CODING_API_KEY"},
	{"minimax", "MINIMAX_API_KEY"},
	{"minimax_plan", "MINIMAX_PLAN_API_KEY"},
	{"moonshot", "MOONSHOT_API_KEY"},
	{"kimi_coding", "KIMI_CODING_API_KEY"},
}

// keyproxyUpdates is everything the launcher sets in daedalus-secrets/keyproxy.env. Provider keys
// live only here: the file is outside every mount the agent container gets, so the agent process
// never holds a key even when it edits its own code.
func keyproxyUpdates(s Setup, current map[string]string) []envVar {
	answer := answered(s, current)
	updates := []envVar{
		{"DEEPSEEK_API_KEY", answer("DEEPSEEK_API_KEY", "deepseek", s.DeepseekKey)},
		{"OPENROUTER_API_KEY", answer("OPENROUTER_API_KEY", "openrouter", s.OpenrouterKey)},
		{"OPENCODE_API_KEY", answer("OPENCODE_API_KEY", "opencode", s.OpencodeKey)},
	}
	for _, provider := range providerKeyVars {
		updates = append(updates, envVar{provider[1], answer(provider[1], provider[0], s.ProviderKeys[provider[0]])})
	}
	updates = append(updates,
		envVar{"KEYPROXY_UPSTREAM_LOCAL", answer("KEYPROXY_UPSTREAM_LOCAL", "local_url", s.LocalURL)},
		envVar{"KEYPROXY_KEY_LOCAL", answer("KEYPROXY_KEY_LOCAL", "local_key", s.LocalKey)},
		envVar{"KEYPROXY_UPSTREAM_KILO", "https://api.kilo.ai/api/gateway"},
		envVar{"KEYPROXY_UPSTREAM_OPENCODE_ZEN", "https://opencode.ai/zen/v1"},
	)
	return append(updates, envVar{"KEYPROXY_USD_PER_DAY", dailyCap(s.USDPerDay, current["KEYPROXY_USD_PER_DAY"])})
}

// containerReachable is the endpoint's address as the key proxy's container reaches it. Typed on
// this machine, 127.0.0.1 and localhost mean the operator's own computer, and inside a container
// they mean the container: the compose file gives the proxy host.docker.internal for this.
func containerReachable(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	switch u.Hostname() {
	case "127.0.0.1", "localhost", "::1":
		host := "host.docker.internal"
		if port := u.Port(); port != "" {
			host += ":" + port
		}
		u.Host = host
		return u.String()
	}
	return raw
}

// withProfile is a COMPOSE_PROFILES list with name added or taken out, the others kept in order.
func withProfile(list, name string, on bool) string {
	var out []string
	for _, item := range strings.Split(list, ",") {
		item = strings.TrimSpace(item)
		if item != "" && item != name {
			out = append(out, item)
		}
	}
	if on {
		out = append(out, name)
	}
	return strings.Join(out, ",")
}

// WriteSetup writes both env files. The existing values are read first, so re-running setup keeps
// a key the form did not carry, keeps a public address set by hand, and keeps the SearXNG secret
// stable across runs.
func WriteSetup(p Paths, s Setup, mode Mode) error {
	return writeSetup(p, s, mode, func(string, ...any) {})
}

// writeSetup is WriteSetup saying in log which ports a first setup could not have at their defaults.
func writeSetup(p Paths, s Setup, mode Mode, log func(string, ...any)) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = p.Data // the CLI logins are mounted from here; a missing home is not fatal
	}
	current := readEnv(readFile(p.Env))
	secret := current["SEARXNG_SECRET"]
	if secret == "" {
		secret, err = randomSecret()
		if err != nil {
			return err
		}
	}
	fresh := strings.TrimSpace(current["API_PORT"]) == ""
	updates := envUpdates(p, s, current, secret, home)
	if s.Browser != nil && mode == ModeDocker {
		updates = withValues(updates, []envVar{{"COMPOSE_PROFILES", withProfile(current["COMPOSE_PROFILES"], "browser", *s.Browser)}})
	}
	if s.FixPort {
		// A fixed port is written as the app's port and named in DAEDALUS_FIXED_PORTS, which is
		// what keeps a start from moving it (ports.go). An empty answer hands it back.
		fixed := ""
		if port := clean(s.AppPort); port != "" {
			current["API_PORT"], fixed = port, "API_PORT"
			updates = withValues(updates, []envVar{{"API_PORT", port}})
		}
		current["DAEDALUS_FIXED_PORTS"] = fixed
		updates = withValues(updates, []envVar{{"DAEDALUS_FIXED_PORTS", fixed}})
	}
	if fresh {
		// A first setup: nothing of this installation runs yet, so whatever holds a default port is
		// another program, and the installation starts out on ports that are free — all but one the
		// operator fixed, which the plan keeps where they put it.
		roles, probe := portRoles(mode, supervisorOverTCP(p, runtime.GOOS)), machineProbe(nil)
		warmRoles(probe, current, roles)
		ports, moves := planPorts(current, roles, probe, true)
		updates = withValues(updates, ports)
		for _, move := range moves {
			log("%s", move)
		}
	}
	if err := os.WriteFile(p.Env, []byte(mergeEnv(readFile(p.Env), updates)), 0o600); err != nil {
		return err
	}
	keys := readEnv(readFile(p.KeyproxyEnv))
	if mode == ModeDocker && clean(s.LocalURL) != "" {
		s.LocalURL = containerReachable(clean(s.LocalURL))
	}
	if err := os.WriteFile(p.KeyproxyEnv, []byte(mergeEnv(readFile(p.KeyproxyEnv), keyproxyUpdates(s, keys))), 0o600); err != nil {
		return err
	}
	return SyncBotEnv(p, mode)
}

// SyncBotEnv copies <data>/.env to the checkout root, in Docker mode. The compose file reads ../.env
// as the agent container's environment and the rebuilder reads .env from the project directory, both
// of which are the checkout, while the launcher keeps the file it owns next to the data folder.
//
// Native mode has no compose to interpolate and is not given the copy. The file holds the Telegram
// bot token and the API hash — full control of one of the two front doors — and the checkout is a
// directory the agent works in freely, so copying it there put the token inside the one place the
// rules open rather than the one they seal.
func SyncBotEnv(p Paths, mode Mode) error {
	if mode == ModeNative || !exists(p.Bot) {
		return nil
	}
	body, err := os.ReadFile(p.Env)
	if err != nil {
		return err
	}
	return os.WriteFile(p.BotEnv, body, 0o600)
}

// CurrentSetup reads back what a previous run wrote, so the setup page opens with the values that
// are in force rather than with empty fields. Keys come from the secrets file.
func CurrentSetup(p Paths) Setup {
	env := readEnv(readFile(p.Env))
	keys := readEnv(readFile(p.KeyproxyEnv))
	providerKeys := map[string]string{}
	for _, provider := range providerKeyVars {
		if value := keys[provider[1]]; value != "" {
			providerKeys[provider[0]] = value
		}
	}
	if len(providerKeys) == 0 {
		providerKeys = nil
	}
	return Setup{
		DeepseekKey:   keys["DEEPSEEK_API_KEY"],
		OpenrouterKey: keys["OPENROUTER_API_KEY"],
		OpencodeKey:   keys["OPENCODE_API_KEY"],
		ProviderKeys:  providerKeys,
		BotToken:      env["TELEGRAM_BOT_TOKEN"],
		OwnerID:       env["OWNER_USER_ID"],
		APIID:         env["TELEGRAM_API_ID"],
		APIHash:       env["TELEGRAM_API_HASH"],
		USDPerDay:     env["USD_PER_DAY"],
		LocalURL:      keys["KEYPROXY_UPSTREAM_LOCAL"],
		LocalKey:      keys["KEYPROXY_KEY_LOCAL"],
		LocalModel:    env["DAEDALUS_LOCAL_MODEL"],
		FreeProvider:  env["DAEDALUS_FREE_PROVIDER"],
		FreeModel:     env["DAEDALUS_FREE_MODEL"],
		Voice:         env["DAEDALUS_VOICE"],
		AppPort:       env["API_PORT"],
		FixPort:       strings.Contains(","+env["DAEDALUS_FIXED_PORTS"]+",", ",API_PORT,"),
	}
}

// APIPort is the port the bot's API and app listen on, as the env file has it.
func APIPort(p Paths) string {
	if port := readEnv(readFile(p.Env))["API_PORT"]; port != "" {
		return port
	}
	return "8765"
}

func readFile(path string) string {
	body, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(body)
}

func randomSecret() (string, error) {
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}
