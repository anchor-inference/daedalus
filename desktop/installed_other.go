//go:build !windows

package main

// noteInstalledVersion has nothing to keep up to date elsewhere: a .dmg and an unpacked folder
// leave no record of the version, and a package's record is its package manager's.
func noteInstalledVersion(string, string) {}
