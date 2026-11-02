//go:build !darwin && !linux

package auth

import "errors"

// newKeychainStore has no keychain to offer here. On Windows the Credential Manager is reachable only
// through a hand-written advapi32 binding that nothing in this repository's CI can exercise, and an
// untested path holding a token is worse than a file whose weakness `status` states.
func newKeychainStore(string) (Store, error) {
	return nil, errors.New("no keychain this client can reach on this system")
}
