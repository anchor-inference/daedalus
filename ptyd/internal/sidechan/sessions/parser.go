// Package sessions reads the sessions other agent programs (Claude Code, Codex, …) keep on this
// machine and gives them back in one normalised model, for the host to import: sessions.harnesses,
// sessions.scan and sessions.read.
//
// It reads and never writes, and it runs nothing. What it may read is a fixed list of storage
// roots per program inside the home folder; every file it opens is checked, as written and as
// resolved, against that root and against the side channels' deny list, so a symbolic link planted
// in a session folder cannot make it read a login or a key. Every text it returns has been through
// the secret shapes first (redact.go).
//
// A program is one Parser. Adding one is a file with a Parser and a line in Parsers; everything
// else — the listing, the caches, the paging, the masking, the live check — is shared.
package sessions

import (
	"time"
)

// Parser is one program's storage format.
type Parser interface {
	// ID is the program's id on the wire ("claude", "codex"); Name is what the app shows.
	ID() string
	Name() string
	// Procs are the process names that write this program's sessions, for the live check.
	Procs() []string
	// Roots are where this program keeps its sessions on this machine, existing or not, in the
	// order they are looked at. They follow the program's own override variable.
	Roots(e *Env) []string
	// Discover lists the sessions under one root from directory entries alone: no file is opened.
	Discover(e *Env, root string, emit func(Candidate)) error
	// Peek reads the least of a session that the listing needs: its folder, its times and, when it
	// is near the start or the end, its title. It reads the head and the tail of the file, never
	// the whole of it.
	Peek(e *Env, c *Candidate) (Header, error)
	// Index passes over every record once and returns what each turn is made of (by offset), the
	// full header with its counts, and the bytes it covered. Nothing of a record's content is
	// kept, so an index of a session of hundreds of megabytes is small.
	Index(e *Env, c *Candidate, sidechains bool) (*Index, error)
	// Build makes the parts of one turn from the records the index named for it.
	Build(e *Env, idx *Index, ref *TurnRef, records [][]byte) []Part
}

// Candidate is one session found on disk.
type Candidate struct {
	Harness string
	ID      string
	Root    string
	Path    string   // the session's main file
	Extra   []string // its other files (Claude Code's sub-agents), in a stable order
	Size    int64    // of every file together
	Mtime   time.Time
	// Children are other sessions that are this one's sub-agents (a Codex thread a spawn started),
	// filled from their headers before Index is called.
	Children []Candidate
}

// signature identifies a candidate's state on disk: a change of any file changes it.
func (c *Candidate) signature() string {
	return c.Path + "\x00" + itoa(c.Size) + "\x00" + itoa(c.Mtime.UnixNano()) + "\x00" + itoa(int64(len(c.Extra)+len(c.Children)))
}

// Span is one record, by file and offset. File 0 is the candidate's Path, then its Extra in order,
// then the Children's paths.
type Span struct {
	File int
	Off  int64
	Len  int
}

// TurnRef is one turn of an index: everything about it but its parts, and where its records are.
type TurnRef struct {
	ExtID     string
	Parent    string
	Role      string
	Sidechain string
	Model     string
	At        time.Time
	Usage     *Usage
	Spans     []Span
	// Aux is the parser's own note for Build (what a record alone does not say).
	Aux any
}

// Index is a session's turns by reference, and its full header.
type Index struct {
	Files    []string
	Turns    []TurnRef
	Header   Header
	Consumed int64 // bytes of the main file the index covers: where a later read of new records starts
	Damaged  int   // records that did not parse and were skipped
	Aux      any
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
