//go:build linux

package sandbox

import (
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
)

func pinnedFixture(t *testing.T) (*os.File, string) {
	t.Helper()
	path := t.TempDir()
	if err := os.WriteFile(filepath.Join(path, "auth"), []byte("selected-synthetic-account"), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { root.Close() })
	return root, path
}

func TestPinnedSourcesRejectAndClose(t *testing.T) {
	root, path := pinnedFixture(t)
	if err := os.Symlink("auth", filepath.Join(path, "alias")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(".", filepath.Join(path, "parent")); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"../auth", "/auth", ".", "dir/../auth", "alias", "parent/auth", "missing"} {
		_, err := WrapPinnedReadOnly(Options{Bwrap: "bwrap", Argv: []string{"true"}, Cwd: path}, root,
			[]ReadSource{{Relative: "auth", Destination: "/first"}, {Relative: bad, Destination: "/second"}})
		if err == nil {
			t.Fatalf("unsafe source accepted: %s", bad)
		}
	}
	before, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	for index := 0; index < 30; index++ {
		for _, sources := range [][]ReadSource{
			{{Relative: "auth", Destination: "/first"}, {Relative: "missing", Destination: "/second"}},
			{{Relative: "auth", Destination: "/first"}},
		} {
			if _, err := WrapPinnedReadOnly(Options{}, root, sources); err == nil {
				t.Fatal("invalid plan accepted")
			}
		}
	}
	after, err := os.ReadDir("/proc/self/fd")
	if err != nil || len(before) != len(after) {
		t.Fatalf("descriptor leak: before=%d after=%d err=%v", len(before), len(after), err)
	}
	plan, err := WrapPinnedReadOnly(Options{Bwrap: "bwrap", Argv: []string{"true"}, Cwd: path}, root,
		[]ReadSource{{Relative: "auth", Destination: "/first"}, {Relative: "auth", Destination: "/second"}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(plan.Argv, " "), "--ro-bind-fd 3 /first --ro-bind-fd 4 /second") {
		t.Fatalf("descriptor mapping drifted: %v", plan.Argv)
	}
	cmd := exec.Command(filepath.Join(path, "missing-program"))
	cmd.ExtraFiles = plan.ExtraFiles
	if err := cmd.Start(); err == nil {
		t.Fatal("missing executable started")
	}
	for index := 0; index < 2; index++ {
		if err := plan.Close(); err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range plan.ExtraFiles {
		if _, err := file.Stat(); !errors.Is(err, os.ErrClosed) {
			t.Fatalf("source remained open: %v", err)
		}
	}
}

func TestPinnedSourcesPhysicalReplacement(t *testing.T) {
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		t.Skip("bubblewrap is not installed")
	}
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is not installed")
	}
	for _, usePTY := range []bool{false, true} {
		t.Run(strconv.FormatBool(usePTY), func(t *testing.T) {
			root, path := pinnedFixture(t)
			if err := os.Mkdir(filepath.Join(path, "selected"), 0700); err != nil {
				t.Fatal(err)
			}
			selected := filepath.Join(path, "selected", "auth")
			if err := os.WriteFile(selected, []byte("selected-synthetic-account"), 0600); err != nil {
				t.Fatal(err)
			}
			first, second := filepath.Join(path, "exposed", "first"), filepath.Join(path, "exposed", "second")
			probe := "import os,sys; a,b=sys.argv[1:]; assert open(a).read()=='selected-synthetic-account'; assert open(b).read()=='selected-synthetic-account'; " +
				"print('pinned_original_account=True'); " +
				"\nfor n in (3,4):\n try: os.fstat(n)\n except OSError: pass\n else: raise AssertionError('source descriptor leaked')\n" +
				"try: open(a,'w')\nexcept OSError: pass\nelse: raise AssertionError('credential writable')\n"
			cwd := t.TempDir()
			plan, err := WrapPinnedReadOnly(Options{Bwrap: bwrap, Argv: []string{python, "-c", probe, first, second}, Cwd: cwd, Mask: []string{path}}, root,
				[]ReadSource{{Relative: "selected/auth", Destination: first}, {Relative: "auth", Destination: second}})
			if err != nil {
				t.Fatal(err)
			}
			defer plan.Close()
			if err := os.Rename(selected, selected+".old"); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(selected, []byte("peer-synthetic-account"), 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.Rename(filepath.Join(path, "selected"), filepath.Join(path, "original")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink("original", filepath.Join(path, "selected")); err != nil {
				t.Fatal(err)
			}
			if err := root.Close(); err != nil {
				t.Fatal(err)
			}
			var output []byte
			if !usePTY {
				cmd := exec.Command(plan.Argv[0], plan.Argv[1:]...)
				cmd.ExtraFiles = plan.ExtraFiles
				output, err = cmd.CombinedOutput()
				if err != nil {
					t.Fatalf("physical fd probe failed: %v\n%s", err, output)
				}
			} else {
				proc, err := ptyproc.Start(ptyproc.Spec{Path: plan.Argv[0], Argv: plan.Argv, Dir: cwd, Env: os.Environ(),
					Cols: 80, Rows: 24, Wrapped: true, ExtraFiles: plan.ExtraFiles})
				if err != nil {
					t.Fatal(err)
				}
				defer proc.Master.Close()
				if err := plan.Close(); err != nil {
					t.Fatal(err)
				}
				read := make(chan []byte, 1)
				go func() { data, _ := io.ReadAll(proc.Master); read <- data }()
				select {
				case <-proc.Done():
				case <-time.After(10 * time.Second):
					proc.Signal(9)
					t.Fatal("physical PTY probe did not exit")
				}
				output = <-read
				if result := proc.Wait(); result.Code != 0 {
					t.Fatalf("physical PTY fd probe failed: %+v\n%s", result, output)
				}
			}
			if !strings.Contains(string(output), "pinned_original_account=True") {
				t.Fatalf("physical evidence missing: %s", output)
			}
			t.Log(strings.TrimSpace(string(output)))
		})
	}
}
