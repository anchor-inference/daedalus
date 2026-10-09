package sidechan

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// FS reads files under the allowed roots for the host: transcripts, session stores, a CLI's own
// agent definitions. It writes in one place only: a staff member's inbox (see Write).
//
// A path is allowed when, both as written and as the filesystem resolves it, it lies under a root
// and matches no deny pattern. The check is made again on the file actually opened (its path as the
// kernel reports it), so a symlink swapped in between the check and the open reads nothing it
// should not.
type FS struct {
	mu        sync.RWMutex
	hostRoots []string // from fs.set_roots
	cfgRoots  []string // from the configuration file
	deny      [][]string
	sealed    []string // the daemon's own directories, never readable
	home      string
	following chan struct{}
}

// NewFS returns a reader with the configuration's roots, the default deny list plus the
// configuration's patterns, and the daemon's own directories sealed.
func NewFS(cfgRoots, cfgDeny, sealed []string, home string) (*FS, error) {
	f := &FS{home: filepath.Clean(home), following: make(chan struct{}, config.MaxFollowing)}
	for _, pattern := range append(append([]string{}, DefaultDeny...), cfgDeny...) {
		segs, err := compilePattern(pattern)
		if err != nil {
			return nil, err
		}
		f.deny = append(f.deny, segs)
	}
	for _, root := range cfgRoots {
		f.cfgRoots = append(f.cfgRoots, filepath.Clean(root))
	}
	for _, dir := range sealed {
		dir = filepath.Clean(dir)
		f.sealed = append(f.sealed, dir)
		if real, err := filepath.EvalSymlinks(dir); err == nil && real != dir {
			f.sealed = append(f.sealed, real)
		}
	}
	return f, nil
}

// compilePattern splits a deny pattern into segments. It must be absolute or start with `**/`; a
// relative pattern would match nothing, silently.
func compilePattern(pattern string) ([]string, error) {
	p := filepath.ToSlash(pattern)
	if foldPaths {
		p = strings.ToLower(strings.ReplaceAll(p, `\`, "/"))
	}
	if !strings.HasPrefix(p, "/") && !strings.HasPrefix(p, "**/") {
		return nil, fmt.Errorf("deny pattern %q must be absolute or start with **/", pattern)
	}
	segs := strings.Split(strings.TrimPrefix(p, "/"), "/")
	for _, s := range segs {
		if _, err := path.Match(s, ""); err != nil {
			return nil, fmt.Errorf("deny pattern %q: %w", pattern, err)
		}
	}
	return segs, nil
}

// matchSegments matches path segments against pattern segments, `**` being any number of them.
func matchSegments(pat, segs []string) bool {
	for len(pat) > 0 {
		if pat[0] == "**" {
			for i := 0; i <= len(segs); i++ {
				if matchSegments(pat[1:], segs[i:]) {
					return true
				}
			}
			return false
		}
		if len(segs) == 0 {
			return false
		}
		if ok, _ := path.Match(pat[0], segs[0]); !ok {
			return false
		}
		pat, segs = pat[1:], segs[1:]
	}
	return len(segs) == 0
}

// Refusal is a root fs.set_roots would not take, and why.
type Refusal struct {
	Root   string `json:"root"`
	Reason string `json:"reason"`
}

// SetRoots replaces the host's roots. A root that is not absolute, is the filesystem's root, or holds
// the home directory is refused: the deny list names the credentials it knows of, and a root that
// wide would put every one it does not know of within reach.
func (f *FS) SetRoots(roots []string) (accepted []string, refused []Refusal, err error) {
	if len(roots) > config.MaxRoots {
		return nil, nil, fmt.Errorf("%w: at most %d roots", ErrInvalid, config.MaxRoots)
	}
	seen := map[string]bool{}
	for _, root := range roots {
		if !filepath.IsAbs(root) {
			refused = append(refused, Refusal{root, "not an absolute path"})
			continue
		}
		clean := filepath.Clean(root)
		real := clean
		if r, err := filepath.EvalSymlinks(clean); err == nil {
			real = r
		}
		if reason := f.rootRefusal(clean, real); reason != "" {
			refused = append(refused, Refusal{root, reason})
			continue
		}
		if !seen[clean] {
			seen[clean] = true
			accepted = append(accepted, clean)
		}
	}
	f.mu.Lock()
	f.hostRoots = accepted
	f.mu.Unlock()
	return accepted, refused, nil
}

// rootRefusal says why a path, as written and as resolved, could not be a root; "" when it could.
func (f *FS) rootRefusal(clean, real string) string {
	homes := []string{f.home}
	if r, err := filepath.EvalSymlinks(f.home); err == nil {
		homes = append(homes, r)
	}
	if isVolumeRoot(clean) || isVolumeRoot(real) {
		return "is the root of the filesystem"
	}
	reason := ""
	for _, p := range []string{clean, real} {
		for _, home := range homes {
			if under(p, home) {
				reason = "holds the home directory"
			}
		}
		if f.sealedPath(p) || f.denied(p) {
			reason = "is the terminal service's own or a denied path"
		}
	}
	return reason
}

// isVolumeRoot reports "/" or, on Windows, a drive's or a share's root (`C:\`, `\\host\share\`).
func isVolumeRoot(p string) bool {
	return p == "/" || filepath.Dir(p) == p
}

// checkRoot resolves a path the host is about to make a root and holds it to the rules a root is
// held to. It returns the cleaned path, its resolution (that of the deepest existing ancestor with
// the rest appended, when it is missing) and whether it exists.
func (f *FS) checkRoot(p string) (clean, real string, exists bool, err error) {
	if !filepath.IsAbs(p) {
		return "", "", false, fmt.Errorf("%w: the path must be absolute", ErrInvalid)
	}
	clean = filepath.Clean(p)
	real, err = filepath.EvalSymlinks(clean)
	exists = err == nil
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			return "", "", false, err
		}
		real = resolveMissing(clean)
	}
	if reason := f.rootRefusal(clean, real); reason != "" {
		return "", "", false, fmt.Errorf("%w: %s cannot be a folder of a project: it %s", ErrForbidden, clean, reason)
	}
	return clean, real, exists, nil
}

// StatRoot describes a path that is to become a root — a project folder being added — and so is
// not under one yet. It answers only what a folder check needs (whether it is there, whether it is a
// directory, whether this user may write in it), for any path that could be a root; a path that
// could not is refused as SetRoots would refuse it.
func (f *FS) StatRoot(p string) (Stat, error) {
	_, real, exists, err := f.checkRoot(p)
	if err != nil || !exists {
		return Stat{}, err
	}
	st, err := os.Stat(real)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Stat{}, nil
		}
		return Stat{}, err
	}
	out := statOf(st)
	out.Writable = writable(real)
	return out, nil
}

// MkdirRoot creates a directory that is to become a root, with its missing parents, as this user
// and under this user's umask. An existing directory is left as it is (created is false). This is
// the side channels' only write: a project folder on the host has to exist before anything, a
// terminal included, can be started in it.
func (f *FS) MkdirRoot(p string) (st Stat, created bool, err error) {
	clean, real, exists, err := f.checkRoot(p)
	if err != nil {
		return Stat{}, false, err
	}
	if exists {
		info, err := os.Stat(real)
		if err != nil {
			return Stat{}, false, err
		}
		if !info.IsDir() {
			return Stat{}, false, fmt.Errorf("%w: %s exists and is not a directory", ErrInvalid, clean)
		}
		out := statOf(info)
		out.Writable = writable(real)
		return out, false, nil
	}
	if err := os.MkdirAll(clean, 0o777); err != nil {
		return Stat{}, false, err
	}
	// Checked again as made: a parent swapped for a symlink between the check and the MkdirAll
	// would have put the directory somewhere a root may not be, and the host must not take it.
	if _, _, _, err := f.checkRoot(clean); err != nil {
		return Stat{}, true, err
	}
	st, err = f.StatRoot(clean)
	return st, true, err
}

// Roots is every root in force: the host's and the configuration's.
func (f *FS) Roots() []string {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return append(append([]string{}, f.hostRoots...), f.cfgRoots...)
}

// under reports whether p is dir or inside it.
func under(dir, p string) bool { return underPath(dir, p, filepath.Separator, foldPaths) }

// foldPaths says that a path names the same file whatever its case, as on Windows. There every
// comparison of paths with roots, the sealed directories and the deny list is made in lower case:
// otherwise ".SSH\id_ed25519" would pass a rule written for ".ssh".
var foldPaths = runtime.GOOS == "windows"

// underPath is under with the separator and the case rule given, so the Windows rules are tested
// everywhere. A directory that ends in its separator is a volume's root ("C:\" or "/").
func underPath(dir, p string, sep byte, fold bool) bool {
	if fold {
		dir, p = strings.ToLower(dir), strings.ToLower(p)
	}
	if dir == "/" || (len(dir) > 0 && dir[len(dir)-1] == sep) {
		return strings.HasPrefix(p, dir)
	}
	return p == dir || strings.HasPrefix(p, dir+string(sep))
}

func (f *FS) denied(p string) bool {
	p = filepath.ToSlash(p)
	if foldPaths {
		// A Windows path, spelled with either separator, in lower case.
		p = strings.ToLower(strings.ReplaceAll(p, `\`, "/"))
	}
	segs := strings.Split(strings.TrimPrefix(p, "/"), "/")
	for _, pat := range f.deny {
		if matchSegments(pat, segs) {
			return true
		}
	}
	return false
}

// Refused reports a path no read may open, as written and as the filesystem resolves it: one on the
// deny list or inside the daemon's own directories. The session reader (sessions.*) reads outside
// the roots, from each program's fixed store, and holds every file it opens to this.
func (f *FS) Refused(p string) bool {
	if f.denied(p) || f.sealedPath(p) {
		return true
	}
	if real, err := filepath.EvalSymlinks(p); err == nil && real != p {
		return f.denied(real) || f.sealedPath(real)
	}
	return false
}

func (f *FS) sealedPath(p string) bool {
	for _, dir := range f.sealed {
		if under(dir, p) {
			return true
		}
	}
	return false
}

// allowed reports whether a cleaned path and its resolution may be read.
func (f *FS) allowed(clean, real string) error {
	for _, p := range []string{clean, real} {
		if f.denied(p) || f.sealedPath(p) {
			return fmt.Errorf("%w: %s is on the deny list", ErrForbidden, clean)
		}
	}
	for _, root := range f.Roots() {
		rootReal := root
		if r, err := filepath.EvalSymlinks(root); err == nil {
			rootReal = r
		}
		if under(root, clean) && under(rootReal, real) {
			return nil
		}
	}
	return fmt.Errorf("%w: %s is not under an allowed root", ErrForbidden, clean)
}

// resolve checks a requested path. It returns the cleaned path and its resolution; exists is false
// when the file is missing, and then the resolution is that of its nearest existing parent with the
// rest appended — so asking about a missing file tells nothing about places outside the roots.
func (f *FS) resolve(p string) (clean, real string, exists bool, err error) {
	if !filepath.IsAbs(p) {
		return "", "", false, fmt.Errorf("%w: the path must be absolute", ErrInvalid)
	}
	clean = filepath.Clean(p)
	real, err = filepath.EvalSymlinks(clean)
	exists = err == nil
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			if aerr := f.allowed(clean, clean); aerr != nil {
				return "", "", false, aerr
			}
			return "", "", false, err
		}
		real = resolveMissing(clean)
	}
	if err := f.allowed(clean, real); err != nil {
		return "", "", false, err
	}
	return clean, real, exists, nil
}

// resolveMissing resolves the deepest existing ancestor of p and appends the rest.
func resolveMissing(p string) string {
	rest := []string{}
	for dir := p; ; {
		parent := filepath.Dir(dir)
		rest = append([]string{filepath.Base(dir)}, rest...)
		if parent == dir {
			return p
		}
		if real, err := filepath.EvalSymlinks(parent); err == nil {
			return filepath.Join(append([]string{real}, rest...)...)
		}
		dir = parent
	}
}

// open opens a checked path and checks the opened file again, by the path the kernel has for it.
func (f *FS) open(clean, real string, dir bool) (*os.File, os.FileInfo, error) {
	file, err := openNoFollow(real, dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		return nil, nil, err
	}
	if opened, ok := openedPath(file); ok {
		if err := f.allowed(clean, opened); err != nil {
			file.Close()
			return nil, nil, err
		}
	}
	st, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, nil, err
	}
	if dir != st.IsDir() || (!dir && !st.Mode().IsRegular()) {
		file.Close()
		want := "a regular file"
		if dir {
			want = "a directory"
		}
		return nil, nil, fmt.Errorf("%w: %s is not %s", ErrInvalid, clean, want)
	}
	return file, st, nil
}

// Stat is fs.stat.
type Stat struct {
	Exists bool      `json:"exists"`
	Type   string    `json:"type,omitempty"`
	Size   int64     `json:"size"`
	Mtime  time.Time `json:"mtime,omitzero"`
	Mode   string    `json:"mode,omitempty"`
	FileID string    `json:"file_id,omitempty"`
	// Writable is whether this user may create files in it; only a folder check asks.
	Writable *bool `json:"writable,omitempty"`
}

func statOf(st os.FileInfo) Stat {
	return Stat{Exists: true, Type: typeOf(st.Mode()), Size: st.Size(), Mtime: st.ModTime().UTC(),
		Mode: fmt.Sprintf("%04o", st.Mode().Perm()), FileID: fileID(st)}
}

func typeOf(m fs.FileMode) string {
	switch {
	case m.IsRegular():
		return "file"
	case m.IsDir():
		return "dir"
	case m&fs.ModeSymlink != 0:
		return "symlink"
	}
	return "other"
}

// Stat describes a path; a missing one under a root is {exists: false}.
func (f *FS) Stat(p string) (Stat, error) {
	_, real, exists, err := f.resolve(p)
	if err != nil {
		return Stat{}, err
	}
	if !exists {
		return Stat{}, nil
	}
	st, err := os.Stat(real)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Stat{}, nil
		}
		return Stat{}, err
	}
	return statOf(st), nil
}

// Entry is one fs.list entry. A symlink is listed as one, not followed.
type Entry struct {
	Name  string    `json:"name"`
	Type  string    `json:"type"`
	Size  int64     `json:"size"`
	Mtime time.Time `json:"mtime"`
}

// maxScan bounds the entries read from one directory, whatever the limit of the listing: sorting by
// mtime needs them all, and a directory of a million files must not cost a million stats.
const maxScan = 100000

// List lists a directory, filtered by a glob on the name, sorted by name or by mtime (newest first),
// at most limit entries. Entries on the deny list are left out.
func (f *FS) List(p, glob, sortBy string, limit int) (entries []Entry, truncated bool, err error) {
	if limit <= 0 {
		limit = config.DefaultFSList
	}
	if limit > config.MaxFSList {
		return nil, false, fmt.Errorf("%w: limit is at most %d", ErrInvalid, config.MaxFSList)
	}
	if sortBy == "" {
		sortBy = "name"
	}
	if sortBy != "name" && sortBy != "mtime" {
		return nil, false, fmt.Errorf("%w: sort is name or mtime", ErrInvalid)
	}
	if glob != "" {
		if _, err := path.Match(glob, ""); err != nil {
			return nil, false, fmt.Errorf("%w: glob: %v", ErrInvalid, err)
		}
	}
	clean, real, exists, err := f.resolve(p)
	if err != nil {
		return nil, false, err
	}
	if !exists {
		return nil, false, fmt.Errorf("%w: %s", ErrNotFound, clean)
	}
	dir, _, err := f.open(clean, real, true)
	if err != nil {
		return nil, false, err
	}
	defer dir.Close()
	des, err := dir.ReadDir(maxScan)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, false, err
	}
	truncated = len(des) == maxScan
	for _, de := range des {
		name := de.Name()
		if glob != "" {
			if ok, _ := path.Match(glob, name); !ok {
				continue
			}
		}
		full := filepath.Join(real, name)
		if f.denied(full) || f.denied(filepath.Join(clean, name)) || f.sealedPath(full) {
			continue
		}
		info, err := de.Info()
		if err != nil {
			continue // removed while listing
		}
		entries = append(entries, Entry{Name: name, Type: typeOf(info.Mode()), Size: info.Size(), Mtime: info.ModTime().UTC()})
	}
	if sortBy == "mtime" {
		sort.SliceStable(entries, func(i, j int) bool {
			if !entries[i].Mtime.Equal(entries[j].Mtime) {
				return entries[i].Mtime.After(entries[j].Mtime)
			}
			return entries[i].Name < entries[j].Name
		})
	} else {
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name < entries[j].Name })
	}
	if len(entries) > limit {
		entries, truncated = entries[:limit], true
	}
	return entries, truncated, nil
}

// Read is fs.read's result.
type Read struct {
	Data   []byte `json:"data_b64"`
	Offset int64  `json:"offset"`
	Size   int64  `json:"size"`
	EOF    bool   `json:"eof"`
	FileID string `json:"file_id,omitempty"`
}

// clampMax is how much one read may return: what was asked, within what one reply carries.
func clampMax(max int) (int, error) {
	if max < 0 || max > config.MaxFSRead {
		return 0, fmt.Errorf("%w: max is 1..%d", ErrInvalid, config.MaxFSRead)
	}
	if max == 0 {
		max = 64 << 10
	}
	return min(max, config.MaxReplyData), nil
}

// Read reads up to max bytes at offset. EOF is true when the read reached the end of the file; a
// caller that asked for more than one reply carries continues at Offset+len(Data).
func (f *FS) Read(p string, offset int64, max int) (Read, error) {
	n, err := clampMax(max)
	if err != nil {
		return Read{}, err
	}
	if offset < 0 {
		return Read{}, fmt.Errorf("%w: offset must not be negative", ErrInvalid)
	}
	clean, real, exists, err := f.resolve(p)
	if err != nil {
		return Read{}, err
	}
	if !exists {
		return Read{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
	}
	file, st, err := f.open(clean, real, false)
	if err != nil {
		return Read{}, err
	}
	defer file.Close()
	data, err := readAt(file, offset, n)
	if err != nil {
		return Read{}, err
	}
	return Read{Data: data, Offset: offset, Size: st.Size(), EOF: offset+int64(len(data)) >= st.Size(), FileID: fileID(st)}, nil
}

func readAt(file *os.File, offset int64, n int) ([]byte, error) {
	buf := make([]byte, n)
	got, err := file.ReadAt(buf, offset)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	return buf[:got], nil
}

// Tail is fs.tail's result. Rotated means the file at the path is not the one the caller was
// reading (another inode), or it shrank; the data then starts at its beginning.
type Tail struct {
	Data       []byte `json:"data_b64"`
	NextOffset int64  `json:"next_offset"`
	Size       int64  `json:"size"`
	Rotated    bool   `json:"rotated"`
	FileID     string `json:"file_id,omitempty"`
}

// Tail returns what the file holds past from, waiting up to follow for something to arrive. fileID
// is the one a previous read or tail returned, so a replaced file is noticed even when the new one
// has already grown past the old offset.
func (f *FS) Tail(ctx context.Context, p string, from int64, max int, follow time.Duration, fileIDWas string) (Tail, error) {
	n, err := clampMax(max)
	if err != nil {
		return Tail{}, err
	}
	if from < 0 {
		return Tail{}, fmt.Errorf("%w: from_offset must not be negative", ErrInvalid)
	}
	if follow < 0 || follow > config.MaxTailFollow {
		return Tail{}, fmt.Errorf("%w: follow_ms is 0..%d", ErrInvalid, config.MaxTailFollow.Milliseconds())
	}
	if follow > 0 {
		select {
		case f.following <- struct{}{}:
			defer func() { <-f.following }()
		default:
			return Tail{}, fmt.Errorf("%w: %d tails are waiting already", ErrBusy, config.MaxFollowing)
		}
	}
	deadline := time.Now().Add(follow)
	for {
		t, done, err := f.tailOnce(p, from, n, fileIDWas)
		if err != nil || done || !time.Now().Before(deadline) {
			return t, err
		}
		select {
		case <-ctx.Done():
			return t, nil
		case <-time.After(config.TailPoll):
		}
	}
}

// tailOnce looks once. done is true when there is something to return: data, or a rotation.
func (f *FS) tailOnce(p string, from int64, n int, fileIDWas string) (Tail, bool, error) {
	clean, real, exists, err := f.resolve(p)
	if err != nil {
		return Tail{}, false, err
	}
	if !exists {
		return Tail{}, false, fmt.Errorf("%w: %s", ErrNotFound, clean)
	}
	file, st, err := f.open(clean, real, false)
	if err != nil {
		return Tail{}, false, err
	}
	defer file.Close()
	id := fileID(st)
	t := Tail{NextOffset: from, Size: st.Size(), FileID: id}
	if (fileIDWas != "" && id != "" && id != fileIDWas) || st.Size() < from {
		t.Rotated, from, t.NextOffset = true, 0, 0
	}
	if st.Size() > from {
		data, err := readAt(file, from, int(min(int64(n), st.Size()-from)))
		if err != nil {
			return Tail{}, false, err
		}
		t.Data, t.NextOffset = data, from+int64(len(data))
	}
	return t, len(t.Data) > 0 || t.Rotated, nil
}

// Written is fs.write's answer: the file's size after the write, and whether this call made it.
type Written struct {
	Size    int64 `json:"size"`
	Created bool  `json:"created"`
}

// ErrExists is a file fs.write was asked to create that is there already. The host looks before it
// writes and picks another name; a file is never overwritten, because what a member was given must
// not change under it.
var ErrExists = fmt.Errorf("%w: the file exists", ErrForbidden)

// inboxTarget reports whether a path is one fs.write may write: below the last `.agents` of the
// path, `inbox/<...>/<name>` — a file handed to a staff member, or the inbox's own `.gitignore`,
// which keeps it out of a repository whose exclude file the host cannot reach.
func inboxTarget(p string) bool {
	segs := strings.Split(filepath.ToSlash(p), "/")
	last := -1
	for i, s := range segs {
		if s == ".agents" || (foldPaths && strings.EqualFold(s, ".agents")) {
			last = i
		}
	}
	if last < 1 {
		return false
	}
	rest := segs[last+1:]
	return len(rest) >= 2 && rest[0] == "inbox" && rest[len(rest)-1] != ""
}

// Write writes data at offset into a file of a staff member's inbox: the side channels' only file
// write, because a member working on the host can open nothing the agent's container holds.
//
// The path is held to every rule a read is — under a root as written and as resolved, nothing
// denied, nothing of the daemon's own — and must also be an inbox target (see inboxTarget), so a
// root is not thereby writable anywhere else. Offset 0 creates the file and refuses one that is
// there (ErrExists); a later offset continues a regular file that holds exactly offset bytes, which
// is how the host sends a file larger than one frame. The missing directories are made, and the
// directory and the opened file are checked again afterwards: an `.agents` swapped for a symbolic
// link to elsewhere is refused, not written through.
func (f *FS) Write(p string, offset int64, data []byte) (Written, error) {
	if len(data) > config.MaxFSWrite {
		return Written{}, fmt.Errorf("%w: one write carries at most %d bytes", ErrInvalid, config.MaxFSWrite)
	}
	if offset < 0 || offset+int64(len(data)) > config.MaxFSWriteFile {
		return Written{}, fmt.Errorf("%w: a file written here is at most %d bytes", ErrInvalid, config.MaxFSWriteFile)
	}
	if !filepath.IsAbs(p) {
		return Written{}, fmt.Errorf("%w: the path must be absolute", ErrInvalid)
	}
	clean := filepath.Clean(p)
	if !inboxTarget(clean) {
		return Written{}, fmt.Errorf("%w: %s is not in a staff inbox (<folder>/.agents/inbox/...)", ErrForbidden, clean)
	}
	dir, name := filepath.Dir(clean), filepath.Base(clean)
	if _, _, _, err := f.resolve(dir); err != nil {
		return Written{}, err
	}
	if err := os.MkdirAll(dir, 0o777); err != nil {
		return Written{}, err
	}
	dirClean, dirReal, exists, err := f.resolve(dir)
	if err != nil {
		return Written{}, err
	}
	if !exists {
		return Written{}, fmt.Errorf("%w: %s", ErrNotFound, dirClean)
	}
	target := filepath.Join(dirReal, name)
	if !inboxTarget(target) {
		return Written{}, fmt.Errorf("%w: %s resolves outside a staff inbox", ErrForbidden, clean)
	}
	if err := f.allowed(clean, target); err != nil {
		return Written{}, err
	}
	file, err := openForWrite(target, offset == 0)
	if err != nil {
		if errors.Is(err, fs.ErrExist) {
			return Written{}, fmt.Errorf("%w: %s", ErrExists, clean)
		}
		if errors.Is(err, fs.ErrNotExist) {
			return Written{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		return Written{}, err
	}
	defer file.Close()
	if opened, ok := openedPath(file); ok {
		if err := f.allowed(clean, opened); err != nil {
			return Written{}, err
		}
		if !inboxTarget(opened) {
			return Written{}, fmt.Errorf("%w: %s resolves outside a staff inbox", ErrForbidden, clean)
		}
	}
	st, err := file.Stat()
	if err != nil {
		return Written{}, err
	}
	if !st.Mode().IsRegular() {
		return Written{}, fmt.Errorf("%w: %s is not a regular file", ErrInvalid, clean)
	}
	if offset > 0 && st.Size() != offset {
		return Written{}, fmt.Errorf("%w: %s holds %d bytes, not %d", ErrInvalid, clean, st.Size(), offset)
	}
	if _, err := file.WriteAt(data, offset); err != nil {
		return Written{}, err
	}
	return Written{Size: offset + int64(len(data)), Created: offset == 0}, nil
}
