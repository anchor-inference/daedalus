package bridge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
)

// PinnedExtensionIDs are the Daedalus extensions the bridge carries. An extension's id is derived
// from the public key in its manifest, so it is fixed with the key: this is the published
// extension's (extension/static/manifest.json). More can be given with --allow-extension,
// $BROWSERD_BRIDGE_EXTENSION_IDS or the extension_ids of browserd-bridge.json, for an extension
// loaded unpacked without the key.
var PinnedExtensionIDs = []string{"lkhhklicnjlplokpadpccndipomikoaj"}

// HostName is the name the native messaging host is registered under, in the manifest Chrome reads
// and (on Windows) the registry key that points at it.
const HostName = "com.daedalus.browser"

// ConfigFile is the file beside the bridge's executable the launcher may write when it registers the
// connector: {"run_dir": "...", "extension_ids": ["..."]}. Chrome starts a native host with no
// arguments of the launcher's and no environment it controls, so this is how a launcher that keeps
// its installation somewhere other than the default tells the bridge where it is.
const ConfigFile = "browserd-bridge.json"

// RunDirEnv names the user daemon's run directory, before the file and the default.
const RunDirEnv = "BROWSERD_BRIDGE_RUN_DIR"

// ExtensionIDsEnv adds extension ids, separated by commas.
const ExtensionIDsEnv = "BROWSERD_BRIDGE_EXTENSION_IDS"

// UserRunDir is where the user daemon's run directory is in an installation's local state, beside
// the host daemon's browserd/run.
const UserRunDir = "browserd-user/run"

var extensionID = regexp.MustCompile(`^[a-p]{32}$`)

// IsOrigin reports whether s is an extension's origin, which is how Chrome names the extension that
// started its native host.
func IsOrigin(s string) bool { return strings.HasPrefix(s, "chrome-extension://") }

// OriginID is the extension id of an origin "chrome-extension://<id>/".
func OriginID(origin string) (string, error) {
	id := strings.TrimSuffix(strings.TrimPrefix(origin, "chrome-extension://"), "/")
	if !IsOrigin(origin) || !extensionID.MatchString(id) {
		return "", fmt.Errorf("%q is not an extension's origin", origin)
	}
	return id, nil
}

// fileConfig is browserd-bridge.json.
type fileConfig struct {
	RunDir       string   `json:"run_dir"`
	ExtensionIDs []string `json:"extension_ids"`
}

func readConfig(exe string) (fileConfig, error) {
	var fc fileConfig
	if exe == "" {
		return fc, nil
	}
	b, err := os.ReadFile(filepath.Join(filepath.Dir(exe), ConfigFile))
	if errors.Is(err, os.ErrNotExist) {
		return fc, nil
	}
	if err != nil {
		return fc, err
	}
	if err := json.Unmarshal(b, &fc); err != nil {
		return fc, fmt.Errorf("%s: %w", ConfigFile, err)
	}
	return fc, nil
}

// The default installation's run directory, worked out as the desktop launcher works out its own
// folders (desktop/relocate.go, desktop/local.go): the standard data folder of this user, its key —
// the folder's name and a digest of its resolved path — and the local state folder of that key.
// A launcher whose data folder is elsewhere writes browserd-bridge.json instead.

// DefaultRunDir is the user daemon's run directory of the installation whose data folder is data,
// or of the standard one when data is empty.
func DefaultRunDir(data string, getenv func(string) string) (string, error) {
	if data == "" {
		var err error
		if data, err = standardDataDir(runtime.GOOS, getenv); err != nil {
			return "", err
		}
	}
	abs, err := filepath.Abs(data)
	if err != nil {
		return "", err
	}
	key := localKey(abs, runtime.GOOS, filepath.EvalSymlinks)
	var state string
	if root := strings.TrimSpace(getenv("DAEDALUS_LOCAL_ROOT")); root != "" {
		if !filepath.IsAbs(root) {
			return "", errors.New("DAEDALUS_LOCAL_ROOT must be an absolute path")
		}
		state = filepath.Join(root, "state", key)
	} else {
		base, err := stateBase(runtime.GOOS, getenv)
		if err != nil {
			return "", err
		}
		state = filepath.Join(base, key)
	}
	return filepath.Join(state, filepath.FromSlash(UserRunDir)), nil
}

func home(getenv func(string) string) string {
	if h := getenv("HOME"); h != "" {
		return h
	}
	h, _ := os.UserHomeDir()
	return h
}

func xdgDir(getenv func(string) string, name string, fallback ...string) string {
	if v := getenv(name); filepath.IsAbs(v) {
		return v
	}
	h := home(getenv)
	if h == "" {
		return ""
	}
	return filepath.Join(append([]string{h}, fallback...)...)
}

func standardDataDir(goos string, getenv func(string) string) (string, error) {
	switch goos {
	case "windows":
		base := getenv("LOCALAPPDATA")
		if base == "" {
			return "", errors.New("%LOCALAPPDATA% is not set")
		}
		return filepath.Join(base, "Daedalus", "data"), nil
	case "darwin":
		h := home(getenv)
		if h == "" {
			return "", errors.New("no home folder")
		}
		return filepath.Join(h, "Library", "Application Support", "Daedalus", "data"), nil
	}
	share := xdgDir(getenv, "XDG_DATA_HOME", ".local", "share")
	if share == "" {
		return "", errors.New("neither $XDG_DATA_HOME nor $HOME is set")
	}
	return filepath.Join(share, "daedalus", "data"), nil
}

func stateBase(goos string, getenv func(string) string) (string, error) {
	switch goos {
	case "windows":
		base := getenv("LOCALAPPDATA")
		if base == "" {
			return "", errors.New("%LOCALAPPDATA% is not set")
		}
		return filepath.Join(base, "Daedalus", "State"), nil
	case "darwin":
		h := home(getenv)
		if h == "" {
			return "", errors.New("no home folder")
		}
		return filepath.Join(h, "Library", "Application Support", "Daedalus", "State"), nil
	}
	state := xdgDir(getenv, "XDG_STATE_HOME", ".local", "state")
	if state == "" {
		return "", errors.New("neither $XDG_STATE_HOME nor $HOME is set")
	}
	return filepath.Join(state, "daedalus"), nil
}

// localKey is the desktop launcher's key of a data folder: the folder's name, then 12 hex digits of
// the SHA-256 of its resolved path, folded to lower case where the filesystem ignores case.
func localKey(data, goos string, resolve func(string) (string, error)) string {
	canonical := canonicalPath(filepath.Clean(data), resolve)
	if goos == "darwin" || goos == "windows" {
		canonical = strings.ToLower(canonical)
	}
	sum := sha256.Sum256([]byte(canonical))
	return keyName(filepath.Base(data)) + "-" + hex.EncodeToString(sum[:])[:12]
}

func canonicalPath(path string, resolve func(string) (string, error)) string {
	var rest []string
	for current := path; ; {
		if resolved, err := resolve(current); err == nil {
			for i := len(rest) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, rest[i])
			}
			return resolved
		}
		parent := filepath.Dir(current)
		if parent == current {
			return path
		}
		rest = append(rest, filepath.Base(current))
		current = parent
	}
}

func keyName(name string) string {
	var b strings.Builder
	for _, r := range name {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
			b.WriteRune(r)
		}
		if b.Len() >= 24 {
			break
		}
	}
	if b.Len() == 0 {
		return "data"
	}
	return b.String()
}
