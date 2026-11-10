//go:build !windows

package main

// restrictToUser does nothing more: the 0o700 or 0o600 the path was made with is the whole of it here.
func restrictToUser(string) error { return nil }
