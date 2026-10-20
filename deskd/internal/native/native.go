// Package native opens the backend of the system the daemon was built for: X11 on Linux, Win32 on
// Windows, Quartz on macOS. Each system's file defines open; the others are not compiled.
package native

import (
	"context"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

// Open opens this system's desktop.
func Open(ctx context.Context) (backend.Backend, error) { return open(ctx) }
