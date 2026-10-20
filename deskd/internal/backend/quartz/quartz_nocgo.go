//go:build darwin && !cgo

package quartz

import (
	"context"
	"fmt"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

// Open fails: every call this backend makes is a CoreGraphics or AppKit function, and a build without
// cgo (a cross-compile from another system, or CGO_ENABLED=0) has none of them.
func Open(ctx context.Context) (backend.Backend, error) {
	return nil, fmt.Errorf("%w: this macOS build was made without cgo, which the Quartz backend needs", backend.ErrNoBackend)
}
