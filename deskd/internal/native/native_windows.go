package native

import (
	"context"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/win"
)

func open(ctx context.Context) (backend.Backend, error) { return win.Open(ctx) }
