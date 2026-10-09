package sidechan

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// BrowseEntry is one folder of an fs.browse answer. Path is the folder as written below the listed
// directory, not its resolution: the picker shows and stores the path the operator walked.
type BrowseEntry struct {
	Name     string    `json:"name"`
	Path     string    `json:"path"`
	Mtime    time.Time `json:"mtime"`
	Writable *bool     `json:"writable"`
	Readable bool      `json:"readable"`
	IsGit    bool      `json:"is_git"`
	Link     bool      `json:"link"`
}

// Place is a starting point the picker offers beside the listing: the home folder and the volumes.
type Place struct {
	Name string `json:"name"`
	Path string `json:"path"`
	Kind string `json:"kind"` // "home" or "volume"
}

// Browse is fs.browse's answer.
type Browse struct {
	Path      string        `json:"path"`
	Parent    string        `json:"parent"`
	Home      string        `json:"home"`
	Writable  *bool         `json:"writable"`
	IsGit     bool          `json:"is_git"`
	Entries   []BrowseEntry `json:"entries"`
	Truncated bool          `json:"truncated"`
	Places    []Place       `json:"places"`
}

// Browse lists the folders in one directory, for the folder picker of a new project.
//
// It is the one read of the side channels that is not held to the roots, and that is on purpose: a
// folder being chosen for a new project is under no root yet — the roots are the folders of the
// projects that already exist — so fs.list's rule would leave the picker nothing to show. What keeps
// it safe instead is what it reads and what it never reads:
//
//   - names and metadata of directories only (modification time, whether this user may write and
//     list it, whether it holds a `.git`); no file is listed and no file's contents are opened;
//   - the directory asked for, as written and as resolved, may be neither one of the daemon's own
//     (its run and state directories) nor on the deny list, and the directory actually opened is
//     checked again by the path the kernel has for it, so a symbolic link swapped in between the
//     check and the open is refused rather than listed;
//   - an entry that is sealed or denied, as written or as resolved, is left out as if absent, and a
//     symbolic link is listed only when it resolves to a directory that passes the same rules;
//   - dot-folders are left out unless hidden is asked for, and the deny list holds either way;
//   - it is bounded: at most maxScan directory entries are read, at most limit are returned, and
//     the per-entry stats stop at config.BrowseBudget, so a slow network mount cannot hold the
//     request; any cut says truncated.
//
// Making a folder and checking the chosen one are fs.mkdir and fs.stat with as_root, which hold the
// path to the rules a project folder is held to; this call adds no write.
func (f *FS) Browse(p string, hidden bool, limit int) (Browse, error) {
	if limit <= 0 {
		limit = config.DefaultBrowse
	}
	if limit > config.MaxBrowse {
		return Browse{}, fmt.Errorf("%w: limit is at most %d", ErrInvalid, config.MaxBrowse)
	}
	p = f.expandHome(p)
	if !filepath.IsAbs(p) {
		return Browse{}, fmt.Errorf("%w: the path must be absolute or start with ~", ErrInvalid)
	}
	clean := filepath.Clean(p)
	real, err := filepath.EvalSymlinks(clean)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			if ferr := f.browsable(clean, clean); ferr != nil {
				return Browse{}, ferr
			}
			return Browse{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		if ferr := f.browsable(clean, clean); ferr != nil {
			return Browse{}, ferr
		}
		return Browse{}, err
	}
	if err := f.browsable(clean, real); err != nil {
		return Browse{}, err
	}
	info, err := os.Stat(real)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Browse{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		return Browse{}, err
	}
	if !info.IsDir() {
		return Browse{}, fmt.Errorf("%w: %s is not a directory", ErrInvalid, clean)
	}
	dir, err := openNoFollow(real, true)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Browse{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		return Browse{}, err
	}
	defer dir.Close()
	if opened, ok := openedPath(dir); ok {
		if err := f.browsable(clean, opened); err != nil {
			return Browse{}, err
		}
	}
	des, err := dir.ReadDir(maxScan)
	if err != nil && !errors.Is(err, io.EOF) {
		return Browse{}, err
	}
	out := Browse{Path: clean, Home: f.home, Writable: writable(real), IsGit: hasGit(real), Entries: []BrowseEntry{}}
	out.Truncated = len(des) == maxScan
	if parent := filepath.Dir(clean); parent != clean {
		out.Parent = parent
	}
	// Sorted before the stats, so a cut by the limit or the budget keeps the first names, not
	// whichever the filesystem happened to return first.
	sort.Slice(des, func(i, j int) bool {
		a, b := strings.ToLower(des[i].Name()), strings.ToLower(des[j].Name())
		if a != b {
			return a < b
		}
		return des[i].Name() < des[j].Name()
	})
	deadline := time.Now().Add(config.BrowseBudget)
	for _, de := range des {
		name := de.Name()
		if !hidden && strings.HasPrefix(name, ".") {
			continue
		}
		written, resolved := filepath.Join(clean, name), filepath.Join(real, name)
		if f.denied(written) || f.denied(resolved) || f.sealedPath(written) || f.sealedPath(resolved) {
			continue
		}
		link := de.Type()&fs.ModeSymlink != 0
		if !link && !de.IsDir() {
			continue
		}
		if !time.Now().Before(deadline) {
			out.Truncated = true
			break
		}
		target := resolved
		if link {
			t, err := filepath.EvalSymlinks(resolved)
			if err != nil || f.denied(t) || f.sealedPath(t) {
				continue
			}
			target = t
		}
		st, err := os.Stat(target)
		if err != nil || !st.IsDir() {
			continue
		}
		if len(out.Entries) >= limit {
			out.Truncated = true
			break
		}
		out.Entries = append(out.Entries, BrowseEntry{Name: name, Path: written, Mtime: st.ModTime().UTC(),
			Writable: writable(target), Readable: readableDir(target), IsGit: hasGit(target), Link: link})
	}
	out.Places = f.places()
	return out, nil
}

// expandHome gives "" and a leading "~" the home folder: the picker opens there, and a path typed
// as ~/projects is what an operator writes.
func (f *FS) expandHome(p string) string {
	switch {
	case p == "" || p == "~":
		return f.home
	case strings.HasPrefix(p, "~/") || (filepath.Separator == '\\' && strings.HasPrefix(p, `~\`)):
		return filepath.Join(f.home, p[2:])
	}
	return p
}

// browsable reports whether a directory may be listed by fs.browse: not the daemon's own and not on
// the deny list, either as written or as resolved. Roots do not enter into it (see Browse).
func (f *FS) browsable(clean, real string) error {
	for _, p := range []string{clean, real} {
		if f.denied(p) || f.sealedPath(p) {
			return fmt.Errorf("%w: %s is the terminal service's own or a denied path", ErrForbidden, clean)
		}
	}
	return nil
}

// places is the home folder and then the volumes, leaving out any the daemon has sealed.
func (f *FS) places() []Place {
	out := []Place{}
	if !f.sealedPath(f.home) && !f.denied(f.home) {
		out = append(out, Place{Name: filepath.Base(f.home), Path: f.home, Kind: "home"})
	}
	for _, v := range volumes() {
		if !f.sealedPath(v.Path) {
			out = append(out, v)
		}
	}
	return out
}

// hasGit reports a `.git` in the folder, a directory or the file a worktree or submodule keeps. It
// runs no git: the picker asks this of every folder it lists.
func hasGit(dir string) bool {
	_, err := os.Lstat(filepath.Join(dir, ".git"))
	return err == nil
}
