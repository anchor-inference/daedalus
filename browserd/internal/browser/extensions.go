package browser

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
	"unicode/utf16"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The agent's extensions: unpacked ones it builds in its workspace, loaded into its browser over
// DevTools (Extensions.loadUnpacked, which Chromium offers on the debugging pipe once started with
// --enable-unsafe-extension-debugging; measured on 151 and 156, headed and headless). The command
// line's --load-extension would have meant restarting the browser for every change, and branded
// Chrome stopped honouring it in 137; the DevTools call works in both and on the running browser.
//
// The files come from the host as an archive (the upload area every file the host hands in uses),
// are unpacked into the profile, and are loaded again each time the profile's browser starts, so an
// extension stays installed until it is removed. Installing from the same source again replaces the
// files and reloads it: the agent's loop of edit, reload, look.
const (
	MaxExtensions      = 16
	MaxExtensionBytes  = 100 << 20
	MaxExtensionFiles  = 5000
	extensionsDir      = "extensions"
	extensionsFile     = "extensions.json"
	extensionLoadLimit = 30 * time.Second
)

// Extension is one of the profile's extensions.
type Extension struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Version string `json:"version"`
	// Dir is its folder under the profile's extensions folder.
	Dir string `json:"dir"`
	// Source is where the host said the files came from: a folder or an archive of the agent's.
	Source      string    `json:"source,omitempty"`
	InstalledAt time.Time `json:"installed_at"`
	// Enabled and Error are the browser's word on it now, not kept.
	Enabled bool   `json:"enabled"`
	Error   string `json:"error,omitempty"`
}

func (b *Browser) extensionsRoot() string { return b.devtoolsPath(extensionsDir) }

func (b *Browser) loadExtensionIndex() []Extension {
	data, err := os.ReadFile(b.devtoolsPath(extensionsFile))
	if err != nil {
		return nil
	}
	var list []Extension
	if json.Unmarshal(data, &list) != nil {
		_ = os.Rename(b.devtoolsPath(extensionsFile), b.devtoolsPath(extensionsFile+".unreadable"))
		return nil
	}
	return list
}

func (b *Browser) saveExtensionIndex(list []Extension) error {
	kept := make([]Extension, len(list))
	for i, e := range list {
		e.Enabled, e.Error = false, ""
		kept[i] = e
	}
	return writeJSON(b.devtoolsPath(extensionsFile), kept)
}

// extensionsRefused says why a browser takes no extension: the operator's Chrome is theirs, and a
// throwaway browser's contexts are the incognito kind, where an extension does not run.
func extensionsRefused(b *Browser) error {
	if b.attached {
		return ErrAttachRefused("extensions")
	}
	if b.Ephemeral || b.profileDir == "" {
		return wire.Errorf(wire.CodeUnsupported, "a throwaway browser takes no extensions: install it in your own browser (not fresh)")
	}
	return nil
}

// ExtensionID is the id Chromium gives an unpacked extension at dir: the first half of the SHA-256
// of its path, in the letters a-p. Known before the load, so the extension's service worker, which
// starts during it, is followed from its first line.
func ExtensionID(dir string) string {
	raw := []byte(dir)
	if runtime.GOOS == "windows" {
		// Chromium hashes the path's own UTF-16 code units there, with the drive letter upper-cased.
		if len(dir) > 1 && dir[1] == ':' {
			dir = strings.ToUpper(dir[:1]) + dir[1:]
		}
		units := utf16.Encode([]rune(dir))
		raw = make([]byte, 2*len(units))
		for i, u := range units {
			binary.LittleEndian.PutUint16(raw[2*i:], u)
		}
	}
	sum := sha256.Sum256(raw)
	h := hex.EncodeToString(sum[:16])
	out := make([]byte, len(h))
	for i := range h {
		c := h[i]
		if c >= 'a' {
			c = c - 'a' + 10
		} else {
			c -= '0'
		}
		out[i] = 'a' + c
	}
	return string(out)
}

// wantExtension marks an extension id as the agent's, so its workers are followed (followWorker).
func (b *Browser) wantExtension(id string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.extWanted == nil {
		b.extWanted = map[string]bool{}
	}
	b.extWanted[id] = true
}

func (b *Browser) wantsExtension(id string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.extWanted[id]
}

// restoreExtensions loads the profile's extensions into its browser as it starts. One that no longer
// loads (its files went, or a newer Chromium refuses its manifest) stays listed with the reason.
func (m *Manager) restoreExtensions(ctx context.Context, b *Browser) {
	list := b.loadExtensionIndex()
	if len(list) == 0 {
		return
	}
	errs := map[string]string{}
	for i, e := range list {
		dir := filepath.Join(b.extensionsRoot(), e.Dir)
		b.wantExtension(ExtensionID(dir))
		id, err := m.loadUnpacked(ctx, b, dir)
		if err != nil {
			errs[e.Dir] = err.Error()
			m.log.Warn("extension not restored", "browser", b.ID, "extension", e.Name, "error", err.Error())
			continue
		}
		list[i].ID = id
	}
	_ = b.saveExtensionIndex(list)
	b.mu.Lock()
	b.extErrors = errs
	b.mu.Unlock()
}

func (m *Manager) loadUnpacked(ctx context.Context, b *Browser, dir string) (string, error) {
	lctx, cancel := context.WithTimeout(ctx, extensionLoadLimit)
	defer cancel()
	var r struct {
		ID string `json:"id"`
	}
	if err := b.conn.Call(lctx, "", "Extensions.loadUnpacked", map[string]any{"path": dir}, &r); err != nil {
		if strings.Contains(err.Error(), "wasn't found") || strings.Contains(err.Error(), "not found") {
			return "", wire.Errorf(wire.CodeUnsupported, "this Chromium does not load extensions over DevTools (%v); it needs Chromium 126 or later", err)
		}
		return "", fmt.Errorf("Chromium did not load the extension: %w", err)
	}
	b.wantExtension(r.ID)
	m.followWorkers(lctx, b, r.ID)
	return r.ID, nil
}

// InstallExtension unpacks an archive (a .zip of the extension's folder, or a .crx) into the
// profile and loads it. An extension from the same source, or with the same name when no source is
// given, is replaced and reloaded.
func (m *Manager) InstallExtension(ctx context.Context, g *Group, archive, source string) (Extension, error) {
	b := g.Browser
	if err := extensionsRefused(b); err != nil {
		return Extension{}, err
	}
	data, err := os.ReadFile(archive)
	if err != nil {
		return Extension{}, err
	}
	files, manifest, err := readExtensionArchive(data)
	if err != nil {
		return Extension{}, err
	}
	b.extMu.Lock()
	defer b.extMu.Unlock()
	list := b.loadExtensionIndex()
	slot := -1
	for i, e := range list {
		if (source != "" && e.Source == source) || (source == "" && e.Source == "" && e.Name == manifest.Name) {
			slot = i
		}
	}
	if slot < 0 && len(list) >= MaxExtensions {
		return Extension{}, errLimit("extensions", MaxExtensions, "this browser has %d extensions, the most it keeps; remove one", len(list))
	}
	var e Extension
	if slot >= 0 {
		e = list[slot]
	} else {
		e = Extension{Dir: uniqueDir(list, slug(manifest.Name)), Source: source}
	}
	e.Name, e.Version, e.InstalledAt = manifest.Name, manifest.Version, time.Now().UTC()
	root := b.extensionsRoot()
	final := filepath.Join(root, e.Dir)
	staging := filepath.Join(root, "."+e.Dir+".new-"+randomSuffix())
	if err := writeExtensionFiles(staging, files); err != nil {
		_ = os.RemoveAll(staging)
		return Extension{}, err
	}
	// The new files take the old folder's place, so the extension keeps its path and with it its id,
	// its storage and its permissions: what reloading an unpacked extension does in chrome://extensions.
	_ = os.RemoveAll(final)
	if err := os.Rename(staging, final); err != nil {
		_ = os.RemoveAll(staging)
		return Extension{}, err
	}
	b.wantExtension(ExtensionID(final))
	id, err := m.loadUnpacked(ctx, b, final)
	if err != nil {
		if slot < 0 {
			_ = os.RemoveAll(final)
		}
		return Extension{}, err
	}
	e.ID = id
	if slot >= 0 {
		list[slot] = e
	} else {
		list = append(list, e)
	}
	if err := b.saveExtensionIndex(list); err != nil {
		return Extension{}, fmt.Errorf("keeping the extension in the profile: %w", err)
	}
	b.mu.Lock()
	delete(b.extErrors, e.Dir)
	b.mu.Unlock()
	e.Enabled = true
	return e, nil
}

// UninstallExtension removes one of the profile's extensions, by id, name or folder.
func (m *Manager) UninstallExtension(ctx context.Context, g *Group, which string) (Extension, error) {
	b := g.Browser
	if err := extensionsRefused(b); err != nil {
		return Extension{}, err
	}
	b.extMu.Lock()
	defer b.extMu.Unlock()
	list := b.loadExtensionIndex()
	i := findExtension(list, which)
	if i < 0 {
		return Extension{}, wire.Errorf(wire.CodeNotFound, "no extension %q in this browser", which)
	}
	e := list[i]
	if e.ID != "" {
		if err := b.conn.Call(ctx, "", "Extensions.uninstall", map[string]any{"id": e.ID}, nil); err != nil {
			m.log.Info("extension uninstall", "browser", b.ID, "extension", e.ID, "error", err.Error())
		}
	}
	_ = os.RemoveAll(filepath.Join(b.extensionsRoot(), e.Dir))
	list = append(list[:i], list[i+1:]...)
	if err := b.saveExtensionIndex(list); err != nil {
		return Extension{}, err
	}
	return e, nil
}

// FindExtension is one of the profile's extensions by id, name or folder.
func (m *Manager) FindExtension(g *Group, which string) (Extension, bool) {
	list := g.Browser.loadExtensionIndex()
	if i := findExtension(list, which); i >= 0 {
		return list[i], true
	}
	return Extension{}, false
}

func findExtension(list []Extension, which string) int {
	which = strings.TrimSpace(which)
	for i, e := range list {
		if e.ID == which || e.Dir == which {
			return i
		}
	}
	for i, e := range list {
		if strings.EqualFold(e.Name, which) {
			return i
		}
	}
	return -1
}

// Extensions lists the profile's extensions with what the browser says of each now.
func (m *Manager) Extensions(ctx context.Context, g *Group) ([]Extension, error) {
	b := g.Browser
	if err := extensionsRefused(b); err != nil {
		return nil, err
	}
	list := b.loadExtensionIndex()
	var live struct {
		Extensions []struct {
			ID      string `json:"id"`
			Enabled bool   `json:"enabled"`
		} `json:"extensions"`
	}
	enabled := map[string]bool{}
	known := false
	if err := b.conn.Call(ctx, "", "Extensions.getExtensions", nil, &live); err == nil {
		known = true
		for _, e := range live.Extensions {
			enabled[e.ID] = e.Enabled
		}
	}
	b.mu.Lock()
	errs := b.extErrors
	b.mu.Unlock()
	for i := range list {
		list[i].Error = errs[list[i].Dir]
		on, loaded := enabled[list[i].ID]
		switch {
		case !known:
			list[i].Enabled = list[i].Error == ""
		case !loaded && list[i].Error == "":
			list[i].Error = "not loaded in the running browser"
		default:
			list[i].Enabled = on
		}
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Name < list[j].Name })
	if list == nil {
		list = []Extension{}
	}
	return list, nil
}

// ExtensionIDs are the ids of the profile's extensions, for the logs of their workers.
func (b *Browser) ExtensionIDs() map[string]string {
	out := map[string]string{}
	for _, e := range b.loadExtensionIndex() {
		out[e.ID] = e.Name
	}
	return out
}

type extensionManifest struct {
	Name            string `json:"name"`
	Version         string `json:"version"`
	ManifestVersion int    `json:"manifest_version"`
}

// readExtensionArchive reads a zip, or a crx (a zip behind a signed header), into its files, with
// the folder holding manifest.json as the root: an archive of the folder itself, not only of its
// contents, is what zipping a folder usually makes.
func readExtensionArchive(data []byte) (map[string][]byte, extensionManifest, error) {
	var man extensionManifest
	if bytes.HasPrefix(data, []byte("Cr24")) {
		if len(data) < 16 {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "the .crx file is cut short")
		}
		switch binary.LittleEndian.Uint32(data[4:8]) {
		case 3:
			n := int(binary.LittleEndian.Uint32(data[8:12]))
			if 12+n > len(data) {
				return nil, man, wire.Errorf(wire.CodeInvalidParams, "the .crx file is cut short")
			}
			data = data[12+n:]
		case 2:
			key, sig := int(binary.LittleEndian.Uint32(data[8:12])), int(binary.LittleEndian.Uint32(data[12:16]))
			if 16+key+sig > len(data) {
				return nil, man, wire.Errorf(wire.CodeInvalidParams, "the .crx file is cut short")
			}
			data = data[16+key+sig:]
		default:
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "a .crx of an unknown version")
		}
	}
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, man, wire.Errorf(wire.CodeInvalidParams, "not a zip or crx archive: %v", err)
	}
	if len(zr.File) > MaxExtensionFiles {
		return nil, man, wire.Errorf(wire.CodeInvalidParams, "an extension is at most %d files", MaxExtensionFiles)
	}
	root := ""
	found := false
	for _, f := range zr.File {
		name := path.Clean(strings.ReplaceAll(f.Name, "\\", "/"))
		if path.Base(name) == "manifest.json" && !f.FileInfo().IsDir() {
			dir := path.Dir(name)
			if dir == "." {
				dir = ""
			}
			if !found || len(dir) < len(root) {
				root, found = dir, true
			}
		}
	}
	if !found {
		return nil, man, wire.Errorf(wire.CodeInvalidParams, "the archive has no manifest.json: it is not an extension")
	}
	files := map[string][]byte{}
	var total int64
	for _, f := range zr.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := path.Clean(strings.ReplaceAll(f.Name, "\\", "/"))
		if path.IsAbs(name) || name == ".." || strings.HasPrefix(name, "../") || strings.Contains(name, ":") {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "the archive names a file outside itself: %q", f.Name)
		}
		if root != "" {
			if !strings.HasPrefix(name, root+"/") {
				continue
			}
			name = strings.TrimPrefix(name, root+"/")
		}
		if f.Mode()&os.ModeSymlink != 0 {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "the archive holds a link (%q); an extension's files are plain files", f.Name)
		}
		total += int64(f.UncompressedSize64)
		if total > MaxExtensionBytes {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "an extension unpacks to at most %d MB", MaxExtensionBytes>>20)
		}
		rc, err := f.Open()
		if err != nil {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "%s: %v", f.Name, err)
		}
		body, err := io.ReadAll(io.LimitReader(rc, MaxExtensionBytes+1))
		rc.Close()
		if err != nil {
			return nil, man, wire.Errorf(wire.CodeInvalidParams, "%s: %v", f.Name, err)
		}
		files[name] = body
	}
	if err := json.Unmarshal(bytes.TrimPrefix(files["manifest.json"], []byte("\xef\xbb\xbf")), &man); err != nil {
		return nil, man, wire.Errorf(wire.CodeInvalidParams, "manifest.json is not JSON: %v", err)
	}
	if man.Name == "" || man.Version == "" {
		return nil, man, wire.Errorf(wire.CodeInvalidParams, "manifest.json needs a name and a version")
	}
	return files, man, nil
}

func writeExtensionFiles(dir string, files map[string][]byte) error {
	for name, body := range files {
		target := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return err
		}
		if err := os.WriteFile(target, body, 0o600); err != nil {
			return err
		}
	}
	return nil
}

// slug is a folder name from an extension's name.
func slug(name string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(name) {
		if b.Len() >= 40 {
			break
		}
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case b.Len() > 0 && !strings.HasSuffix(b.String(), "-"):
			b.WriteByte('-')
		}
	}
	s := strings.Trim(b.String(), "-")
	if s == "" {
		s = "extension"
	}
	return s
}

func uniqueDir(list []Extension, base string) string {
	taken := map[string]bool{}
	for _, e := range list {
		taken[e.Dir] = true
	}
	name := base
	for i := 2; taken[name]; i++ {
		name = fmt.Sprintf("%s-%d", base, i)
	}
	return name
}

func randomSuffix() string {
	raw := make([]byte, 4)
	_, _ = rand.Read(raw)
	return hex.EncodeToString(raw)
}
