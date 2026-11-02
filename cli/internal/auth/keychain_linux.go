package auth

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// keychainStore is the freedesktop Secret Service through `secret-tool`: the one Linux path that
// reaches GNOME Keyring and KWallet alike without linking libsecret, which would mean cgo and the end
// of the static binary. It is also the path most often absent (a server, a container, a session
// with no keyring daemon), which is why it is probed and the file is a real fallback.
type keychainStore struct{ service string }

func newKeychainStore(service string) (Store, error) {
	if _, err := exec.LookPath("secret-tool"); err != nil {
		return nil, fmt.Errorf("secret-tool is not installed: %w", err)
	}
	// Without a session bus the tool blocks waiting to start a keyring daemon that never appears,
	// and the client would hang instead of falling back.
	if os.Getenv("DBUS_SESSION_BUS_ADDRESS") == "" {
		return nil, errors.New("no session bus, so no Secret Service")
	}
	return &keychainStore{service: service}, nil
}

func (s *keychainStore) Backend() string { return "Secret Service (secret-tool)" }

func (s *keychainStore) Sealed() bool { return true }

func (s *keychainStore) Get(account string) (string, error) {
	var out, errOut bytes.Buffer
	cmd := exec.Command("secret-tool", "lookup", "service", s.service, "account", account)
	cmd.Stdout, cmd.Stderr = &out, &errOut
	if err := cmd.Run(); err != nil {
		// lookup exits non-zero with nothing printed when no item matches: not paired yet.
		if out.Len() == 0 && errOut.Len() == 0 {
			return "", ErrNotFound
		}
		return "", fmt.Errorf("read the keyring: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	if out.Len() == 0 {
		return "", ErrNotFound
	}
	return strings.TrimRight(out.String(), "\n"), nil
}

// Set passes the token on stdin, which keeps it out of the process list.
func (s *keychainStore) Set(account, value string) error {
	var errOut bytes.Buffer
	cmd := exec.Command("secret-tool", "store", "--label", s.service+" "+account, "service", s.service, "account", account)
	cmd.Stdin, cmd.Stderr = strings.NewReader(value), &errOut
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("write the keyring: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	return nil
}

func (s *keychainStore) Delete(account string) error {
	var errOut bytes.Buffer
	cmd := exec.Command("secret-tool", "clear", "service", s.service, "account", account)
	cmd.Stderr = &errOut
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("delete from the keyring: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	return nil
}
