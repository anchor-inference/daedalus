package main

import (
	"context"
	"sync"
)

// Surface is where the operator sees Daedalus. Under the desktop application it is the shell's
// window, and every address the launcher wants shown is handed to the shell (shell.go): the app
// never opens in a browser. A launcher started on its own — a build from source, a server, a
// terminal with no shell beside the executable — has no window, and there it falls back to what
// the machine has:
//
//  1. a Chromium-family browser in application mode, which is a window with no browser around it;
//  2. the default browser, which every machine has.
type Surface struct {
	paths Paths
	shell *shellLink

	mu      sync.Mutex
	current string
}

// OpenSurface points whatever shows the launcher at its own page: the status, the buttons and — on
// a first start — the questions. The app comes later, when the stack answers.
func OpenSurface(paths Paths, launcher string, link *shellLink) *Surface {
	surface := &Surface{paths: paths, shell: link, current: launcher}
	if link != nil {
		link.emit(shellEvent{Event: "show", URL: launcher})
	}
	return surface
}

// Windowed reports whether this start has a window of its own, which is what the status page says
// out loud so that "closing this leaves the stack running" is read about the right thing.
func (s *Surface) Windowed() bool { return s != nil && s.shell != nil }

// Show puts a URL in front of the operator: in the shell's window, or without one in an
// application-mode browser window or a tab.
func (s *Surface) Show(ctx context.Context, url string) {
	if s == nil || url == "" {
		return
	}
	s.mu.Lock()
	s.current = url
	s.mu.Unlock()
	if s.shell != nil {
		s.shell.emit(shellEvent{Event: "show", URL: url})
		return
	}
	if err := OpenAppWindow(s.paths, url); err == nil {
		return
	}
	_ = OpenBrowser(ctx, url)
}

// Focus is what a second start of the application asks this one to do: come to the front, and go
// to the link the second start was given if it was given one. Without a window there is no window
// to raise, and opening the URL again is what raises the browser instead.
func (s *Surface) Focus(ctx context.Context, url string) {
	if s == nil {
		return
	}
	if s.shell != nil {
		s.shell.emit(shellEvent{Event: "focus", URL: url})
		return
	}
	if url == "" {
		s.mu.Lock()
		url = s.current
		s.mu.Unlock()
	}
	s.Show(ctx, url)
}

// Run holds the launcher open until the context ends: the shell closing its window, or Ctrl+C in
// a terminal. Containers started in Docker mode are detached and stay up either way.
func (s *Surface) Run(ctx context.Context) {
	<-ctx.Done()
}
