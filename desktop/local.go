package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// The data folder holds what cannot be rebuilt: the database, the workspaces, the keys, the
// checkouts with whatever the agent committed. Everything the launcher downloads or builds lives
// elsewhere, per the platform's own split between data and cache:
//
//   - the runtime — uv, the interpreter, the environments, uv's cache, the extras — is a cache: it
//     can be deleted and is rebuilt on the next start;
//   - the local state — logs, the records of running children, the daemons' endpoints and tokens,
//     the terminal logs and the browser profiles — belongs to this machine and this installation but
//     is not part of what an upgrade must carry.
//
// Keeping both out of the data folder is what lets an upgrade copy and fence only the data. uv
// installs an environment by hard-linking files out of its cache, and a hard link is a second name
// the fence cannot watch; five thousand of them in one venv would refuse every switch.
//
//	Linux, BSD  $XDG_CACHE_HOME/daedalus/<key>         (~/.cache)          runtime
//	            $XDG_STATE_HOME/daedalus/<key>         (~/.local/state)    local state
//	macOS       ~/Library/Application Support/Daedalus/Runtime/<key>, …/State/<key>
//	Windows     %LOCALAPPDATA%\Daedalus\Runtime\<key>, …\State\<key>
//
// macOS does not get ~/Library/Caches: the system may empty it under storage pressure while the
// agent is running out of the environment in it, and a vanished interpreter mid-run is worse than
// the space. %LOCALAPPDATA% on Windows is the machine-local, non-roaming profile, which is what an
// interpreter and a venv must be.
//
// <key> is the data folder's name and a digest of its absolute path: two installations on one
// machine never share an environment, and moving a data folder costs one rebuild, nothing more.
// DAEDALUS_LOCAL_ROOT puts both under one folder (runtime/<key> and state/<key>); the tests use it.

// localRoots returns the runtime and the local state folder for a data folder.
func localRoots(data string) (runtimeDir, stateDir string, err error) {
	sum := sha256.Sum256([]byte(data))
	key := localKeyName(filepath.Base(data)) + "-" + hex.EncodeToString(sum[:])[:12]
	if root := strings.TrimSpace(os.Getenv("DAEDALUS_LOCAL_ROOT")); root != "" {
		if !filepath.IsAbs(root) {
			return "", "", errors.New("DAEDALUS_LOCAL_ROOT must be an absolute path")
		}
		runtimeDir, stateDir = filepath.Join(root, "runtime", key), filepath.Join(root, "state", key)
	} else {
		cache, state, err := platformLocalBases()
		if err != nil {
			return "", "", err
		}
		runtimeDir, stateDir = filepath.Join(cache, key), filepath.Join(state, key)
	}
	for _, dir := range []string{runtimeDir, stateDir} {
		if within(dir, data) {
			return "", "", errors.New("the runtime and the local state must be outside the data folder, so that an upgrade never copies or fences them: " + dir)
		}
	}
	return runtimeDir, stateDir, nil
}

func localKeyName(name string) string {
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

func platformLocalBases() (cache, state string, err error) {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			return "", "", errors.New("%LOCALAPPDATA% is not set")
		}
		return filepath.Join(base, "Daedalus", "Runtime"), filepath.Join(base, "Daedalus", "State"), nil
	case "darwin":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", "", err
		}
		support := filepath.Join(home, "Library", "Application Support", "Daedalus")
		return filepath.Join(support, "Runtime"), filepath.Join(support, "State"), nil
	}
	home, _ := os.UserHomeDir()
	cache = xdgDir("XDG_CACHE_HOME", home, ".cache")
	state = xdgDir("XDG_STATE_HOME", home, ".local", "state")
	if cache == "" || state == "" {
		return "", "", errors.New("neither $XDG_CACHE_HOME/$XDG_STATE_HOME nor $HOME is set")
	}
	return filepath.Join(cache, "daedalus"), filepath.Join(state, "daedalus"), nil
}

// xdgDir follows the base-directory specification: a relative value is invalid and ignored.
func xdgDir(name, home string, fallback ...string) string {
	if value := os.Getenv(name); filepath.IsAbs(value) {
		return value
	}
	if home == "" {
		return ""
	}
	return filepath.Join(append([]string{home}, fallback...)...)
}

// within reports whether path is dir itself or lies under it.
func within(path, dir string) bool {
	rel, err := filepath.Rel(dir, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}
