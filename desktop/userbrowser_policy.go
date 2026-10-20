package main

// What an administrator's Chrome policies say about the extension and its connector. Detection
// reports and never refuses: a policy can change between now and the moment the operator presses
// the button, Chrome is the one that enforces it, and chrome://policy is where the operator can
// see the whole of it. What this adds is the sentence in the app before the operator spends ten
// minutes wondering why the extension will not load.

import (
	"encoding/json"
	"encoding/xml"
	"errors"
	"io"
	"os/user"
	"path"
	"sort"
	"strconv"
	"strings"
)

// regRoot names a registry hive. Only the two Chrome reads are ever needed.
type regRoot int

const (
	regCurrentUser regRoot = iota
	regLocalMachine
)

func (r regRoot) String() string {
	if r == regLocalMachine {
		return "HKLM"
	}
	return "HKCU"
}

// chromeRegistry is the part of the Windows registry the connector and the policy probe touch, as
// an interface so both are tested on any machine against a fake. Writes go to HKEY_CURRENT_USER
// only: the connector is the operator's, and nothing here asks for an administrator.
type chromeRegistry interface {
	// String reads a string value; name "" is the key's default value.
	String(root regRoot, key, name string) (string, bool)
	// Values reads every value of a key, strings as string and numbers as uint64.
	Values(root regRoot, key string) map[string]any
	// SetDefault creates key under HKCU if needed and sets its default value.
	SetDefault(key, value string) error
	// DeleteKey removes key under HKCU, which must have no subkeys; a missing key is not an error.
	DeleteKey(key string) error
}

// PolicyFinding is one policy that stands in the way, and what it stops.
type PolicyFinding struct {
	Policy string `json:"policy"`
	// Effect is what it stops: "extension" (the extension cannot be installed or loaded),
	// "developer_mode" (an unpacked extension cannot be loaded), "debugger" (the extension may not
	// drive a tab), "connector" (Chrome will not start the bridge), "sites" (some sites are out of
	// the extension's reach), or "unknown" (policies are set and could not be read here).
	Effect string `json:"effect"`
	Source string `json:"source"`
	Detail string `json:"detail"`
}

// evaluateChromePolicies turns one source's policies into findings. values maps a policy name to
// what the source holds: a bool, a number, a string, a list of strings, or for ExtensionSettings a
// JSON object or its text.
func evaluateChromePolicies(source string, values map[string]any) []PolicyFinding {
	var out []PolicyFinding
	add := func(policy, effect, detail string) {
		out = append(out, PolicyFinding{Policy: policy, Effect: effect, Source: source, Detail: detail})
	}
	extensionAllowed := containsFold(policyList(values["ExtensionInstallAllowlist"]), chromeExtensionID)
	if block := policyList(values["ExtensionInstallBlocklist"]); !extensionAllowed && (containsFold(block, "*") || containsFold(block, chromeExtensionID)) {
		add("ExtensionInstallBlocklist", "extension", "extensions are blocked and the Daedalus extension is not on the allowlist")
	}
	if types := policyList(values["ExtensionAllowedTypes"]); values["ExtensionAllowedTypes"] != nil && !containsFold(types, "extension") {
		add("ExtensionAllowedTypes", "extension", "ordinary extensions are not among the allowed types")
	}
	if n, ok := policyInt(values["ExtensionDeveloperModeSettings"]); ok && n == 1 {
		add("ExtensionDeveloperModeSettings", "developer_mode", "developer mode is off by policy, so an unpacked extension cannot be loaded")
	}
	if n, ok := policyInt(values["DeveloperToolsAvailability"]); ok && n == 2 {
		add("DeveloperToolsAvailability", "debugger", "developer tools are disallowed, which also forbids an extension to debug a tab")
	}
	if on, ok := policyBool(values["NativeMessagingUserLevelHosts"]); ok && !on {
		add("NativeMessagingUserLevelHosts", "connector", "only hosts installed for every user may run, and the connector is registered for you alone")
	}
	connectorAllowed := containsFold(policyList(values["NativeMessagingAllowlist"]), chromeHostName)
	if block := policyList(values["NativeMessagingBlocklist"]); !connectorAllowed && (containsFold(block, "*") || containsFold(block, chromeHostName)) {
		add("NativeMessagingBlocklist", "connector", "native-messaging hosts are blocked and "+chromeHostName+" is not on the allowlist")
	}
	out = append(out, evaluateExtensionSettings(source, values["ExtensionSettings"])...)
	return out
}

// evaluateExtensionSettings reads the one policy that is a whole object: per-extension settings,
// with "*" as the default for every extension it does not name.
func evaluateExtensionSettings(source string, raw any) []PolicyFinding {
	settings, ok := raw.(map[string]any)
	if text, isText := raw.(string); isText {
		ok = json.Unmarshal([]byte(text), &settings) == nil
	}
	if !ok || settings == nil {
		return nil
	}
	var out []PolicyFinding
	mode := func(entry any) string {
		if m, ok := entry.(map[string]any); ok {
			s, _ := m["installation_mode"].(string)
			return s
		}
		return ""
	}
	ours, named := settings[chromeExtensionID]
	blocked := func(m string) bool { return m == "blocked" || m == "removed" }
	switch {
	case named && blocked(mode(ours)):
		out = append(out, PolicyFinding{Policy: "ExtensionSettings", Effect: "extension", Source: source, Detail: "the Daedalus extension is blocked by name"})
	case !named && blocked(mode(settings["*"])):
		out = append(out, PolicyFinding{Policy: "ExtensionSettings", Effect: "extension", Source: source, Detail: "extensions are blocked by default and the Daedalus extension is not named"})
	}
	for _, entry := range []any{settings["*"], ours} {
		if m, ok := entry.(map[string]any); ok {
			if hosts := policyList(m["runtime_blocked_hosts"]); len(hosts) > 0 {
				out = append(out, PolicyFinding{Policy: "ExtensionSettings", Effect: "sites", Source: source, Detail: "the extension may not touch: " + strings.Join(hosts, ", ")})
				break
			}
		}
	}
	return out
}

func policyList(v any) []string {
	switch list := v.(type) {
	case []string:
		return list
	case []any:
		out := make([]string, 0, len(list))
		for _, item := range list {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	case string:
		return []string{list}
	}
	return nil
}

func policyInt(v any) (int, bool) {
	switch n := v.(type) {
	case uint64:
		return int(n), true
	case int:
		return n, true
	case float64:
		return int(n), true
	case bool:
		if n {
			return 1, true
		}
		return 0, true
	case string:
		i, err := strconv.Atoi(n)
		return i, err == nil
	}
	return 0, false
}

func policyBool(v any) (bool, bool) {
	if b, ok := v.(bool); ok {
		return b, true
	}
	n, ok := policyInt(v)
	return n != 0, ok
}

func containsFold(list []string, want string) bool {
	for _, item := range list {
		if strings.EqualFold(strings.TrimSpace(item), want) {
			return true
		}
	}
	return false
}

// chromePolicyListNames are the policies Windows keeps as a subkey of numbered values rather than
// as one value.
var chromePolicyListNames = []string{"ExtensionInstallBlocklist", "ExtensionInstallAllowlist", "ExtensionAllowedTypes", "NativeMessagingBlocklist", "NativeMessagingAllowlist"}

const chromePolicyKey = `SOFTWARE\Policies\Google\Chrome`

// chromePolicyFindings reads every source of Chrome policy on the probe's system and evaluates it.
// Machine policy comes first, as Chrome ranks it.
func chromePolicyFindings(pr chromeProbe) []PolicyFinding {
	var out []PolicyFinding
	switch pr.goos {
	case "windows":
		if pr.registry == nil {
			return nil
		}
		for _, root := range []regRoot{regLocalMachine, regCurrentUser} {
			values := pr.registry.Values(root, chromePolicyKey)
			if values == nil {
				values = map[string]any{}
			}
			for _, name := range chromePolicyListNames {
				if sub := pr.registry.Values(root, chromePolicyKey+`\`+name); len(sub) > 0 {
					values[name] = registryList(sub)
				}
			}
			if len(values) > 0 {
				out = append(out, evaluateChromePolicies(root.String()+`\`+chromePolicyKey, values)...)
			}
		}
	case "darwin":
		account := pr.getenv("USER")
		if account == "" {
			if u, err := user.Current(); err == nil {
				account = u.Username
			}
		}
		files := []string{"/Library/Managed Preferences/com.google.Chrome.plist"}
		if account != "" {
			files = append(files, path.Join("/Library/Managed Preferences", account, "com.google.Chrome.plist"))
		}
		for _, file := range files {
			body, err := pr.readFile(file)
			if err != nil {
				continue
			}
			values, err := parsePlistDict(body)
			if err != nil {
				out = append(out, PolicyFinding{Policy: "managed preferences", Effect: "unknown", Source: file, Detail: "Chrome is managed and its policies could not be read here; open chrome://policy to see them"})
				continue
			}
			out = append(out, evaluateChromePolicies(file, values)...)
		}
	default:
		dir := "/etc/opt/chrome/policies/managed"
		entries, err := pr.readDir(dir)
		if err != nil {
			return nil
		}
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			if !e.IsDir() && strings.HasSuffix(e.Name(), ".json") {
				names = append(names, e.Name())
			}
		}
		sort.Strings(names)
		for _, name := range names {
			file := path.Join(dir, name)
			body, err := pr.readFile(file)
			if err != nil {
				continue
			}
			var values map[string]any
			if json.Unmarshal(body, &values) != nil {
				out = append(out, PolicyFinding{Policy: name, Effect: "unknown", Source: file, Detail: "a policy file Chrome may read could not be parsed here; open chrome://policy to see what applies"})
				continue
			}
			out = append(out, evaluateChromePolicies(file, values)...)
		}
	}
	return out
}

// registryList orders a list policy's numbered values ("1", "2", …) and keeps their strings.
func registryList(values map[string]any) []string {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool {
		a, errA := strconv.Atoi(names[i])
		b, errB := strconv.Atoi(names[j])
		if errA == nil && errB == nil {
			return a < b
		}
		return names[i] < names[j]
	})
	out := make([]string, 0, len(names))
	for _, name := range names {
		if s, ok := values[name].(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// errBinaryPlist is a plist this reader does not parse. Managed preferences are often written in
// the binary form; the finding then sends the operator to chrome://policy.
var errBinaryPlist = errors.New("binary property list")

// parsePlistDict reads the top-level dictionary of an XML property list into the same shapes the
// JSON policy files decode to: strings, float64, bools, lists and maps.
func parsePlistDict(body []byte) (map[string]any, error) {
	if strings.HasPrefix(string(body), "bplist") {
		return nil, errBinaryPlist
	}
	decoder := xml.NewDecoder(strings.NewReader(string(body)))
	for {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if start, ok := token.(xml.StartElement); ok && start.Name.Local == "dict" {
			value, err := plistValue(decoder, start)
			if err != nil {
				return nil, err
			}
			m, _ := value.(map[string]any)
			return m, nil
		}
	}
}

func plistValue(decoder *xml.Decoder, start xml.StartElement) (any, error) {
	switch start.Name.Local {
	case "dict":
		out := map[string]any{}
		key := ""
		for {
			token, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			switch t := token.(type) {
			case xml.StartElement:
				if t.Name.Local == "key" {
					var k string
					if err := decoder.DecodeElement(&k, &t); err != nil {
						return nil, err
					}
					key = k
					continue
				}
				v, err := plistValue(decoder, t)
				if err != nil {
					return nil, err
				}
				out[key] = v
			case xml.EndElement:
				return out, nil
			}
		}
	case "array":
		out := []any{}
		for {
			token, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			switch t := token.(type) {
			case xml.StartElement:
				v, err := plistValue(decoder, t)
				if err != nil {
					return nil, err
				}
				out = append(out, v)
			case xml.EndElement:
				return out, nil
			}
		}
	case "true", "false":
		if err := decoder.Skip(); err != nil {
			return nil, err
		}
		return start.Name.Local == "true", nil
	default:
		var text string
		if err := decoder.DecodeElement(&text, &start); err != nil && !errors.Is(err, io.EOF) {
			return nil, err
		}
		switch start.Name.Local {
		case "integer", "real":
			f, err := strconv.ParseFloat(strings.TrimSpace(text), 64)
			if err != nil {
				return nil, err
			}
			return f, nil
		}
		return text, nil
	}
}
