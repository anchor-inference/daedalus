//go:build unix

package sidechan

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// tree is a home with a project root, a CLI's credentials and transcripts, and a place outside.
type tree struct {
	home, project, transcripts, outside, state string
	fs                                         *FS
}

func newTree(t *testing.T) *tree {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	tr := &tree{home: filepath.Join(base, "home"), outside: filepath.Join(base, "outside"), state: filepath.Join(base, "state")}
	tr.project = filepath.Join(tr.home, "work", "site")
	tr.transcripts = filepath.Join(tr.home, ".claude", "projects")
	for _, d := range []string{tr.project, tr.transcripts, tr.outside, tr.state, filepath.Join(tr.home, ".ssh")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	write(t, filepath.Join(tr.home, ".claude", ".credentials.json"), `{"token":"secret"}`)
	write(t, filepath.Join(tr.home, ".ssh", "id_ed25519"), "key")
	write(t, filepath.Join(tr.outside, "secret.txt"), "outside")
	write(t, filepath.Join(tr.project, "README.md"), "hello")
	write(t, filepath.Join(tr.state, "token"), "daemon token")
	tr.fs, err = NewFS(nil, nil, []string{tr.state}, tr.home)
	if err != nil {
		t.Fatal(err)
	}
	if _, refused, err := tr.fs.SetRoots([]string{tr.project, tr.transcripts}); err != nil || len(refused) != 0 {
		t.Fatalf("%v %v", refused, err)
	}
	return tr
}

func write(t *testing.T, p, body string) {
	t.Helper()
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestReadUnderARoot(t *testing.T) {
	tr := newTree(t)
	r, err := tr.fs.Read(filepath.Join(tr.project, "README.md"), 0, 0)
	if err != nil || string(r.Data) != "hello" || !r.EOF || r.Size != 5 {
		t.Fatalf("%+v %v", r, err)
	}
	r, err = tr.fs.Read(filepath.Join(tr.project, "README.md"), 2, 2)
	if err != nil || string(r.Data) != "ll" || r.EOF {
		t.Fatalf("%+v %v", r, err)
	}
	if _, err := tr.fs.Read(filepath.Join(tr.project, "missing"), 0, 0); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing: %v", err)
	}
	if _, err := tr.fs.Read("relative/path", 0, 0); !errors.Is(err, ErrInvalid) {
		t.Fatalf("relative: %v", err)
	}
	if _, err := tr.fs.Read(tr.project, 0, 0); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a directory read as a file: %v", err)
	}
}

func TestCredentialsAreRefusedEvenUnderARoot(t *testing.T) {
	tr := newTree(t)
	// Widen the roots to the CLI's whole directory: its login is still refused.
	if _, refused, _ := tr.fs.SetRoots([]string{filepath.Join(tr.home, ".claude"), filepath.Join(tr.home, ".ssh")}); len(refused) != 1 {
		t.Fatalf("refused %v; .ssh itself is on the deny list", refused)
	}
	for _, p := range []string{
		filepath.Join(tr.home, ".claude", ".credentials.json"),
		filepath.Join(tr.home, ".claude", "projects", "..", ".credentials.json"),
	} {
		if _, err := tr.fs.Read(p, 0, 0); !errors.Is(err, ErrForbidden) {
			t.Errorf("%s: %v", p, err)
		}
		if _, err := tr.fs.Stat(p); !errors.Is(err, ErrForbidden) {
			t.Errorf("stat %s: %v", p, err)
		}
	}
	entries, _, err := tr.fs.List(filepath.Join(tr.home, ".claude"), "", "", 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Name == ".credentials.json" {
			t.Fatal("a denied file is listed")
		}
	}
}

func TestSymlinksOutOfARootAreRefused(t *testing.T) {
	tr := newTree(t)
	link := filepath.Join(tr.project, "escape")
	if err := os.Symlink(filepath.Join(tr.outside, "secret.txt"), link); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Read(link, 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("file link: %v", err)
	}
	dirLink := filepath.Join(tr.project, "out")
	if err := os.Symlink(tr.outside, dirLink); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Read(filepath.Join(dirLink, "secret.txt"), 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("directory link: %v", err)
	}
	if _, _, err := tr.fs.List(dirLink, "", "", 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("list through a link: %v", err)
	}
	// A link to the credentials, from inside a root, is refused by the deny list.
	credLink := filepath.Join(tr.project, "creds")
	if err := os.Symlink(filepath.Join(tr.home, ".claude", ".credentials.json"), credLink); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Read(credLink, 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("credentials link: %v", err)
	}
	// Outside any root, by plain path and by dot-dot.
	for _, p := range []string{filepath.Join(tr.outside, "secret.txt"), filepath.Join(tr.project, "..", "..", "..", "outside", "secret.txt")} {
		if _, err := tr.fs.Read(p, 0, 0); !errors.Is(err, ErrForbidden) {
			t.Errorf("%s: %v", p, err)
		}
	}
	// A missing file outside the roots says nothing about whether it exists.
	if _, err := tr.fs.Stat(filepath.Join(tr.outside, "nothing")); !errors.Is(err, ErrForbidden) {
		t.Fatalf("stat outside: %v", err)
	}
	if st, err := tr.fs.Stat(filepath.Join(tr.project, "nothing")); err != nil || st.Exists {
		t.Fatalf("stat inside: %+v %v", st, err)
	}
}

// A directory swapped for a symlink after the path was checked is caught on the file actually
// opened: the last component is opened without following links, and the kernel's path of the open
// file is checked again.
func TestOpenedFileIsCheckedAgain(t *testing.T) {
	tr := newTree(t)
	sub := filepath.Join(tr.project, "sub")
	if err := os.Mkdir(sub, 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(sub, "secret.txt"), "fine")
	clean, real, _, err := tr.fs.resolve(filepath.Join(sub, "secret.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(sub); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(tr.outside, sub); err != nil {
		t.Fatal(err)
	}
	f, _, err := tr.fs.open(clean, real, false)
	if err == nil {
		f.Close()
		t.Fatal("a file reached through a swapped-in link was opened")
	}
	if !errors.Is(err, ErrForbidden) {
		t.Fatalf("%v", err)
	}
}

func TestSetRootsRefusesWideRoots(t *testing.T) {
	tr := newTree(t)
	accepted, refused, err := tr.fs.SetRoots([]string{"/", tr.home, filepath.Dir(tr.home), "relative", tr.state, tr.project})
	if err != nil {
		t.Fatal(err)
	}
	if len(accepted) != 1 || accepted[0] != tr.project || len(refused) != 5 {
		t.Fatalf("accepted %v refused %v", accepted, refused)
	}
	if _, err := tr.fs.Read(filepath.Join(tr.state, "token"), 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("the daemon's own directory: %v", err)
	}
	// The configuration's roots stand beside the host's, and the host cannot remove them.
	cfg, err := NewFS([]string{tr.outside}, []string{"**/*.secret"}, nil, tr.home)
	if err != nil {
		t.Fatal(err)
	}
	cfg.SetRoots(nil)
	if _, err := cfg.Read(filepath.Join(tr.outside, "secret.txt"), 0, 0); err != nil {
		t.Fatalf("a configured root: %v", err)
	}
	write(t, filepath.Join(tr.outside, "x.secret"), "no")
	if _, err := cfg.Read(filepath.Join(tr.outside, "x.secret"), 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("a configured deny pattern: %v", err)
	}
	if _, err := NewFS(nil, []string{"relative/*"}, nil, tr.home); err == nil {
		t.Fatal("a relative deny pattern was accepted")
	}
}

func TestListSortsAndFilters(t *testing.T) {
	tr := newTree(t)
	for i, name := range []string{"b.jsonl", "a.jsonl", "c.txt"} {
		p := filepath.Join(tr.transcripts, name)
		write(t, p, name)
		stamp := time.Now().Add(time.Duration(i) * time.Minute)
		if err := os.Chtimes(p, stamp, stamp); err != nil {
			t.Fatal(err)
		}
	}
	entries, truncated, err := tr.fs.List(tr.transcripts, "*.jsonl", "mtime", 0)
	if err != nil || truncated || len(entries) != 2 || entries[0].Name != "a.jsonl" || entries[1].Name != "b.jsonl" {
		t.Fatalf("%+v %v %v", entries, truncated, err)
	}
	entries, truncated, err = tr.fs.List(tr.transcripts, "", "name", 2)
	if err != nil || !truncated || len(entries) != 2 || entries[0].Name != "a.jsonl" {
		t.Fatalf("%+v %v %v", entries, truncated, err)
	}
}

func TestTailFollowsAppendsAndRotation(t *testing.T) {
	tr := newTree(t)
	p := filepath.Join(tr.transcripts, "session.jsonl")
	write(t, p, "one\n")
	ctx := context.Background()
	first, err := tr.fs.Tail(ctx, p, 0, 0, 0, "")
	if err != nil || string(first.Data) != "one\n" || first.NextOffset != 4 || first.Rotated || first.FileID == "" {
		t.Fatalf("%+v %v", first, err)
	}
	// Nothing new: a tail without follow returns empty at once.
	idle, err := tr.fs.Tail(ctx, p, first.NextOffset, 0, 0, first.FileID)
	if err != nil || len(idle.Data) != 0 || idle.NextOffset != 4 {
		t.Fatalf("%+v %v", idle, err)
	}
	// A following tail returns what is appended while it waits.
	go func() {
		time.Sleep(200 * time.Millisecond)
		f, _ := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0)
		f.WriteString("two\n")
		f.Close()
	}()
	start := time.Now()
	next, err := tr.fs.Tail(ctx, p, first.NextOffset, 0, 10*time.Second, first.FileID)
	if err != nil || string(next.Data) != "two\n" || next.NextOffset != 8 || next.Rotated {
		t.Fatalf("%+v %v", next, err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatalf("the append took %s to arrive", time.Since(start))
	}
	// Replaced by a new file that is already longer than the old offset: the inode tells.
	tmp := p + ".new"
	write(t, tmp, "brand new content\n")
	if err := os.Rename(tmp, p); err != nil {
		t.Fatal(err)
	}
	rot, err := tr.fs.Tail(ctx, p, next.NextOffset, 0, 0, next.FileID)
	if err != nil || !rot.Rotated || string(rot.Data) != "brand new content\n" || rot.FileID == next.FileID {
		t.Fatalf("%+v %v", rot, err)
	}
	// Truncated in place: it shrank.
	if err := os.Truncate(p, 3); err != nil {
		t.Fatal(err)
	}
	shrunk, err := tr.fs.Tail(ctx, p, rot.NextOffset, 0, 0, rot.FileID)
	if err != nil || !shrunk.Rotated || string(shrunk.Data) != "bra" {
		t.Fatalf("%+v %v", shrunk, err)
	}
	// A following tail ends early when its caller goes away.
	cctx, cancel := context.WithCancel(ctx)
	time.AfterFunc(100*time.Millisecond, cancel)
	start = time.Now()
	if _, err := tr.fs.Tail(cctx, p, 3, 0, time.Minute, shrunk.FileID); err != nil || time.Since(start) > 5*time.Second {
		t.Fatalf("%v after %s", err, time.Since(start))
	}
}

func TestSpecialFilesAreNotRead(t *testing.T) {
	tr := newTree(t)
	fifo := filepath.Join(tr.project, "pipe")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Skip("no fifo here:", err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := tr.fs.Read(fifo, 0, 0)
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "regular") {
			t.Fatalf("%v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("reading a FIFO blocked")
	}
}

func TestDenyPatterns(t *testing.T) {
	fs, err := NewFS(nil, nil, nil, "/nonexistent-home")
	if err != nil {
		t.Fatal(err)
	}
	for p, want := range map[string]bool{
		"/root/.claude/.credentials.json":                true,
		"/home/someone/.ssh":                             true,
		"/home/someone/.ssh/config":                      true,
		"/srv/p/.git-credentials":                        true,
		"/root/.local/share/opencode/auth.json":          true,
		"/root/.local/share/opencode/storage/x.json":     false,
		"/root/.claude/projects/-srv-p/abc.jsonl":        false,
		"/srv/p/src/ssh.go":                              false,
		"/root/.config/gh/hosts.yml":                     true,
		"/root/.docker/config.json":                      true,
		"/root/.codex/sessions/2026/09/24/rollout.jsonl": false,
	} {
		if got := fs.denied(p); got != want {
			t.Errorf("%s: denied %v, want %v", p, got, want)
		}
	}
}

func TestAFolderCheckHoldsAPathToTheRulesOfARoot(t *testing.T) {
	tr := newTree(t)
	// Outside every root, which is the point: a folder being added is not a root yet.
	st, err := tr.fs.StatRoot(tr.outside)
	if err != nil || !st.Exists || st.Type != "dir" || st.Writable == nil || !*st.Writable {
		t.Fatalf("%+v %v", st, err)
	}
	if st, err := tr.fs.StatRoot(filepath.Join(tr.outside, "later")); err != nil || st.Exists {
		t.Fatalf("a missing folder: %+v %v", st, err)
	}
	for _, p := range []string{"/", tr.home, filepath.Dir(tr.home), filepath.Join(tr.home, ".ssh"), filepath.Join(tr.home, ".ssh", "keys"), tr.state} {
		if _, err := tr.fs.StatRoot(p); !errors.Is(err, ErrForbidden) {
			t.Fatalf("%s: %v", p, err)
		}
		if _, _, err := tr.fs.MkdirRoot(p); !errors.Is(err, ErrForbidden) {
			t.Fatalf("mkdir %s: %v", p, err)
		}
	}
	if _, err := tr.fs.StatRoot("relative"); !errors.Is(err, ErrInvalid) {
		t.Fatal(err)
	}
	// A link that leads into the home is judged by where it leads.
	link := filepath.Join(tr.outside, "to-home")
	if err := os.Symlink(tr.home, link); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.StatRoot(link); !errors.Is(err, ErrForbidden) {
		t.Fatalf("a link to the home: %v", err)
	}
	if _, _, err := tr.fs.MkdirRoot(filepath.Join(link, ".ssh", "nested")); !errors.Is(err, ErrForbidden) {
		t.Fatalf("a folder below a link into a denied place: %v", err)
	}
	// Inside the home is where most project folders are.
	if st, err := tr.fs.StatRoot(filepath.Join(tr.home, "work")); err != nil || !st.Exists {
		t.Fatalf("%+v %v", st, err)
	}
}

func TestMkdirRootMakesAFolderWithItsParentsOnce(t *testing.T) {
	tr := newTree(t)
	target := filepath.Join(tr.home, "code", "new", "site")
	st, created, err := tr.fs.MkdirRoot(target)
	if err != nil || !created || !st.Exists || st.Type != "dir" {
		t.Fatalf("%+v %v %v", st, created, err)
	}
	if info, err := os.Stat(target); err != nil || !info.IsDir() {
		t.Fatal(err)
	}
	if _, created, err := tr.fs.MkdirRoot(target); err != nil || created {
		t.Fatalf("again: %v %v", created, err)
	}
	if _, _, err := tr.fs.MkdirRoot(filepath.Join(tr.project, "README.md")); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a file: %v", err)
	}
	// Nothing about it widened the reads: the new folder is not a root until the host says so.
	write(t, filepath.Join(target, "a.txt"), "x")
	if _, err := tr.fs.Read(filepath.Join(target, "a.txt"), 0, 0); !errors.Is(err, ErrForbidden) {
		t.Fatalf("read: %v", err)
	}
	if st.Writable == nil || !*st.Writable {
		t.Fatalf("%+v", st)
	}
	if err := os.Chmod(target, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(target, 0o700) })
	if os.Geteuid() != 0 {
		if st, err := tr.fs.StatRoot(target); err != nil || st.Writable == nil || *st.Writable {
			t.Fatalf("a read-only folder: %+v %v", st, err)
		}
	}
}

func TestWriteGoesIntoAStaffInboxAndNowhereElse(t *testing.T) {
	tr := newTree(t)
	inbox := filepath.Join(tr.project, ".agents", "inbox", "t1")
	target := filepath.Join(inbox, "spec.md")
	w, err := tr.fs.Write(target, 0, []byte("first "))
	if err != nil || !w.Created || w.Size != 6 {
		t.Fatalf("%+v %v", w, err)
	}
	// A file larger than one frame continues by offset; a wrong offset is refused, not padded.
	if w, err = tr.fs.Write(target, 6, []byte("second")); err != nil || w.Created || w.Size != 12 {
		t.Fatalf("%+v %v", w, err)
	}
	if _, err := tr.fs.Write(target, 3, []byte("x")); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a gap or an overlap: %v", err)
	}
	if body, _ := os.ReadFile(target); string(body) != "first second" {
		t.Fatalf("%q", body)
	}
	// What a member was given never changes under it.
	if _, err := tr.fs.Write(target, 0, []byte("other")); !errors.Is(err, ErrExists) {
		t.Fatalf("overwrite: %v", err)
	}
	if _, err := tr.fs.Write(filepath.Join(tr.project, ".agents", "inbox", ".gitignore"), 0, []byte("*\n")); err != nil {
		t.Fatalf("the inbox's ignore file: %v", err)
	}
	if _, err := tr.fs.Write(filepath.Join(tr.project, ".agents", ".gitignore"), 0, []byte("*\n")); !errors.Is(err, ErrForbidden) {
		t.Fatalf("an ignore file for all of .agents, which may hold the operator's own files: %v", err)
	}
	// A worktree's own inbox is an inbox too: the last .agents of the path counts.
	nested := filepath.Join(tr.project, ".agents", "worktrees", "ira", ".agents", "inbox", "t2", "a.bin")
	if _, err := tr.fs.Write(nested, 0, []byte{0, 1, 2}); err != nil {
		t.Fatalf("a worktree's inbox: %v", err)
	}
	for _, p := range []string{
		filepath.Join(tr.project, "README.md"),                          // any other file of a root
		filepath.Join(tr.project, "new.txt"),                            // a new file outside an inbox
		filepath.Join(tr.project, ".agents", "inbox"),                   // the inbox itself, not a file in it
		filepath.Join(tr.project, ".agents", "worktrees", "x", "a.txt"), // elsewhere under .agents
		filepath.Join(tr.project, ".agents", "inbox", "t1", "..", "..", "..", "README.md"),
		filepath.Join(tr.outside, ".agents", "inbox", "t1", "a.txt"),  // an inbox outside every root
		filepath.Join(tr.home, ".ssh", ".agents", "inbox", "t1", "a"), // an inbox in a denied place
	} {
		if _, err := tr.fs.Write(p, 0, []byte("x")); !errors.Is(err, ErrForbidden) {
			t.Fatalf("%s: %v", p, err)
		}
	}
	if _, err := os.Stat(filepath.Join(tr.outside, ".agents")); !os.IsNotExist(err) {
		t.Fatalf("a refused write made directories: %v", err)
	}
	if _, err := tr.fs.Write("relative/.agents/inbox/t/a", 0, nil); !errors.Is(err, ErrInvalid) {
		t.Fatalf("relative: %v", err)
	}
	if _, err := tr.fs.Write(filepath.Join(inbox, "big"), 0, make([]byte, 512<<10+1)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a chunk over a frame: %v", err)
	}
	if _, err := tr.fs.Write(filepath.Join(inbox, "big"), 50<<20, []byte("x")); !errors.Is(err, ErrInvalid) {
		t.Fatalf("a file over the limit: %v", err)
	}
}

func TestWriteRefusesAnInboxThatIsALinkToElsewhere(t *testing.T) {
	tr := newTree(t)
	// .agents replaced by a link out of the root: the write must not follow it.
	if err := os.Symlink(tr.outside, filepath.Join(tr.project, ".agents")); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Write(filepath.Join(tr.project, ".agents", "inbox", "t1", "a.txt"), 0, []byte("x")); !errors.Is(err, ErrForbidden) {
		t.Fatalf("through a link out of the root: %v", err)
	}
	if _, err := os.Stat(filepath.Join(tr.outside, "inbox")); !os.IsNotExist(err) {
		t.Fatalf("something was made through the link: %v", err)
	}
	// The file itself a link: O_NOFOLLOW refuses it even inside the inbox.
	os.Remove(filepath.Join(tr.project, ".agents"))
	inbox := filepath.Join(tr.project, ".agents", "inbox", "t1")
	if err := os.MkdirAll(inbox, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(tr.project, "README.md"), filepath.Join(inbox, "readme")); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Write(filepath.Join(inbox, "readme"), 5, []byte("!")); err == nil {
		t.Fatal("wrote through a link in the inbox")
	}
	if body, _ := os.ReadFile(filepath.Join(tr.project, "README.md")); string(body) != "hello" {
		t.Fatalf("the linked file changed: %q", body)
	}
	// A FIFO is refused, not waited on.
	if err := syscall.Mkfifo(filepath.Join(inbox, "pipe"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.fs.Write(filepath.Join(inbox, "pipe"), 1, []byte("x")); err == nil {
		t.Fatal("wrote into a FIFO")
	}
}
