package native

import (
	"context"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/quartz"
)

func open(ctx context.Context) (backend.Backend, error) { return quartz.Open(ctx) }
