package main

// The first-run wizard's side of the launcher: what the page is told when it opens, the three
// questions it asks while the operator answers (which CLIs are signed in, whether Docker is here,
// whether an endpoint answers), the form it posts, and what is done with the answers after the
// configuration is written — start at login, the optional runtime pieces, the hand-off to the app.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// setupHelpers is what the wizard's questions are answered with. The real machine by default; the
// tests give the server a machine of their own.
type setupHelpers struct {
	autostart Autostart
	logins    func(ctx context.Context) []CLILogin
	docker    func(ctx context.Context) bool
	client    *http.Client
}

// newSetupHelpers makes a server's helpers. A variable so that the tests answer from a machine of
// their own: the real one runs the operator's CLIs and writes into their login items.
var newSetupHelpers = systemSetupHelpers

func systemSetupHelpers() setupHelpers {
	home, _ := os.UserHomeDir()
	return setupHelpers{
		autostart: NewAutostart(runtime.GOOS, home, AutostartCommand(), nil),
		logins:    func(ctx context.Context) []CLILogin { return DetectCLILogins(ctx, home, systemCLIRunner()) },
		docker:    func(ctx context.Context) bool { return DockerVersion(ctx) != "" },
		client:    &http.Client{},
	}
}

// afterSetup is what the posted answers asked for beyond the files: the pieces of the runtime to
// install once the agent is up, and whether the page itself opens the app when the start is done.
type afterSetup struct {
	mu      sync.Mutex
	extras  []string
	handoff bool
}

func (a *afterSetup) set(extras []string, handoff bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.extras, a.handoff = extras, handoff
}

// take hands the pending extras over once: a second start must not install them again.
func (a *afterSetup) take() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := a.extras
	a.extras = nil
	return out
}

func (a *afterSetup) handsOff() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.handoff
}

// mask is how a stored secret is shown back: enough of its end to recognise which one it is, never
// enough to use. Short values show nothing at all.
func mask(secret string) string {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return ""
	}
	if len(secret) <= 8 {
		return "••••"
	}
	return "••••" + secret[len(secret)-4:]
}

// setupBoot is the state the wizard opens with. Every field it shows is here; a secret is here only
// as its mask, and the field it belongs to starts empty — an empty answer keeps what is stored.
type setupBoot struct {
	Lang        Lang              `json:"lang"`
	Providers   []wizardProvider  `json:"providers"`
	Reconfigure bool              `json:"reconfigure"`
	Native      bool              `json:"native"`
	Mode        string            `json:"mode"`
	Docker      bool              `json:"docker"`
	CLIs        map[string]bool   `json:"clis"`
	Kept        map[string]string `json:"kept"`
	Kind        string            `json:"kind"`
	Provider    string            `json:"provider"`
	CLI         string            `json:"cli"`
	Local       struct {
		URL   string `json:"url"`
		Model string `json:"model"`
	} `json:"local"`
	Free struct {
		Provider string `json:"provider"`
		Model    string `json:"model"`
	} `json:"free"`
	Limit string `json:"limit"`
	TG    struct {
		Owner string `json:"owner"`
		APIID string `json:"apiid"`
	} `json:"tg"`
	Adv struct {
		Data      string `json:"data"`
		PortsAuto bool   `json:"portsAuto"`
		Port      string `json:"port"`
		Login     bool   `json:"login"`
		Browser   bool   `json:"browser"`
	} `json:"adv"`
	Speech struct {
		Choice string        `json:"choice"`
		Models []speechModel `json:"models"`
	} `json:"speech"`
	Search struct {
		// Searxng is whether the step opens with SearXNG on: on for a first setup, which is the
		// recommendation, and afterwards whatever the installation has.
		Searxng   bool             `json:"searxng"`
		Providers []searchProvider `json:"providers"`
	} `json:"search"`
}

// searchProvider is one search API the web search step offers a key field for, with where to sign
// up and where the key is then found. Recommended marks the one the step puts first and explains.
type searchProvider struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	SignUp      string `json:"signup"`
	KeyPage     string `json:"keys"`
	Recommended bool   `json:"recommended"`
}

// searchProviders is the search step's catalogue, in searchKeyVars' order. The addresses were
// checked when they were written down; a vendor that moves its console breaks only the link.
var searchProviders = []searchProvider{
	{"keenable", "Keenable", "https://app.keenable.ai/signup", "https://app.keenable.ai/console", true},
	{"serper", "Serper", "https://serper.dev/signup", "https://serper.dev/api-keys", false},
	{"tavily", "Tavily", "https://app.tavily.com", "https://app.tavily.com/home", false},
	{"exa", "Exa", "https://dashboard.exa.ai", "https://dashboard.exa.ai/api-keys", false},
	{"perplexity", "Perplexity", "https://console.perplexity.ai", "https://console.perplexity.ai", false},
}

// wizardProvider is one key the wizard's model step offers. The id is the form field the key
// travels in and the provider id the app and the key proxy know it by; Plan says the key belongs to
// a subscription or coding plan rather than to a balance billed per token, which is the group the
// page puts it in.
type wizardProvider struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Prefix string `json:"prefix"`
	Plan   bool   `json:"plan"`
}

// wizardProviders is the model step's catalogue, in its order. The page draws its two groups from
// Plan and has no list of its own. The names are the app's own (daedalus/config.py) so a provider
// reads the same in the wizard and in Settings, and each vendor sold both ways is named for which
// of the two the key is: the plans have their own endpoints and their keys work nowhere else
// (deploy/keyproxy/proxy.py). OpenCode's key opens both Go and Zen; this entry is the Go route, the
// prepaid plan, and Zen is offered on the Free tab and in Settings. No prefix is checked where the
// vendor's key format is not fixed.
var wizardProviders = []wizardProvider{
	{"deepseek", "DeepSeek API", "sk-", false},
	{"openrouter", "OpenRouter", "sk-or-", false},
	{"openai", "OpenAI API", "sk-", false},
	{"anthropic", "Anthropic API", "sk-ant-", false},
	{"zai", "Z.AI API", "", false},
	{"minimax", "MiniMax API", "", false},
	{"moonshot", "Moonshot AI API", "sk-", false},
	{"opencode", "OpenCode Go", "", true},
	{"zai_coding", "Z.AI Coding Plan", "", true},
	{"minimax_plan", "MiniMax Token Plan", "", true},
	{"kimi_coding", "Kimi Code", "", true},
}

// setupProviders is every key the launcher stores, by the form field it travels in: the wizard's
// own and the ones only the Free tab asks for. A stored one is shown back masked.
var setupProviders = func() []string {
	ids := []string{"deepseek", "openrouter", "opencode"}
	for _, provider := range providerKeyVars {
		ids = append(ids, provider[0])
	}
	return ids
}()

// offeredProvider reports whether the model step has a tile for this id.
func offeredProvider(id string) bool {
	for _, provider := range wizardProviders {
		if provider.ID == id {
			return true
		}
	}
	return false
}

func providerKey(s Setup, id string) string {
	switch id {
	case "deepseek":
		return s.DeepseekKey
	case "openrouter":
		return s.OpenrouterKey
	case "opencode":
		return s.OpencodeKey
	}
	return s.ProviderKeys[id]
}

// hostReachable is the endpoint's address as the operator typed it, undoing the rewrite a Docker
// installation stores (containerReachable): the page tests from this machine, not from the proxy's
// container.
func hostReachable(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() != "host.docker.internal" {
		return raw
	}
	host := "127.0.0.1"
	if port := u.Port(); port != "" {
		host += ":" + port
	}
	u.Host = host
	return u.String()
}

func (s *Server) setupBoot(ctx context.Context, lang Lang, status Status, suggested string) setupBoot {
	current := CurrentSetup(s.app.paths)
	env := readEnv(readFile(s.app.paths.Env))
	var b setupBoot
	b.Lang = lang
	b.Reconfigure = s.app.paths.Configured()
	b.Native = s.app.Native()
	b.Mode = suggested
	b.Docker = status.Docker != ""
	b.CLIs = map[string]bool{}
	for _, login := range s.helpers.logins(ctx) {
		b.CLIs[login.ID] = login.SignedIn
	}
	b.Kept = map[string]string{}
	b.Kind, b.Provider = "cloud", "deepseek"
	providerChosen := false
	b.Providers = wizardProviders
	for _, id := range setupProviders {
		if key := providerKey(current, id); key != "" {
			b.Kept["keys."+id] = mask(key)
			// Only a provider with a tile is chosen: a Zen key alone, stored by the Free tab, used
			// to open the step on a provider the page had no tile for.
			if !providerChosen && offeredProvider(id) {
				b.Provider, providerChosen = id, true
			}
		}
	}
	b.CLI = "codex"
	for _, id := range []string{"codex", "claude", "grok"} {
		if b.CLIs[id] {
			b.CLI = id
			break
		}
	}
	if !providerChosen && b.Reconfigure && b.CLIs[b.CLI] {
		b.Kind = "cli"
	}
	b.Local.URL = "http://127.0.0.1:11434/v1"
	if current.LocalURL != "" {
		b.Local.URL = hostReachable(current.LocalURL)
		b.Local.Model = current.LocalModel
		if current.LocalModel != "" {
			b.Kind = "local"
		}
	}
	if current.LocalKey != "" {
		b.Kept["local.key"] = mask(current.LocalKey)
	}
	b.Free.Provider, b.Free.Model = current.FreeProvider, current.FreeModel
	if b.Free.Provider == "" {
		b.Free.Provider = "kilo"
	}
	if current.FreeModel != "" {
		b.Kind = "free"
	}
	b.Limit = firstSet(current.USDPerDay, "20")
	if current.BotToken != "" {
		b.Kept["tg.token"] = mask(current.BotToken)
	}
	if current.APIHash != "" {
		b.Kept["tg.apihash"] = mask(current.APIHash)
	}
	b.TG.Owner, b.TG.APIID = current.OwnerID, current.APIID
	b.Adv.Data = s.app.paths.Data
	b.Adv.PortsAuto = !current.FixPort
	b.Adv.Port = firstSet(current.AppPort, portDefaults["API_PORT"])
	if on, err := s.helpers.autostart.Enabled(); err == nil {
		b.Adv.Login = on
	}
	if s.app.Native() {
		b.Adv.Browser = exists(s.app.paths.RuntimeBrowsers)
	} else {
		b.Adv.Browser = strings.Contains(","+env["COMPOSE_PROFILES"]+",", ",browser,")
	}
	b.Speech.Choice = speechChoice(current)
	b.Speech.Models = speechModels
	b.Search.Providers = searchProviders
	if current.Search != nil {
		for id, key := range current.Search.Keys {
			b.Kept["search.keys."+id] = mask(key)
		}
	}
	switch {
	case !b.Reconfigure:
		b.Search.Searxng = true
	case s.app.Native():
		b.Search.Searxng = searxngInstalled(s.app.paths)
	default:
		b.Search.Searxng = strings.Contains(","+env["COMPOSE_PROFILES"]+",", ",search,")
	}
	return b
}

// bootJSON renders the boot state for the page's data block. The template escapes it for a script
// element; a value that cannot be marshalled is a page that opens on its defaults.
func bootJSON(b setupBoot) template.JS {
	body, err := json.Marshal(b)
	if err != nil {
		return "{}"
	}
	return template.JS(body)
}

// setupError is a refused answer, named by the wizard's own field so the page can put the sentence
// under it.
type setupError struct {
	Field string `json:"field"`
	Error string `json:"error"`
}

func (e setupError) Err() error { return errors.New(e.Field + ": " + e.Error) }

// parseSetupForm reads the wizard's post. The page validates as the operator types; this is the
// same rules again for whatever reaches the launcher by another way, because the values end up in
// env files that are read by programs that trust them.
func parseSetupForm(form url.Values) (Setup, map[string]string, *setupError) {
	get := func(name string) string { return clean(form.Get(name)) }
	s := Setup{
		DeepseekKey:   get("deepseek"),
		OpenrouterKey: get("openrouter"),
		OpencodeKey:   get("opencode"),
		ProviderKeys:  map[string]string{},
		BotToken:      get("bot_token"),
		OwnerID:       get("owner_id"),
		APIID:         get("api_id"),
		APIHash:       get("api_hash"),
		USDPerDay:     strings.ReplaceAll(get("usd_per_day"), ",", "."),
		ModelKind:     get("kind"),
		Clear:         clearedFields(form["clear"]),
	}
	for _, provider := range providerKeyVars {
		if key := get(provider[0]); key != "" {
			s.ProviderKeys[provider[0]] = key
		}
	}
	if s.USDPerDay != "" {
		if v, err := strconv.ParseFloat(s.USDPerDay, 64); err != nil || v < 1 || v > 1000 {
			return s, nil, &setupError{"limit", "from 1 to 1000"}
		}
	}
	if s.OwnerID != "" && strings.Trim(s.OwnerID, "0123456789") != "" {
		return s, nil, &setupError{"tg.owner", "digits only"}
	}
	if s.APIID != "" && strings.Trim(s.APIID, "0123456789") != "" {
		return s, nil, &setupError{"tg.apiid", "digits only"}
	}
	if form.Get("kind") == "local" {
		base, err := normaliseEndpoint(get("local_url"))
		if err != nil {
			return s, nil, &setupError{"local.url", err.Error()}
		}
		s.LocalURL, s.LocalKey, s.LocalModel = base, get("local_key"), get("local_model")
		if s.LocalModel == "" {
			return s, nil, &setupError{"local.model", "pick a model"}
		}
	}
	if form.Get("kind") == "free" {
		s.FreeProvider, s.FreeModel = get("free_provider"), get("free_model")
		if s.FreeProvider != "kilo" && s.FreeProvider != "openrouter" && s.FreeProvider != "opencode_zen" {
			return s, nil, &setupError{"free.provider", "choose a free provider"}
		}
		if s.FreeModel == "" || len(s.FreeModel) > 200 || strings.ContainsAny(s.FreeModel, " \t\r\n") {
			return s, nil, &setupError{"free.model", "choose a listed free model"}
		}
		if s.FreeProvider == "openrouter" {
			key := get("free_key")
			if key != "" {
				s.OpenrouterKey = key
			}
		}
		if s.FreeProvider == "opencode_zen" {
			if key := get("free_key"); key != "" {
				s.ProviderKeys["opencode_zen"] = key
			}
		}
	}
	switch voice := get("voice"); voice {
	case "", "off", "cloud":
		s.Voice = voice
	case "local":
		// Local is always one model now: the speech step chooses it, and the app downloads that.
		s.Voice, s.SttModel = voice, get("stt_model")
		if !knownSpeechModel(s.SttModel) {
			return s, nil, &setupError{"speech", "choose a listed speech model"}
		}
	default:
		return s, nil, &setupError{"speech", "off, local or cloud"}
	}
	switch get("browser") {
	case "on":
		on := true
		s.Browser = &on
	case "off":
		off := false
		s.Browser = &off
	}
	switch get("search") {
	case "":
		// The step was skipped: what the installation searches with stays as it was.
	case "on":
		s.Search = &SearchAnswer{Searxng: get("searxng") == "on", Keys: map[string]string{}}
		for _, provider := range searchKeyVars {
			key := get("search_" + provider[0])
			if key == "" {
				continue
			}
			if len(key) > 512 || strings.ContainsAny(key, " \t\r\n") {
				return s, nil, &setupError{"search." + provider[0], "paste the key as it was given"}
			}
			s.Search.Keys[provider[0]] = key
		}
	default:
		return s, nil, &setupError{"search", "on, or nothing"}
	}
	if _, ok := form["port"]; ok {
		s.FixPort = true
		if port := get("port"); port != "" {
			n, err := strconv.Atoi(port)
			if err != nil || n < 1024 || n > 65535 {
				return s, nil, &setupError{"adv.port", "from 1024 to 65535"}
			}
			s.AppPort = strconv.Itoa(n)
		}
	}
	extras := map[string]string{"login": get("login")}
	return s, extras, nil
}

// wantedExtras is the runtime pieces the answers ask for that this installation installs itself:
// only a native one, where they are parts of the runtime folder. A container has the browser in its
// image (the compose profile the form wrote) and the speech engine in every image.
func wantedExtras(s Setup, native bool, paths Paths) []string {
	if !native {
		return nil
	}
	var out []string
	if s.Browser != nil && *s.Browser && !exists(paths.RuntimeBrowsers) {
		out = append(out, "browser")
	}
	if s.Voice == "local" {
		out = append(out, "speech")
	}
	if s.Search != nil && s.Search.Searxng && !searxngInstalled(paths) {
		out = append(out, "searxng")
	}
	return out
}

// searxngInstalled reports whether the agent has put SearXNG into the runtime folder: the marker
// its install writes last (launcher/searxng.py), which is also what the supervisor starts it by.
func searxngInstalled(paths Paths) bool {
	return exists(filepath.Join(paths.Runtime, "searxng", "installed.json"))
}

// restartsFor reports whether an installed extra needs the agent started again to be found. The
// browser and the speech engine are reached through the environment the supervisor was given;
// SearXNG is picked up by the running supervisor by itself.
func restartsFor(name string) bool { return name != "searxng" }

// applyLogin turns start at login on or off as the form said. An empty answer leaves it alone, and
// a machine where it cannot be done says so in the log rather than failing the whole setup: the
// configuration is written either way.
func (s *Server) applyLogin(answer string) {
	var err error
	switch answer {
	case "on":
		err = s.helpers.autostart.Enable()
	case "off":
		err = s.helpers.autostart.Disable()
	default:
		return
	}
	if err != nil {
		s.app.log("start at login could not be changed: %v", err)
		return
	}
	s.app.log("start at login is %s (%s)", answer, s.helpers.autostart.Where())
}

// writeJSON answers one of the page's own calls.
func writeJSON(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(body)
}

// handleSetupDetect answers the wizard's "scan again": which CLIs are signed in and whether Docker
// answers. A post with the token, because it runs programs on the operator's machine.
func (s *Server) handleSetupDetect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post to detect", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	clis := map[string]bool{}
	for _, login := range s.helpers.logins(ctx) {
		clis[login.ID] = login.SignedIn
	}
	writeJSON(w, http.StatusOK, map[string]any{"clis": clis, "docker": s.helpers.docker(ctx)})
}

// handleSetupEndpoint is the wizard's "test connection": the launcher asks the address the operator
// typed for its models and says what came back. Only that address — the probe follows no redirect —
// and only from here: the page itself cannot reach another origin, and should not. A loopback or
// LAN address is the ordinary case, the operator's own server. The key is never in the answer; an
// empty one with the stored address reuses the stored key, which is what lets a re-run test the
// endpoint without pasting the key again.
func (s *Server) handleSetupEndpoint(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post an endpoint", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	var body struct {
		URL string `json:"url"`
		Key string `json:"key"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, EndpointProbe{Error: "the request could not be read"})
		return
	}
	base, err := normaliseEndpoint(body.URL)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, EndpointProbe{Error: err.Error()})
		return
	}
	key := clean(body.Key)
	if key == "" {
		current := CurrentSetup(s.app.paths)
		if stored, err := normaliseEndpoint(hostReachable(current.LocalURL)); err == nil && stored == base {
			key = current.LocalKey
		}
	}
	writeJSON(w, http.StatusOK, ProbeEndpoint(r.Context(), s.helpers.client, base, key))
}

// handleAutostart is the status page's switch for start at login, the way back from the wizard's.
func (s *Server) handleAutostart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "post a choice", http.StatusMethodNotAllowed)
		return
	}
	if !s.hasToken(r.Header.Get(csrfHeader)) {
		refuse(w)
		return
	}
	var body struct {
		On bool `json:"on"`
	}
	_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10)).Decode(&body)
	answer := "off"
	if body.On {
		answer = "on"
	}
	var err error
	if body.On {
		err = s.helpers.autostart.Enable()
	} else {
		err = s.helpers.autostart.Disable()
	}
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	s.app.log("start at login is %s (%s)", answer, s.helpers.autostart.Where())
	on, _ := s.helpers.autostart.Enabled()
	writeJSON(w, http.StatusOK, map[string]any{"on": on})
}

// AfterStart installs the runtime pieces the setup asked for, once the agent is up, and starts it
// again when one was installed: the browser and the speech model are found through the environment
// the supervisor was started with (app.go, Restart).
func (s *Server) AfterStart(ctx context.Context) {
	extras := s.after.take()
	if len(extras) == 0 {
		return
	}
	installed := false
	for _, name := range extras {
		if err := s.app.InstallExtra(ctx, name); err == nil && restartsFor(name) {
			installed = true
		}
	}
	if installed {
		_ = s.app.Restart(ctx)
	}
}

// HandsOff reports whether the wizard opens the app itself when the start is done: it shows the
// start, and the window moving to the app under it would cut its last step short.
func (s *Server) HandsOff() bool { return s.after.handsOff() }

// reconfigure applies answers posted to a launcher that is not waiting for a first setup: the
// installation is up, or was, and has to come back on the new configuration.
func (s *Server) reconfigure(ctx context.Context) {
	status := s.app.Status(ctx)
	var err error
	if s.app.Native() && status.Running > 0 {
		err = s.app.Restart(ctx)
	} else {
		err = s.app.Start(ctx)
	}
	if err != nil {
		fmt.Println("the new configuration did not start:", err)
		return
	}
	s.AfterStart(ctx)
}
