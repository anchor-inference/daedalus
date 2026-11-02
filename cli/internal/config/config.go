// Package config is where daedalus-cli keeps what it knows about the servers it talks to: one
// profile per server, each with its address and the device identity pairing gave it. Secrets are
// not here; they live in the credential store (internal/auth), and a profile only names them.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// DefaultProfile is the profile used when nothing names one.
const DefaultProfile = "default"

// Environment variables that override the file. They are first-class rather than test hooks: a
// script points one binary at a server without writing a profile first.
const (
	EnvConfigDir = "DAEDALUS_CLI_CONFIG_DIR"
	EnvProfile   = "DAEDALUS_PROFILE"
	EnvServer    = "DAEDALUS_SERVER"
)

// Profile is one server.
type Profile struct {
	// Server is the base URL of the Daedalus API, without a trailing slash.
	Server string `json:"server"`
	// DeviceID and DeviceName are what pairing registered this machine as. Empty before pairing,
	// and for a profile that authenticates with the static API token instead.
	DeviceID   string `json:"device_id,omitempty"`
	DeviceName string `json:"device_name,omitempty"`
	// Guard is the local guard level a session starts in: ask, auto or plan.
	Guard string `json:"guard,omitempty"`
}

// File is the whole configuration file.
type File struct {
	DefaultProfile string             `json:"default_profile,omitempty"`
	Profiles       map[string]Profile `json:"profiles"`

	path string
}

// Dir is the directory the configuration and the credential fallback live in.
func Dir() (string, error) {
	if dir := os.Getenv(EnvConfigDir); dir != "" {
		return dir, nil
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", fmt.Errorf("locate the configuration directory: %w", err)
	}
	return filepath.Join(base, "daedalus-cli"), nil
}

// Load reads the configuration file. A missing file is an empty configuration, which is what a
// fresh installation has.
func Load() (*File, error) {
	dir, err := Dir()
	if err != nil {
		return nil, err
	}
	f := &File{Profiles: map[string]Profile{}, path: filepath.Join(dir, "config.json")}
	raw, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		return f, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", f.path, err)
	}
	if err := json.Unmarshal(raw, f); err != nil {
		// Starting over would silently forget every pairing; saying where the file is lets the
		// operator fix one typo instead.
		return nil, fmt.Errorf("%s is not valid JSON: %w", f.path, err)
	}
	if f.Profiles == nil {
		f.Profiles = map[string]Profile{}
	}
	return f, nil
}

// Path is the file Save writes.
func (f *File) Path() string { return f.path }

// Save writes the file atomically, readable by its owner only: it names servers and devices, which
// is nothing secret, but nothing another user of the machine needs either.
func (f *File) Save() error {
	if err := os.MkdirAll(filepath.Dir(f.path), 0o700); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(f.path), err)
	}
	body, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(f.path), ".config-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer func() { _ = os.Remove(name) }()
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(append(body, '\n')); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, f.path)
}

var profileName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// ValidProfileName reports whether name can be a profile: it becomes a keychain item name, so it is
// kept to characters every keychain accepts.
func ValidProfileName(name string) bool { return profileName.MatchString(name) }

// Selected is the profile a command runs against, after the flags and the environment had their say.
type Selected struct {
	Name    string
	Profile Profile
	// Known is false when the profile is not in the file yet (a first login).
	Known bool
}

// Select picks the profile: the flag, then DAEDALUS_PROFILE, then the file's default, then
// "default". The server address is the flag, then DAEDALUS_SERVER, then the profile's.
func (f *File) Select(profileFlag, serverFlag string) (Selected, error) {
	name := profileFlag
	if name == "" {
		name = os.Getenv(EnvProfile)
	}
	if name == "" {
		name = f.DefaultProfile
	}
	if name == "" {
		name = DefaultProfile
	}
	if !ValidProfileName(name) {
		return Selected{}, fmt.Errorf("profile name %q: letters, digits, '.', '_' and '-' only", name)
	}
	p, known := f.Profiles[name]
	server := serverFlag
	if server == "" {
		server = os.Getenv(EnvServer)
	}
	if server != "" {
		norm, err := NormalizeServer(server)
		if err != nil {
			return Selected{}, err
		}
		p.Server = norm
	}
	return Selected{Name: name, Profile: p, Known: known}, nil
}

// Put stores a profile and, when the file had no default yet, makes it the default.
func (f *File) Put(name string, p Profile) {
	f.Profiles[name] = p
	if f.DefaultProfile == "" {
		f.DefaultProfile = name
	}
}

// Remove forgets a profile.
func (f *File) Remove(name string) {
	delete(f.Profiles, name)
	if f.DefaultProfile == name {
		f.DefaultProfile = ""
	}
}

// Names are the profiles, sorted.
func (f *File) Names() []string {
	out := make([]string, 0, len(f.Profiles))
	for name := range f.Profiles {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// NormalizeServer turns what an operator types into a base URL: a scheme is added when missing
// (http, since a bare address is a machine on the operator's own network), the trailing slash and an
// `/app` suffix copied from the browser are dropped, and anything with a query or fragment is refused.
func NormalizeServer(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errors.New("the server address is empty")
	}
	if !strings.Contains(raw, "://") {
		raw = "http://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("server address %q: %w", raw, err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("server address %q: the scheme must be http or https", raw)
	}
	if u.Host == "" {
		return "", fmt.Errorf("server address %q has no host", raw)
	}
	if strings.ContainsAny(u.Host, "<>{} ") {
		// The documentation's own placeholder, pasted as it stands.
		return "", fmt.Errorf("server address %q: put the server's real host and port in place of the placeholder", raw)
	}
	if u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("server address %q: no query or fragment", raw)
	}
	p := strings.TrimRight(u.Path, "/")
	p = strings.TrimSuffix(p, "/app")
	u.Path = strings.TrimRight(p, "/")
	return u.String(), nil
}
