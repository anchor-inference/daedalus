// Package server is the daemon's socket: the run directory and its files, the token handshake, the
// channel framing, and the JSON-RPC dispatcher.
package server

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// The files of a run directory. The host is told only the directory; everything else it learns from
// these. The socket and the lock carry the daemon's name (SocketName, lockName), so a directory
// handed to the wrong daemon is refused by the files it holds rather than silently shared.
const (
	EndpointFile = "endpoint" // "unix:<daemon>.sock" or "tcp:127.0.0.1:<port>", written last
	TokenFile    = "token"    // 64 hex characters, 0600, new at every start
)

// SocketName is the socket file of the daemon called daemon ("ptyd", "browserd"), mode 0600.
func SocketName(daemon string) string { return daemon + ".sock" }

func lockName(daemon string) string { return daemon + ".lock" }

// Endpoint is a listening socket with its run directory prepared.
type Endpoint struct {
	Listener net.Listener
	Token    string
	RunDir   string
	release  func()
}

// ErrHeld is what a HeldError matches: another daemon is serving the run directory.
var ErrHeld = errors.New("another daemon holds the run directory")

// HeldError names the daemon and the directory, in the words the daemon exits with.
type HeldError struct {
	Daemon, Dir, Detail string
}

func (e *HeldError) Error() string {
	msg := fmt.Sprintf("another %s holds the run directory %s", e.Daemon, e.Dir)
	if e.Detail != "" {
		msg += ": " + e.Detail
	}
	return msg
}

func (e *HeldError) Is(target error) bool { return target == ErrHeld }

// Prepare claims runDir for the daemon called daemon and listens. The order matters to a host that is watching the directory:
// the token is written before the endpoint, and the endpoint only once the socket accepts, so an
// endpoint file that exists always leads to a daemon that is ready and a token that matches it.
func Prepare(runDir, listen, daemon string) (*Endpoint, error) {
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		return nil, err
	}
	// The directory holds the token; nobody else needs to list it.
	if err := os.Chmod(runDir, 0o700); err != nil {
		return nil, err
	}
	if err := RestrictDir(runDir); err != nil {
		return nil, err
	}
	unlock, err := lockDir(filepath.Join(runDir, lockName(daemon)))
	if err != nil {
		return nil, &HeldError{Daemon: daemon, Dir: runDir, Detail: err.Error()}
	}
	ok := false
	defer func() {
		if !ok {
			unlock()
		}
	}()
	// Whatever a previous daemon left is either live (refuse) or stale (remove).
	var ln net.Listener
	var endpoint string
	if listen == "unix" {
		sock := filepath.Join(runDir, SocketName(daemon))
		if c, err := net.DialTimeout("unix", sock, time.Second); err == nil {
			c.Close()
			return nil, &HeldError{Daemon: daemon, Dir: runDir}
		}
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		if err := os.Remove(sock); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if len(sock) > 104 {
			// sun_path is 108 bytes on Linux and 104 on macOS; a longer path fails with a confusing
			// "invalid argument", so say what is wrong.
			return nil, fmt.Errorf("socket path %s is %d bytes, longer than a unix socket allows", sock, len(sock))
		}
		ln, err = net.Listen("unix", sock)
		if err != nil {
			return nil, err
		}
		if err := os.Chmod(sock, 0o600); err != nil {
			ln.Close()
			return nil, err
		}
		endpoint = "unix:" + SocketName(daemon)
	} else {
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		addr := strings.TrimPrefix(listen, "tcp:")
		ln, err = net.Listen("tcp", addr)
		if err != nil {
			return nil, err
		}
		endpoint = "tcp:" + ln.Addr().String()
	}
	token, err := NewToken()
	if err != nil {
		ln.Close()
		return nil, err
	}
	if err := writeAtomic(filepath.Join(runDir, TokenFile), []byte(token+"\n")); err != nil {
		ln.Close()
		return nil, err
	}
	if err := writeAtomic(filepath.Join(runDir, EndpointFile), []byte(endpoint+"\n")); err != nil {
		ln.Close()
		return nil, err
	}
	ok = true
	e := &Endpoint{Listener: ln, Token: token, RunDir: runDir}
	e.release = func() {
		// The endpoint goes first, so that a watching host never finds an endpoint without a daemon.
		_ = os.Remove(filepath.Join(runDir, EndpointFile))
		_ = os.Remove(filepath.Join(runDir, TokenFile))
		if listen == "unix" {
			_ = os.Remove(filepath.Join(runDir, SocketName(daemon)))
		}
		unlock()
	}
	return e, nil
}

// Release removes the endpoint, the token and the socket, and gives up the directory.
func (e *Endpoint) Release() {
	if e.release != nil {
		e.release()
		e.release = nil
	}
}

// WriteSecret writes a file only this user may read (0600, and on Windows an access list to match)
// through a temporary name, so a reader never sees half of it: a second token a daemon hands to one
// client alone.
func WriteSecret(path string, data []byte) error { return writeAtomic(path, data) }

// NewToken is a fresh token in the form the run directory's token has: 32 random bytes in hex.
func NewToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

// writeAtomic writes a 0600 file through a temporary name, so a reader never sees half of it.
func writeAtomic(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := restrictFile(tmp); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

// ReadEndpoint reads a run directory the way a client does: the endpoint and the token.
func ReadEndpoint(runDir string) (network, address, token string, err error) {
	ep, err := os.ReadFile(filepath.Join(runDir, EndpointFile))
	if err != nil {
		return "", "", "", err
	}
	tok, err := os.ReadFile(filepath.Join(runDir, TokenFile))
	if err != nil {
		return "", "", "", err
	}
	e := strings.TrimSpace(string(ep))
	switch {
	case strings.HasPrefix(e, "unix:"):
		// Relative to the run directory, so a directory bind-mounted elsewhere still works.
		name := strings.TrimPrefix(e, "unix:")
		if !filepath.IsAbs(name) {
			name = filepath.Join(runDir, name)
		}
		return "unix", name, strings.TrimSpace(string(tok)), nil
	case strings.HasPrefix(e, "tcp:"):
		return "tcp", strings.TrimPrefix(e, "tcp:"), strings.TrimSpace(string(tok)), nil
	}
	return "", "", "", fmt.Errorf("endpoint %q is neither unix: nor tcp:", e)
}
