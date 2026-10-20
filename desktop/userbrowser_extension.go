package main

// The unpacked extension a release carries, and the copy of it the operator loads into Chrome.
//
// Chrome remembers an unpacked extension by its folder, so the folder the operator chose once has
// to stay where it is and be refreshed in place on every release: <local>/browser-extension. The
// release's own copy beside the launcher is not that folder — an upgrade moves the launcher's
// files aside, and Chrome would lose the extension with them.
//
// The copy arrived inside the signed installer or archive, as the seed did, and is checked the
// same way before it is used: every file against the digests in its SHA256SUMS, nothing unlisted,
// and the manifest's public key against the pinned extension id. A damaged or swapped payload is
// refused and the folder already in place is kept.

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
)

const (
	extensionDirName  = "browser-extension"
	extensionSumsName = "SHA256SUMS"
	// extensionFileLimit and extensionFileCount wall a damaged list off from memory and disk; the
	// built extension is a few hundred kilobytes in a few dozen files.
	extensionFileLimit = 16 << 20
	extensionFileCount = 1000
)

func extensionDir(p Paths) string { return filepath.Join(p.Local, extensionDirName) }

// ExtensionState is what the app shows of the extension folder.
type ExtensionState struct {
	Path    string `json:"path"`
	Ready   bool   `json:"ready"`
	Version string `json:"version,omitempty"`
	Error   string `json:"error,omitempty"`
}

// bundledExtension is the release's extension beside the launcher (in the Mac application under
// Contents/Resources), or DAEDALUS_BROWSER_EXTENSION for one built by hand. "" when there is none.
func bundledExtension(exe string, getenv func(string) string, exists func(string) bool) string {
	if dir := getenv("DAEDALUS_BROWSER_EXTENSION"); dir != "" {
		return dir
	}
	if exe == "" {
		return ""
	}
	candidates := []string{filepath.Join(filepath.Dir(exe), extensionDirName)}
	if app, ok := bundleRoot(exe); ok {
		candidates = append(candidates, filepath.Join(app, "Contents", "Resources", extensionDirName))
	}
	for _, dir := range candidates {
		if exists(filepath.Join(dir, extensionSumsName)) {
			return dir
		}
	}
	return ""
}

// extensionIDFromKey is the id Chrome gives an extension whose manifest carries this public key:
// the first 128 bits of the key's SHA-256, each hex digit spelled with the letters a to p.
func extensionIDFromKey(key string) (string, error) {
	der, err := base64.StdEncoding.DecodeString(strings.TrimSpace(key))
	if err != nil {
		return "", fmt.Errorf("the manifest's key is not base64: %w", err)
	}
	sum := sha256.Sum256(der)
	digits := hex.EncodeToString(sum[:16])
	out := make([]byte, len(digits))
	for i, d := range []byte(digits) {
		if d >= 'a' {
			out[i] = 'a' + 10 + (d - 'a')
		} else {
			out[i] = 'a' + (d - '0')
		}
	}
	return string(out), nil
}

// readExtensionSums parses a SHA256SUMS: "<hex digest>  <path>" per line, paths relative with
// forward slashes. A path that climbs out of the folder or is absolute is a damaged list.
func readExtensionSums(body []byte) (map[string]string, error) {
	sums := map[string]string{}
	scanner := bufio.NewScanner(bytes.NewReader(body))
	for scanner.Scan() {
		line := strings.TrimRight(scanner.Text(), "\r")
		if strings.TrimSpace(line) == "" {
			continue
		}
		digest, name, ok := strings.Cut(line, " ")
		name = strings.TrimPrefix(strings.TrimLeft(name, " "), "*")
		if !ok || len(digest) != 64 || name == "" {
			return nil, fmt.Errorf("a line of %s is not a digest and a path: %q", extensionSumsName, line)
		}
		if _, err := hex.DecodeString(digest); err != nil {
			return nil, fmt.Errorf("a digest in %s is not hex: %q", extensionSumsName, line)
		}
		clean := path.Clean(name)
		if clean != name || path.IsAbs(name) || strings.HasPrefix(clean, "../") || clean == ".." || strings.Contains(name, `\`) || name == extensionSumsName {
			return nil, fmt.Errorf("%s names a path outside the extension: %q", extensionSumsName, name)
		}
		sums[name] = strings.ToLower(digest)
		if len(sums) > extensionFileCount {
			return nil, fmt.Errorf("%s lists more files than an extension has", extensionSumsName)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if _, ok := sums["manifest.json"]; !ok {
		return nil, fmt.Errorf("%s does not list manifest.json", extensionSumsName)
	}
	return sums, nil
}

// verifyExtension checks a folder against its SHA256SUMS and the pinned id, and returns the
// manifest's version. Every file must be listed and match; every listed file must be there.
func verifyExtension(dir string) (version string, sums []byte, err error) {
	sums, err = os.ReadFile(filepath.Join(dir, extensionSumsName))
	if err != nil {
		return "", nil, err
	}
	listed, err := readExtensionSums(sums)
	if err != nil {
		return "", nil, err
	}
	seen := map[string]bool{}
	walkErr := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(dir, p)
		rel = filepath.ToSlash(rel)
		if d.IsDir() || rel == extensionSumsName {
			return nil
		}
		if !d.Type().IsRegular() {
			return fmt.Errorf("%s is not a regular file", rel)
		}
		want, ok := listed[rel]
		if !ok {
			return fmt.Errorf("%s is not listed in %s", rel, extensionSumsName)
		}
		got, err := fileDigest(p)
		if err != nil {
			return err
		}
		if got != want {
			return fmt.Errorf("%s does not match its digest", rel)
		}
		seen[rel] = true
		return nil
	})
	if walkErr != nil {
		return "", nil, walkErr
	}
	for name := range listed {
		if !seen[name] {
			return "", nil, fmt.Errorf("%s is listed but missing", name)
		}
	}
	body, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		return "", nil, err
	}
	var manifest struct {
		Key     string `json:"key"`
		Version string `json:"version"`
	}
	if err := json.Unmarshal(body, &manifest); err != nil {
		return "", nil, fmt.Errorf("the extension's manifest.json: %w", err)
	}
	id, err := extensionIDFromKey(manifest.Key)
	if err != nil {
		return "", nil, err
	}
	if id != chromeExtensionID {
		return "", nil, fmt.Errorf("the extension's key gives the id %s, not Daedalus's %s", id, chromeExtensionID)
	}
	return manifest.Version, sums, nil
}

func fileDigest(p string) (string, error) {
	file, err := os.Open(p)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	n, err := io.Copy(hash, io.LimitReader(file, extensionFileLimit+1))
	if err != nil {
		return "", err
	}
	if n > extensionFileLimit {
		return "", fmt.Errorf("%s is larger than any file of an extension", filepath.Base(p))
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// installExtension brings dst up to the release's extension in src. Nothing is done when dst
// already holds the same files; otherwise src is verified, copied beside dst, verified again
// there, and swapped in. dst keeps its path, which is what Chrome knows the extension by.
func installExtension(src, dst string) (ExtensionState, error) {
	state := ExtensionState{Path: dst}
	if src == "" {
		if version, _, err := verifyExtension(dst); err == nil {
			state.Ready, state.Version = true, version
			return state, nil
		}
		state.Error = "this build carries no browser extension"
		return state, nil
	}
	version, sums, err := verifyExtension(src)
	if err != nil {
		state.Error = "the release's extension failed its check: " + err.Error()
		return keepExisting(state, dst), errors.New(state.Error)
	}
	if current, err := os.ReadFile(filepath.Join(dst, extensionSumsName)); err == nil && bytes.Equal(current, sums) {
		if _, _, err := verifyExtension(dst); err == nil {
			state.Ready, state.Version = true, version
			return state, nil
		}
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return keepExisting(state, dst), err
	}
	staged := dst + ".new"
	_ = os.RemoveAll(staged)
	if err := copyTree(src, staged); err != nil {
		_ = os.RemoveAll(staged)
		return keepExisting(state, dst), err
	}
	if _, _, err := verifyExtension(staged); err != nil {
		_ = os.RemoveAll(staged)
		return keepExisting(state, dst), fmt.Errorf("the copied extension failed its check: %w", err)
	}
	old := dst + ".old"
	_ = os.RemoveAll(old)
	if exists(dst) {
		if err := os.Rename(dst, old); err != nil {
			_ = os.RemoveAll(staged)
			return keepExisting(state, dst), err
		}
	}
	if err := os.Rename(staged, dst); err != nil {
		_ = os.Rename(old, dst)
		return keepExisting(state, dst), err
	}
	_ = os.RemoveAll(old)
	state.Ready, state.Version = true, version
	return state, nil
}

// keepExisting reports the folder already in place when a new one could not be put there: the
// operator's Chrome goes on running the extension it has.
func keepExisting(state ExtensionState, dst string) ExtensionState {
	if version, _, err := verifyExtension(dst); err == nil {
		state.Ready, state.Version = true, version
	}
	return state
}
