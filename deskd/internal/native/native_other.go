//go:build !linux && !windows && !darwin

package native

import (
	"context"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

func open(context.Context) (backend.Backend, error) { return nil, backend.ErrNoBackend }
