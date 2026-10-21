// Package quartz is the macOS desktop: CoreGraphics for displays, capture and synthetic input, the
// window server's window list, and the Accessibility API where raising one window needs it.
//
// Desktop units are Quartz's global display coordinates: points, the origin at the top-left corner
// of the primary display, y growing down, the same space CGEventPost, CGDisplayBounds and the window
// list all use, so nothing here flips an axis. A Retina display has two captured pixels to a point.
//
// macOS grants Screen Recording, Accessibility and Input Monitoring to the "responsible process",
// which for a helper started by an app is the app itself, but only while the helper stays its direct
// child. deskd must therefore never daemonise, double-fork or be started through launchd: it would
// become a program of its own in the privacy settings and every grant the operator gave Daedalus
// would stop applying to it.
//
// The app is signed ad hoc, so each update can look like a new program to the privacy database: the
// settings keep showing it as allowed while the probes say no. The backend never prompts on its own;
// it reports the missing grant with a PermissionError and the hint in StaleGrantHint, and only
// RequestPermission, which the operator's own flow calls, raises a system prompt.
//
// Everything that needs cgo is in the files built for darwin with cgo; the rest is plain Go so its
// tables and arithmetic are tested on any machine.
package quartz
