//go:build linux

package sandbox

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == "--scoped-exec" {
		if err := RunScopedExec(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	os.Exit(m.Run())
}

func TestRuntimeExportFreezesExecutable(t *testing.T) {
	program, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	bytes, err := os.ReadFile(program)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "probe"), bytes, 0700); err != nil {
		t.Fatal(err)
	}
	directory, err := os.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer directory.Close()
	snapshot, err := SnapshotRuntimeFile(directory, "probe")
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.Close()
	if err := os.WriteFile(filepath.Join(root, "probe"), []byte("replaced"), 0700); err != nil {
		t.Fatal(err)
	}
	content, err := io.ReadAll(snapshot)
	if err != nil || len(content) != len(bytes) || string(content[:4]) != string(bytes[:4]) {
		t.Fatal("runtime snapshot changed with its source")
	}
	for _, path := range []string{"../probe", "/probe", "missing", "alias"} {
		if path == "alias" {
			if err := os.Symlink("probe", filepath.Join(root, path)); err != nil {
				t.Fatal(err)
			}
		}
		if file, err := SnapshotRuntimeFile(directory, path); err == nil {
			file.Close()
			t.Fatalf("unsafe runtime path accepted: %s", path)
		}
	}
}

func TestScopedExecutorRejectsMissingMountCount(t *testing.T) {
	for _, args := range [][]string{{}, {"0", "/usr/bin/bwrap", "--"}, {"9999", "/usr/bin/bwrap", "--"},
		{"1", "bwrap", "--"}} {
		if err := RunScopedExec(args); err == nil {
			t.Fatalf("invalid scoped executor arguments accepted: %q", args)
		}
	}
}

func TestScopedProviderSocketIsTheOnlyHostChannel(t *testing.T) {
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		t.Skip("bubblewrap is not installed")
	}
	root := t.TempDir()
	for _, name := range []string{"selected", "project", "provider", "peer"} {
		if err := os.Mkdir(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	selected := filepath.Join(root, "selected")
	if err := os.WriteFile(filepath.Join(selected, "auth"), []byte("selected-synthetic-account"), 0600); err != nil {
		t.Fatal(err)
	}
	providerPath := filepath.Join(root, "provider", "provider.sock")
	listen := func(path string) int {
		t.Helper()
		fd, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
		if err != nil {
			t.Fatal(err)
		}
		if err := unix.Bind(fd, &unix.SockaddrUnix{Name: path}); err != nil {
			unix.Close(fd)
			t.Fatal(err)
		}
		if err := unix.Listen(fd, 1); err != nil {
			unix.Close(fd)
			t.Fatal(err)
		}
		t.Cleanup(func() { unix.Close(fd) })
		return fd
	}
	listener := listen(providerPath)
	if err := os.Chmod(providerPath, 0600); err != nil {
		t.Fatal(err)
	}
	peerPath := filepath.Join(root, "peer", "peer.sock")
	listen(peerPath)
	project := filepath.Join(root, "project")
	if err := os.WriteFile(filepath.Join(project, "peer-path"), []byte(peerPath), 0600); err != nil {
		t.Fatal(err)
	}
	peerFile := filepath.Join(root, "peer", "secret")
	if err := os.WriteFile(peerFile, []byte("peer-synthetic-account"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(project, "peer-file"), []byte(peerFile), 0600); err != nil {
		t.Fatal(err)
	}
	// An unrelated descriptor without close-on-exec must not survive the daemon's spawn plan.
	peerFD, err := unix.Open(peerFile, unix.O_RDONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(peerFD)
	open := func(path string) *os.File {
		t.Helper()
		file, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { file.Close() })
		return file
	}
	credential, err := SnapshotCredentialFile(open(selected), "auth")
	if err != nil {
		t.Fatal(err)
	}
	defer credential.Close()
	program, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	runtimeFile, err := SnapshotRuntimeFile(open(filepath.Dir(program)), filepath.Base(program))
	if err != nil {
		t.Fatal(err)
	}
	defer runtimeFile.Close()
	providerDirectory := open(filepath.Join(root, "provider"))
	plan, err := WrapScopedRead(ScopedOptions{Bwrap: bwrap,
		Argv: []string{"/bin/probe", "-test.run=TestScopedProviderChild", "-test.v"}, Cwd: "/workspace",
		ReadOnly: []ScopedRead{{Source: runtimeFile, Destination: "/bin/probe"},
			{Source: open(project), Destination: "/workspace"}},
		Credential: credential, Account: "/home/operator/.codex/auth.json", ProviderSocket: providerDirectory})
	if err != nil {
		t.Fatal(err)
	}
	defer plan.Close()
	answer := make(chan error, 1)
	go func() {
		for attempt := 0; attempt < 2; attempt++ {
			fd, _, err := unix.Accept(listener)
			if err != nil {
				answer <- err
				return
			}
			conn := os.NewFile(uintptr(fd), "accepted provider connection")
			err = ServeProviderConnect(conn, "api.test:443", func() (io.ReadWriteCloser, error) {
				pair, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
				if err != nil {
					return nil, err
				}
				endpoint := os.NewFile(uintptr(pair[1]), "pinned synthetic provider")
				go func() {
					defer endpoint.Close()
					request := make([]byte, 4)
					if _, err := io.ReadFull(endpoint, request); err == nil && string(request) == "ping" {
						_, _ = endpoint.Write([]byte("provider-synthetic-response"))
					}
				}()
				return os.NewFile(uintptr(pair[0]), "selected provider tunnel"), nil
			})
			if (attempt == 0 && (err == nil || !strings.Contains(err.Error(), "target refused"))) ||
				(attempt == 1 && err != nil) {
				answer <- err
				return
			}
		}
		answer <- nil
	}()
	cmd := exec.Command(plan.Argv[0], plan.Argv[1:]...)
	cmd.ExtraFiles = plan.ExtraFiles
	if output, err := cmd.CombinedOutput(); err != nil || !strings.Contains(string(output), "provider_socket_only=true") {
		t.Fatalf("scoped provider probe: %v\n%s", err, output)
	}
	if err := <-answer; err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "provider", "other"), []byte("extra"), 0600); err != nil {
		t.Fatal(err)
	}
	if plan, err := WrapScopedRead(ScopedOptions{Bwrap: bwrap,
		Argv: []string{"/bin/probe"}, Cwd: "/workspace",
		ReadOnly: []ScopedRead{{Source: runtimeFile, Destination: "/bin/probe"},
			{Source: open(project), Destination: "/workspace"}},
		Credential: credential, Account: "/home/operator/.codex/auth.json", ProviderSocket: providerDirectory}); err == nil {
		plan.Close()
		t.Fatal("provider directory with another entry accepted")
	}
}

func TestScopedProviderChild(t *testing.T) {
	socket := os.Getenv("DAEDALUS_PROVIDER_SOCKET")
	if socket == "" {
		return
	}
	if socket != "/run/provider/provider.sock" {
		t.Fatalf("provider channel missing: %q", socket)
	}
	connect := func(request string) string {
		t.Helper()
		fd, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
		if err != nil {
			t.Fatal(err)
		}
		if err := unix.Connect(fd, &unix.SockaddrUnix{Name: socket}); err != nil {
			unix.Close(fd)
			t.Fatal(err)
		}
		conn := os.NewFile(uintptr(fd), "provider connection")
		defer conn.Close()
		if _, err := io.WriteString(conn, request); err != nil {
			t.Fatal(err)
		}
		content, err := io.ReadAll(conn)
		if err != nil {
			t.Fatal(err)
		}
		return string(content)
	}
	if response := connect("CONNECT peer.test:443 HTTP/1.1\r\n\r\n"); !strings.HasPrefix(response, "HTTP/1.1 403") {
		t.Fatalf("peer provider request was allowed: %q", response)
	}
	if response := connect("CONNECT api.test:443 HTTP/1.1\r\n\r\nping"); !strings.HasPrefix(response, "HTTP/1.1 200") || !strings.HasSuffix(response, "provider-synthetic-response") {
		t.Fatalf("selected provider answer: %q", response)
	}
	peerPath, err := os.ReadFile("/workspace/peer-path")
	if err != nil {
		t.Fatal(err)
	}
	peer, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Connect(peer, &unix.SockaddrUnix{Name: string(peerPath)}); err == nil {
		unix.Close(peer)
		t.Fatal("peer host socket reachable")
	}
	unix.Close(peer)
	peerFile, err := os.ReadFile("/workspace/peer-file")
	if err != nil {
		t.Fatal(err)
	}
	files, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		path, err := os.Readlink("/proc/self/fd/" + file.Name())
		if err == nil && path == string(peerFile) {
			t.Fatal("unrelated daemon descriptor survived the scoped spawn")
		}
	}
	if credential, err := os.ReadFile("/home/operator/.codex/auth.json"); err != nil || string(credential) != "selected-synthetic-account" {
		t.Fatalf("selected credential missing: %q %v", credential, err)
	}
	_, _ = io.WriteString(os.Stdout, "provider_socket_only=true\n")
}
