package sessions

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Errors the RPC layer maps to protocol codes.
var (
	ErrInvalid   = errors.New("invalid request")
	ErrForbidden = errors.New("forbidden")
	ErrNotFound  = errors.New("not found")
	ErrBusy      = errors.New("too many at once")
)

// Env is the machine the parsers read: the home folder, the environment, the system, and the
// deny list. Tests give each one its own.
type Env struct {
	Home   string
	Getenv func(string) string
	GOOS   string
	// Refused reports a path no read may open: the side channels' deny list and the daemon's own
	// directories. Nil refuses nothing (tests that do not exercise it).
	Refused func(path string) bool
	Now     func() time.Time
	// ProcRoot is where the process table is read for the live check ("/proc"); empty skips it.
	ProcRoot string
}

// NewEnv is the environment of the running daemon.
func NewEnv(home string, refused func(string) bool) *Env {
	return &Env{Home: filepath.Clean(home), Getenv: os.Getenv, GOOS: runtime.GOOS, Refused: refused, Now: time.Now, ProcRoot: "/proc"}
}

func (e *Env) getenv(k string) string {
	if e.Getenv == nil {
		return ""
	}
	return e.Getenv(k)
}

func (e *Env) now() time.Time {
	if e.Now == nil {
		return time.Now()
	}
	return e.Now()
}

// fold says that paths name the same file whatever their case: Windows, and macOS, whose default
// volume format is case-insensitive. Comparing in lower case there is what keeps a session written
// as /Users/Someone/Proj in the folder the operator browses as /Users/someone/proj.
func (e *Env) fold() bool { return e.GOOS == "windows" || e.GOOS == "darwin" }

// samePath compares two normalised paths under the system's case rule.
func (e *Env) samePath(a, b string) bool {
	if e.fold() {
		return strings.EqualFold(a, b)
	}
	return a == b
}

// within reports whether p is dir or below it, both normalised with forward slashes.
func (e *Env) within(dir, p string) bool {
	if e.fold() {
		dir, p = strings.ToLower(dir), strings.ToLower(p)
	}
	if dir == "" {
		return true
	}
	if strings.HasSuffix(dir, "/") {
		return strings.HasPrefix(p, dir)
	}
	return p == dir || strings.HasPrefix(p, dir+"/")
}

// NormPath is a folder as the listing compares it: forward slashes, no trailing slash, and on
// Windows the drive letter in upper case ("c:\Users\x\" → "C:/Users/x"). It is applied to the cwd a
// program recorded and to the path the host asks about alike.
func NormPath(p string) string {
	if p == "" {
		return ""
	}
	p = strings.ReplaceAll(p, `\`, "/")
	unc := strings.HasPrefix(p, "//")
	if len(p) >= 2 && p[1] == ':' && isLetter(p[0]) {
		p = strings.ToUpper(p[:1]) + p[1:]
		rest := path.Clean("/" + strings.TrimPrefix(p[2:], "/"))
		return p[:2] + rest
	}
	p = path.Clean(p)
	if unc && !strings.HasPrefix(p, "//") {
		p = "/" + p
	}
	return p
}

// expand gives "~" and "~/…" the home folder.
func (e *Env) expand(p string) string {
	switch {
	case p == "~":
		return e.Home
	case strings.HasPrefix(p, "~/") || strings.HasPrefix(p, `~\`):
		return filepath.Join(e.Home, p[2:])
	}
	return p
}

// display writes a root with the home folder as "~", as the app shows it.
func (e *Env) display(p string) string {
	h := NormPath(e.Home)
	n := NormPath(p)
	if e.within(h, n) {
		return "~" + n[len(h):]
	}
	return n
}

// refused applies the deny list to a path as written and as resolved.
func (e *Env) refused(paths ...string) bool {
	if e.Refused == nil {
		return false
	}
	for _, p := range paths {
		if e.Refused(p) {
			return true
		}
	}
	return false
}

// open opens a session file for reading. The file must be a regular file below root, both as
// written and as the filesystem resolves it, and on neither side on the deny list: the roots are
// the only folders sessions.* reads, and a link inside one that points at a login is refused.
func (e *Env) open(root, p string) (*os.File, os.FileInfo, error) {
	clean := filepath.Clean(p)
	if !filepath.IsAbs(clean) {
		return nil, nil, fmt.Errorf("%w: %s is not absolute", ErrInvalid, p)
	}
	real, err := filepath.EvalSymlinks(clean)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil, fmt.Errorf("%w: %s", ErrNotFound, clean)
		}
		return nil, nil, err
	}
	rootReal := root
	if r, err := filepath.EvalSymlinks(root); err == nil {
		rootReal = r
	}
	if !e.within(NormPath(root), NormPath(clean)) || !e.within(NormPath(rootReal), NormPath(real)) {
		return nil, nil, fmt.Errorf("%w: %s is outside the session store", ErrForbidden, clean)
	}
	if e.refused(clean, real) {
		return nil, nil, fmt.Errorf("%w: %s is on the deny list", ErrForbidden, clean)
	}
	f, err := os.Open(real)
	if err != nil {
		return nil, nil, err
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		f.Close()
		return nil, nil, fmt.Errorf("%w: %s is not a regular file", ErrInvalid, clean)
	}
	return f, st, nil
}

// readHead reads up to n bytes from the start of a file; readTail the last n bytes, starting at a
// record's beginning (the partial first line is dropped).
func readHead(f *os.File, n int64) []byte {
	buf := make([]byte, n)
	m, _ := f.ReadAt(buf, 0)
	return buf[:m]
}

func readTail(f *os.File, size, n int64) []byte {
	if size <= n {
		b := readHead(f, size)
		return b
	}
	buf := make([]byte, n)
	m, err := f.ReadAt(buf, size-n)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil
	}
	buf = buf[:m]
	if i := indexByte(buf, '\n'); i >= 0 {
		return buf[i+1:]
	}
	return nil
}

func indexByte(b []byte, c byte) int {
	for i, x := range b {
		if x == c {
			return i
		}
	}
	return -1
}

// lines splits a buffer into its complete records. When the buffer is a file's head, the last
// line may be cut; complete says whether the buffer ends the file.
func lines(buf []byte, complete bool) [][]byte {
	var out [][]byte
	for len(buf) > 0 {
		i := indexByte(buf, '\n')
		if i < 0 {
			if complete {
				out = append(out, buf)
			}
			break
		}
		if line := buf[:i]; len(line) > 0 {
			out = append(out, line)
		}
		buf = buf[i+1:]
	}
	return out
}

// isDir reports an existing directory (following links: a root may be a link the operator made).
func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}
