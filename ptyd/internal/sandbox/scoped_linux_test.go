//go:build linux

package sandbox

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestScopedReadRejectsBroadAndUnsealedSources(t *testing.T) {
	dir := t.TempDir()
	project, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer project.Close()
	plain, err := os.CreateTemp(dir, "plain")
	if err != nil {
		t.Fatal(err)
	}
	defer plain.Close()
	base := ScopedOptions{Bwrap: "bwrap", Argv: []string{"/workspace/tool"}, Cwd: "/workspace",
		Account: "/home/operator/.codex/auth.json", Credential: plain,
		ReadOnly: []ScopedRead{{Source: project, Destination: "/workspace"}}}
	if _, err := WrapScopedRead(base); err == nil || !strings.Contains(err.Error(), "sealed") {
		t.Fatalf("unsealed account accepted: %v", err)
	}
	root, err := os.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "auth"), []byte("selected"), 0600); err != nil {
		t.Fatal(err)
	}
	sealed, err := SnapshotCredentialFile(root, "auth")
	if err != nil {
		t.Fatal(err)
	}
	defer sealed.Close()
	base.Credential = sealed
	for _, destination := range []string{"/", "/home", "/home/operator", "/proc", "/tmp", "/workspace/../peer", "relative"} {
		base.ReadOnly[0].Destination = destination
		if plan, err := WrapScopedRead(base); err == nil {
			plan.Close()
			t.Fatalf("broad or ambiguous destination accepted: %q", destination)
		}
	}
}

func TestScopedReadPhysicalNestedCredentialBoundary(t *testing.T) {
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		t.Skip("bubblewrap is not installed")
	}
	root, err := os.MkdirTemp("/var/tmp", "scoped-account-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	project := filepath.Join(root, "project")
	selected := filepath.Join(root, "selected")
	peer := filepath.Join(root, "peer")
	for _, path := range []string{project, selected, peer} {
		if err := os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	selectedFile := filepath.Join(selected, "auth")
	peerFile := filepath.Join(peer, "auth")
	if err := os.WriteFile(selectedFile, []byte("selected-synthetic-account"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(peerFile, []byte("peer-synthetic-account"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(project, "peer-path"), []byte(peerFile), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(peerFile, filepath.Join(project, "peer-link")); err != nil {
		t.Fatal(err)
	}
	credentialRoot, err := os.Open(selected)
	if err != nil {
		t.Fatal(err)
	}
	defer credentialRoot.Close()
	credential, err := SnapshotCredentialFile(credentialRoot, "auth")
	if err != nil {
		t.Fatal(err)
	}
	defer credential.Close()
	programPath, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	program, err := os.Open(programPath)
	if err != nil {
		t.Fatal(err)
	}
	defer program.Close()
	projectFD, err := os.Open(project)
	if err != nil {
		t.Fatal(err)
	}
	defer projectFD.Close()
	plan, err := WrapScopedRead(ScopedOptions{Bwrap: bwrap, Argv: []string{"/bin/probe", "-test.run=TestScopedReadChild", "-test.v"},
		Cwd: "/workspace", Credential: credential, Account: "/home/operator/.codex/auth.json",
		ReadOnly: []ScopedRead{{Source: program, Destination: "/bin/probe"}, {Source: projectFD, Destination: "/workspace"}}})
	if err != nil {
		t.Fatal(err)
	}
	defer plan.Close()
	cmd := exec.Command(plan.Argv[0], plan.Argv[1:]...)
	cmd.ExtraFiles = plan.ExtraFiles
	cmd.Env = []string{"HOME=" + peer, "XDG_CONFIG_HOME=" + peer, "PATH=/usr/bin:/bin"}
	output, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(output), "scoped_parent_and_nested_child=true") {
		t.Fatalf("physical scoped probe: %v\n%s", err, output)
	}
	t.Log(strings.TrimSpace(string(output)))
}

func TestScopedReadChild(t *testing.T) {
	path, err := os.ReadFile("/workspace/peer-path")
	if err != nil {
		return
	}
	selected, err := os.ReadFile("/home/operator/.codex/auth.json")
	if err != nil || string(selected) != "selected-synthetic-account" {
		t.Fatalf("selected account unavailable: %q %v", selected, err)
	}
	if os.Getenv("HOME") != "/home/operator" || os.Getenv("XDG_CONFIG_HOME") != "/home/operator/.config" {
		t.Fatal("inherited home or XDG locator escaped")
	}
	if err := os.WriteFile("/home/operator/.codex/auth.json", []byte("changed"), 0600); err == nil {
		t.Fatal("selected account was writable")
	}
	for _, denied := range []string{string(path), "/workspace/peer-link", "/proc/1/root" + string(path)} {
		if data, err := os.ReadFile(denied); err == nil {
			t.Fatalf("peer account readable at %q: %q", denied, data)
		}
	}
	if err := os.WriteFile("/home/operator/.config/local", []byte("private"), 0600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("/bin/probe", "-test.run=TestScopedReadNestedChild", "-test.v")
	output, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(output), "nested_peer_denied=true") {
		t.Fatalf("nested child probe: %v\n%s", err, output)
	}
	_, _ = io.WriteString(os.Stdout, "scoped_parent_and_nested_child=true\n")
}

func TestScopedReadNestedChild(t *testing.T) {
	path, err := os.ReadFile("/workspace/peer-path")
	if err != nil {
		return
	}
	if data, err := os.ReadFile(string(path)); err == nil {
		t.Fatalf("nested child read peer account: %q", data)
	}
	_, _ = io.WriteString(os.Stdout, "nested_peer_denied=true\n")
}
