package auth

import (
	"bytes"
	"fmt"
	"os/exec"
	"strings"
)

// keychainStore is the macOS keychain through the `security` tool, which every macOS has; binding
// the Security framework would need cgo and tie the build to one machine's SDK.
type keychainStore struct{ service string }

func newKeychainStore(service string) (Store, error) {
	if _, err := exec.LookPath("security"); err != nil {
		return nil, fmt.Errorf("the security tool is missing: %w", err)
	}
	return &keychainStore{service: service}, nil
}

func (s *keychainStore) Backend() string { return "macOS keychain" }

func (s *keychainStore) Sealed() bool { return true }

func (s *keychainStore) Get(account string) (string, error) {
	var out, errOut bytes.Buffer
	cmd := exec.Command("security", "find-generic-password", "-s", s.service, "-a", account, "-w")
	cmd.Stdout, cmd.Stderr = &out, &errOut
	if err := cmd.Run(); err != nil {
		// A missing item and a locked keychain fail with the same status; telling someone to pair
		// again when the answer is "unlock your keychain" sends them down a path that cannot work.
		if strings.Contains(errOut.String(), "could not be found") {
			return "", ErrNotFound
		}
		return "", fmt.Errorf("read the keychain: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	return strings.TrimRight(out.String(), "\n"), nil
}

// Set passes the token as an argument, because `security` has no promptless form that reads it from
// stdin. One user cannot read another's argv on macOS, so the exposure is to processes already
// running as the operator, the same exposure the file store has.
func (s *keychainStore) Set(account, value string) error {
	var errOut bytes.Buffer
	// -U updates in place; without it a second pairing leaves two items and reads pick one at random.
	cmd := exec.Command("security", "add-generic-password", "-s", s.service, "-a", account, "-w", value, "-U")
	cmd.Stderr = &errOut
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("write the keychain: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	return nil
}

func (s *keychainStore) Delete(account string) error {
	var errOut bytes.Buffer
	cmd := exec.Command("security", "delete-generic-password", "-s", s.service, "-a", account)
	cmd.Stderr = &errOut
	if err := cmd.Run(); err != nil {
		if strings.Contains(errOut.String(), "could not be found") {
			return ErrNotFound
		}
		return fmt.Errorf("delete from the keychain: %w: %s", err, strings.TrimSpace(errOut.String()))
	}
	return nil
}
