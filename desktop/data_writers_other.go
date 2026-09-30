//go:build !linux && !windows

package main

import "context"

// Other platforms do not expose Linux /proc. Their live data writer gate needs separate process
// and handle enumeration; the fixture suites can still exercise the upgrade journal and rollback.
func quiesceData(context.Context, Paths) error { return nil }

// checkFolderWriters has nothing to look with here either.
func checkFolderWriters(string) error { return nil }
