// Package auth holds what proves this machine to a Daedalus server: the device token that pairing
// mints, kept in the operator's keychain where one can be reached without cgo and in a file only
// its owner can read otherwise, and the pairing flow that obtains it.
package auth

import (
	"errors"
	"fmt"
	"os"
	"strings"
)

// Service is the keychain service every item of this client is filed under; the account is the
// profile's name.
const Service = "daedalus-cli"

// EnvToken supplies a token instead of the store: the static API token for scripts, or a device token
// on a machine with no keychain and no wish for a file.
const EnvToken = "DAEDALUS_TOKEN"

// EnvStore pins the store: file, keychain or auto.
const EnvStore = "DAEDALUS_CLI_CREDENTIAL_STORE"

// ErrNotFound means nothing is stored under that name: a profile that has not paired yet. Callers
// turn it into "run daedalus-cli login", not into a failure.
var ErrNotFound = errors.New("no credential stored")

// Store is where tokens live on this machine.
type Store interface {
	Get(account string) (string, error)
	Set(account, value string) error
	Delete(account string) error
	// Backend names what actually holds the secret, so `status` and `doctor` can say it plainly
	// instead of implying the stronger store.
	Backend() string
	// Sealed reports whether the secret is held by the system rather than lying in a readable file.
	Sealed() bool
}

// OpenStore returns the best store this machine has, or the one EnvStore pins. A keychain that asks
// to be unlocked on every read makes an unattended client unusable, which is why the weaker file
// can be chosen knowingly.
func OpenStore(dir string) (Store, error) {
	switch strings.ToLower(os.Getenv(EnvStore)) {
	case "file":
		return newFileStore(dir)
	case "keychain":
		s, err := newKeychainStore(Service)
		if err != nil {
			return nil, fmt.Errorf("the keychain was asked for and is not available: %w", err)
		}
		return s, nil
	case "", "auto":
	default:
		return nil, fmt.Errorf("%s=%q: want file, keychain or auto", EnvStore, os.Getenv(EnvStore))
	}
	if s, err := newKeychainStore(Service); err == nil {
		return s, nil
	}
	return newFileStore(dir)
}

// Token is the token for a profile: the environment's when set, the store's otherwise. The second
// value says where it came from.
func Token(store Store, account string) (string, string, error) {
	if v := os.Getenv(EnvToken); v != "" {
		return v, "the " + EnvToken + " environment variable", nil
	}
	if store == nil {
		return "", "", ErrNotFound
	}
	v, err := store.Get(account)
	if err != nil {
		return "", "", err
	}
	return v, store.Backend(), nil
}
