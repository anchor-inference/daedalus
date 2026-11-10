//go:build !windows

package main

// heldByReader is never true: a rename here replaces a file whoever has it open.
func heldByReader(error) bool { return false }
