package native

import (
	"context"
	"errors"
	"os"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/x11"
)

// ErrWayland is a Wayland session. Its X11 compatibility server would let the daemon see and drive
// only the programs that still run on X11, and report the rest of the screen as black, so the session
// is refused with the reason rather than half served. Wayland goes through the desktop portals, which
// this build does not have yet.
var ErrWayland = errors.New("this is a Wayland session: computer use supports X11 sessions only for now")

func open(ctx context.Context) (backend.Backend, error) {
	if os.Getenv("XDG_SESSION_TYPE") == "wayland" || (os.Getenv("WAYLAND_DISPLAY") != "" && os.Getenv("XDG_SESSION_TYPE") != "x11") {
		return nil, ErrWayland
	}
	if os.Getenv("DISPLAY") == "" {
		return nil, errors.New("no X display: DISPLAY is not set")
	}
	x, err := x11.Open(ctx, "")
	if err != nil {
		return nil, err
	}
	return x, nil
}
