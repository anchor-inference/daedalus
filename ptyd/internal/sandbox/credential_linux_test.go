//go:build linux

package sandbox

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
	"golang.org/x/sys/unix"
)

func TestCredentialSnapshotFreezesSelectedFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	selected := filepath.Join(dir, "selected")
	if err := os.Mkdir(selected, 0700); err != nil {
		t.Fatal(err)
	}
	auth := filepath.Join(selected, "auth.json")
	if err := os.WriteFile(auth, []byte("selected-login"), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	snapshot, err := SnapshotCredentialFile(root, "selected/auth.json")
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.Close()
	if err := os.WriteFile(auth, []byte("other-login"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := snapshot.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(snapshot)
	if err != nil || string(got) != "selected-login" {
		t.Fatalf("snapshot changed: %q, %v", got, err)
	}
	if _, err := snapshot.Write([]byte("overwrite")); err == nil {
		t.Fatal("sealed credential was writable")
	}
	seals, err := unix.FcntlInt(snapshot.Fd(), unix.F_GET_SEALS, 0)
	if err != nil || seals&unix.F_SEAL_SEAL == 0 {
		t.Fatalf("credential snapshot is not sealed: %d, %v", seals, err)
	}
}

func TestCredentialSnapshotRejectsAmbiguousProvenance(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	selected := filepath.Join(dir, "selected")
	if err := os.Mkdir(selected, 0700); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(selected, "auth")
	if err := os.WriteFile(file, []byte("selected-login"), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	for _, path := range []string{"../auth", "/auth", "selected/../selected/auth", "selected//auth", "selected\\auth", ""} {
		if snapshot, err := SnapshotCredentialFile(root, path); err == nil {
			snapshot.Close()
			t.Fatalf("accepted ambiguous path %q", path)
		}
	}
	if err := os.Link(file, filepath.Join(dir, "cross-project-auth")); err != nil {
		t.Fatal(err)
	}
	if snapshot, err := SnapshotCredentialFile(root, "selected/auth"); err == nil {
		snapshot.Close()
		t.Fatal("accepted a credential hard-linked into another project")
	}
	if err := os.Remove(filepath.Join(dir, "cross-project-auth")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("auth", filepath.Join(selected, "alias")); err != nil {
		t.Fatal(err)
	}
	if snapshot, err := SnapshotCredentialFile(root, "selected/alias"); err == nil {
		snapshot.Close()
		t.Fatal("accepted a credential symlink")
	}
	if err := os.Chmod(selected, 0755); err != nil {
		t.Fatal(err)
	}
	if snapshot, err := SnapshotCredentialFile(root, "selected/auth"); err == nil || !strings.Contains(err.Error(), "private directory") {
		if snapshot != nil {
			snapshot.Close()
		}
		t.Fatalf("accepted a public credential parent: %v", err)
	}
}

func TestCredentialSnapshotInSandboxedProgramAndTerminal(t *testing.T) {
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		t.Skip("bubblewrap is not installed")
	}
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is not installed")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "selected"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "peer"), 0700); err != nil {
		t.Fatal(err)
	}
	selected := filepath.Join(dir, "selected", "auth")
	peer := filepath.Join(dir, "peer", "auth")
	if err := os.WriteFile(selected, []byte("selected-login"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(peer, []byte("peer-login"), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	snapshot, err := SnapshotCredentialFile(root, "selected/auth")
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.Close()
	probe := "import sys; a,b=sys.argv[1:]; assert open(a).read()=='selected-login'; " +
		"\ntry: open(b).read()\nexcept OSError: pass\nelse: raise AssertionError('peer credential visible')\n" +
		"print('selected_login_only=True')"
	for _, usePTY := range []bool{false, true} {
		t.Run(strconv.FormatBool(usePTY), func(t *testing.T) {
			if _, err := snapshot.Seek(0, io.SeekStart); err != nil {
				t.Fatal(err)
			}
			plan, err := Wrap(Options{Bwrap: bwrap, Argv: []string{python, "-c", probe, selected, peer},
				Cwd: "/", Mask: []string{dir}})
			if err != nil {
				t.Fatal(err)
			}
			at := len(plan.Argv) - 3 - 5
			argv := append([]string{}, plan.Argv[:at]...)
			argv = append(argv, "--perms", "0600", "--ro-bind-data", "3", selected)
			plan.Argv = append(argv, plan.Argv[at:]...)
			if !usePTY {
				cmd := exec.Command(plan.Argv[0], plan.Argv[1:]...)
				cmd.ExtraFiles = []*os.File{snapshot}
				output, err := cmd.CombinedOutput()
				if err != nil {
					t.Fatalf("sandboxed program: %v: %s", err, output)
				}
				if !strings.Contains(string(output), "selected_login_only=True") {
					t.Fatalf("missing evidence: %s", output)
				}
				return
			}
			proc, err := ptyproc.Start(ptyproc.Spec{Path: plan.Argv[0], Argv: plan.Argv,
				Dir: "/", Env: os.Environ(), Cols: 80, Rows: 24, Wrapped: true, ExtraFiles: []*os.File{snapshot}})
			if err != nil {
				t.Fatal(err)
			}
			defer proc.Master.Close()
			read := make(chan []byte, 1)
			go func() { output, _ := io.ReadAll(proc.Master); read <- output }()
			select {
			case <-proc.Done():
			case <-time.After(10 * time.Second):
				proc.Signal(9)
				t.Fatal("sandboxed terminal did not exit")
			}
			output := <-read
			if result := proc.Wait(); result.Code != 0 || !strings.Contains(string(output), "selected_login_only=True") {
				t.Fatalf("sandboxed terminal: %+v: %s", result, output)
			}
		})
	}
}
