//go:build !(darwin && cgo)

package selftest

// mainLoop is nil here: only macOS's AppKit needs the main thread to run an event loop for the test
// window, and the other systems' windows run their own.
var mainLoop func(stop <-chan struct{})
