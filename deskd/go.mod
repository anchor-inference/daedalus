module github.com/ascorblack/daedalus/deskd

// The Go of ptyd, whose shared packages this module builds against.
go 1.26.0

require (
	// The socket framing, the run directory and handshake and the event log, shared with the terminal
	// and browser daemons so the host speaks one protocol to all three.
	github.com/ascorblack/daedalus/ptyd v0.0.0
	// The X11 protocol in pure Go: XTest input, image capture, RandR displays and the screen saver's
	// idle time, with no libX11 to link, so the Linux binary stays static and cross-compiles.
	github.com/jezek/xgb v1.1.1
	// Windows: SendInput, the GDI capture and the window and process calls, as for ptyd.
	golang.org/x/sys v0.48.0
)

replace github.com/ascorblack/daedalus/ptyd => ../ptyd
