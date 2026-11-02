module github.com/ascorblack/daedalus/cli

// The Go of ptyd, whose side channels this module serves on the operator's machine.
go 1.26.0

require (
	// exec.run and fs.*, the same handlers the terminal daemon serves, run here in the folder the
	// operator started the client in.
	github.com/ascorblack/daedalus/ptyd v0.0.0
	// The device connection: an outbound WebSocket, context-aware and without dependencies of its own.
	github.com/coder/websocket v1.8.14
	// The pairing QR code printed in the terminal.
	rsc.io/qr v0.2.0
)

require (
	github.com/creack/pty v1.1.24 // indirect
	golang.org/x/sys v0.48.0 // indirect
)

replace github.com/ascorblack/daedalus/ptyd => ../ptyd
