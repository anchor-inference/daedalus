package main

import "os"

// writePrivateFile writes one of the files that hold keys, and keeps it this user's alone where the
// mode does not (restrictToUser).
func writePrivateFile(path, body string) error {
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		return err
	}
	return restrictToUser(path)
}
