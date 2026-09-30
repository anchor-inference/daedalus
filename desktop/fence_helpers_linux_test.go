//go:build linux

package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// The fence tests act on real ext4 in disposable folders. Everything that must happen at an exact
// moment happens through a seam of the switch; everything that must block on a lease break is a
// child process (this test binary re-run as a helper), because a lease break blocks the opener.

func init() {
	platformTestHelper = fenceTestHelper
}

// fenceRequireExt4 skips on any other filesystem — inside the golang container /tmp is overlayfs
// — unless DAEDALUS_FENCE_REQUIRE_EXT4 is set, which the native run sets so that a skip can never
// pass for a result.
func fenceRequireExt4(t *testing.T, path string) {
	t.Helper()
	var fs unix.Statfs_t
	if err := unix.Statfs(path, &fs); err != nil {
		t.Fatal(err)
	}
	if uint64(fs.Type) == fenceExt4Magic {
		return
	}
	if os.Getenv("DAEDALUS_FENCE_REQUIRE_EXT4") != "" {
		t.Fatalf("disposable ext4 required, %s is filesystem %#x", path, uint64(fs.Type))
	}
	t.Skipf("the fence is proven on ext4 only and %s is filesystem %#x; set DAEDALUS_FENCE_REQUIRE_EXT4=1 to make this a failure", path, uint64(fs.Type))
}

// fenceFixture is a small data folder as it is once the runtime lives outside it: a database, a
// checkout file, a workspace note and the environment file.
func fenceFixture(t *testing.T) string {
	t.Helper()
	base := t.TempDir()
	fenceRequireExt4(t, base)
	data := filepath.Join(base, "data")
	for _, dir := range []string{"", "state", "workspaces", "daedalus"} {
		if err := os.Mkdir(filepath.Join(data, dir), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string][]byte{
		"state/daedalus.sqlite": make([]byte, 8192),
		"workspaces/note.txt":   []byte("before"),
		"daedalus/uv.lock":      []byte("before"),
		".env":                  []byte("API_PORT=1\n"),
	}
	for rel, body := range files {
		if err := os.WriteFile(filepath.Join(data, rel), body, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return data
}

func fenceRun(t *testing.T, data string, seams *fenceSeams) fenceReport {
	t.Helper()
	rep := fencedSwitch(fenceOptions{Data: data, seams: seams})
	t.Logf("switch %s: %s (%s) entries=%v retained=%v gc=%v late=%v overflow=%v leases=%d %dms",
		rep.Op, rep.Outcome, rep.Reason, rep.Entries, rep.Retained, rep.GC, rep.LateWritePossible, rep.Overflow, rep.LeasesTaken, rep.DurationMS)
	return rep
}

// fenceAssertReport checks the report on disk is the one returned: written before return, with
// fsync, and complete.
func fenceAssertReport(t *testing.T, rep fenceReport) {
	t.Helper()
	if rep.ReportPath == "" {
		t.Fatalf("no report on disk for %s", rep.Op)
	}
	body, err := os.ReadFile(rep.ReportPath)
	if err != nil {
		t.Fatalf("report: %v", err)
	}
	var disk fenceReport
	if err := json.Unmarshal(body, &disk); err != nil {
		t.Fatalf("report JSON: %v", err)
	}
	if disk.Op != rep.Op || disk.Outcome != rep.Outcome || disk.Reason != rep.Reason || disk.ExitCode != rep.ExitCode ||
		disk.Overflow != rep.Overflow || len(disk.Entries) != len(rep.Entries) || len(disk.Retained) != len(rep.Retained) ||
		len(disk.LateWritePossible) != len(rep.LateWritePossible) || len(disk.GC) != len(rep.GC) {
		t.Fatalf("report on disk differs:\ndisk %+v\nreturned %+v", disk, rep)
	}
	if disk.ExitCode != fenceExitCode(disk.Outcome) {
		t.Fatalf("exit code %d for %s", disk.ExitCode, disk.Outcome)
	}
}

func fenceWant(t *testing.T, rep fenceReport, outcome string) {
	t.Helper()
	if rep.Outcome != outcome {
		t.Fatalf("outcome %s, want %s: %s; entries %+v", rep.Outcome, outcome, rep.Reason, rep.Entries)
	}
	fenceAssertReport(t, rep)
}

func fenceHasEntry(rep fenceReport, tree, path, cause string) bool {
	for _, entry := range rep.Entries {
		if entry.Tree == tree && entry.Path == path && strings.Contains(entry.Cause, cause) {
			return true
		}
	}
	return false
}

func fenceIno(t *testing.T, path string) uint64 {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Sys().(*syscall.Stat_t).Ino
}

func fenceRead(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(body)
}

func fenceControlDir(data string) string { return fenceControlPath(data) }

func fenceRetainedPath(data string, rep fenceReport, kind string) string {
	for _, r := range rep.Retained {
		if r.Kind == kind {
			return r.Path
		}
	}
	return ""
}

// fenceNoSlot checks that no slot is left beside data.
func fenceNoSlot(t *testing.T, data string) {
	t.Helper()
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(data), "."+filepath.Base(data)+"-slot-*"))
	if len(matches) > 0 {
		t.Fatalf("slot left beside data: %v", matches)
	}
}

func fenceGC(t *testing.T, data, name string, operator bool, seams *fenceSeams) fenceGCReport {
	t.Helper()
	g := fenceCollectRetained(data, "", name, operator, seams)
	t.Logf("gc %s: %s (%s) entries=%v overflow=%v deleted=%d", name, g.Outcome, g.Reason, g.Entries, g.Overflow, g.Deleted)
	return g
}

// fenceNode finds a held node of a fenced tree by its path.
func fenceNodeOf(t *testing.T, tree *fenceTree, rel string) *fenceNode {
	t.Helper()
	for _, group := range [][]*fenceNode{tree.dirs, tree.files, tree.links} {
		for _, node := range group {
			if node.rel == rel {
				return node
			}
		}
	}
	t.Fatalf("%s has no %s", tree.label, rel)
	return nil
}

// fenceWaitBroken waits until the kernel shows the lease on one file as broken: the writer is now
// blocked in open() on it.
func fenceWaitBroken(t *testing.T, tree *fenceTree, rel string) time.Duration {
	t.Helper()
	node := fenceNodeOf(t, tree, rel)
	start := time.Now()
	for time.Since(start) < 10*time.Second {
		if lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0); err != nil || lease != unix.F_RDLCK {
			return time.Since(start)
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("the writer never reached the lease on %s %s", tree.label, rel)
	return 0
}

type fenceChild struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
	lines chan string
}

func startFenceChild(t *testing.T, mode string, env ...string) *fenceChild {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), append([]string{helperEnv + "=" + mode}, env...)...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	c := &fenceChild{cmd: cmd, stdin: stdin, lines: make(chan string, 16)}
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			c.lines <- scanner.Text()
		}
		close(c.lines)
	}()
	t.Cleanup(func() {
		c.stdin.Close()
		_ = c.cmd.Process.Kill()
		_ = c.cmd.Wait()
	})
	return c
}

func (c *fenceChild) expect(t *testing.T, prefix string, timeout time.Duration) string {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case line, ok := <-c.lines:
			if !ok {
				t.Fatalf("helper ended before %q", prefix)
			}
			if strings.HasPrefix(line, prefix) {
				return line
			}
		case <-deadline:
			t.Fatalf("helper did not say %q in %s", prefix, timeout)
		}
	}
}

func (c *fenceChild) say(t *testing.T, line string) {
	t.Helper()
	if _, err := io.WriteString(c.stdin, line+"\n"); err != nil {
		t.Fatal(err)
	}
}

func (c *fenceChild) pid() int { return c.cmd.Process.Pid }

// fenceTestHelper is the child side. Each mode is one foreign actor.
func fenceTestHelper(mode string) {
	if !strings.HasPrefix(mode, "fence-") {
		return
	}
	path := os.Getenv("HELPER_PATH")
	stdin := bufio.NewScanner(os.Stdin)
	switch mode {
	case "fence-write":
		// A writer that opens an existing file and overwrites its first bytes. The open blocks
		// while someone else holds a lease on the file.
		fmt.Println("opening")
		start := time.Now()
		fd, err := unix.Open(path, unix.O_WRONLY|unix.O_CLOEXEC, 0)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		blocked := time.Since(start)
		if _, err := unix.Pwrite(fd, []byte(os.Getenv("HELPER_BODY")), 0); err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		unix.Close(fd)
		fmt.Printf("wrote %d\n", blocked.Milliseconds())
	case "fence-map-write":
		// A writer that maps the file shared and writes through the mapping.
		fmt.Println("opening")
		fd, err := unix.Open(path, unix.O_RDWR|unix.O_CLOEXEC, 0)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		body := os.Getenv("HELPER_BODY")
		mapping, err := unix.Mmap(fd, 0, len(body), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		copy(mapping, body)
		_ = unix.Munmap(mapping)
		unix.Close(fd)
		fmt.Println("wrote")
	case "fence-hold-open":
		// A backend that keeps its log open for appending.
		fd, err := unix.Open(path, unix.O_WRONLY|unix.O_APPEND|unix.O_CREAT|unix.O_CLOEXEC, 0o600)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		fmt.Println("ready")
		for stdin.Scan() {
		}
		unix.Close(fd)
	case "fence-thread-cwd":
		// One thread with a working directory of its own inside the tree; the process's own
		// working directory stays outside. The main goroutine keeps the thread-group leader, so
		// the other thread can never be the one /proc/<pid>/cwd shows.
		runtime.LockOSThread()
		ready := make(chan int)
		go func() {
			runtime.LockOSThread()
			if err := unix.Unshare(unix.CLONE_FS); err != nil {
				fmt.Println("error", err)
				os.Exit(3)
			}
			if err := unix.Chdir(path); err != nil {
				fmt.Println("error", err)
				os.Exit(3)
			}
			ready <- unix.Gettid()
			select {}
		}()
		fmt.Printf("ready %d\n", <-ready)
		for stdin.Scan() {
		}
	case "fence-nondumpable":
		// A process of this user that /proc will not show: cwd inside the tree, and later a write
		// relative to it.
		if err := unix.Prctl(unix.PR_SET_DUMPABLE, 0, 0, 0, 0); err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		if err := unix.Chdir(path); err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		fmt.Println("ready")
		for stdin.Scan() {
			fields := strings.Fields(stdin.Text())
			if len(fields) == 3 && fields[0] == "write" {
				if err := os.WriteFile(fields[1], []byte(fields[2]), 0o600); err != nil {
					fmt.Println("error", err)
					continue
				}
				fmt.Println("ok")
			}
		}
	case "fence-crash":
		// A switch that dies at one point, as a power cut or a kill -9 would end it.
		die := func() { fmt.Println("dying"); os.Exit(9) }
		seams := &fenceSeams{}
		switch os.Getenv("HELPER_AT") {
		case "after-exchange":
			seams.afterExchange = func(p, c *fenceTree) { die() }
		case "before-exchange":
			seams.afterCFence = func(c *fenceTree) { die() }
		case "committing":
			seams.afterFinalSweep = func(p, c *fenceTree) { die() }
		}
		fencedSwitch(fenceOptions{Data: path, seams: seams})
		fmt.Println("finished without dying")
	case "fence-map-read":
		// A reader that maps the file shared and read-only and closes its descriptor: it holds no
		// file open, and nothing but its memory map says it is using the tree.
		fd, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC, 0)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		mapping, err := unix.Mmap(fd, 0, 1, unix.PROT_READ, unix.MAP_SHARED)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		unix.Close(fd)
		fmt.Println("ready", mapping[0])
		for stdin.Scan() {
		}
	case "fence-switch":
		// One switch of its own process, so that what it left in the page cache dies with nothing
		// but the kernel to write it back.
		rep := fencedSwitch(fenceOptions{Data: path})
		fmt.Println("outcome", rep.Outcome, rep.Reason)
	case "fence-readwrite-lock":
		// The lock call of the builds before the lock was held read-only: O_CREAT|O_RDWR, then flock.
		fd, err := unix.Open(path, unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC, 0o600)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		if err := unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
			fmt.Println("errLocked")
		} else {
			fmt.Println("locked")
		}
		unix.Close(fd)
	case "fence-launcher-lock":
		// This launcher's own lock call, unchanged.
		file, err := lockFile(path)
		switch {
		case err == nil:
			fmt.Println("locked")
			unlockFile(file)
		case err == errLocked:
			fmt.Println("errLocked")
		default:
			fmt.Println("error", err)
		}
	default:
		fmt.Println("unknown helper", mode)
		os.Exit(2)
	}
	os.Exit(0)
}

func fenceSHA(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// fenceTreeDigest is every path of a tree with its mode and, for a file, its content hash — what
// "the data is as it was" means in a test.
func fenceTreeDigest(t *testing.T, root string) string {
	t.Helper()
	var lines []string
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		line := fmt.Sprintf("%s %v", rel, info.Mode())
		if info.Mode().IsRegular() {
			line += " " + fenceSHA(t, path)
		}
		lines = append(lines, line)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join(lines, "\n")
}
