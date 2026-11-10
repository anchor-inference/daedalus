//go:build !windows

package main

// restrictToUser does nothing more: the 0o700 the folder was made with is the whole of it here.
func restrictToUser(string) error { return nil }
