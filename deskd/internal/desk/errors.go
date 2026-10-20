package desk

import (
	"errors"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The daemon's error codes, beside ptyd's (wire.Code*). The numbers are the contract
// (docs/architecture/computer.md); 1101 means what it means to browserd.
const (
	CodeHumanDriving = 1101 // the operator touched the mouse or keyboard; actions wait for them
	CodeNoLease      = 1200 // no lease, an expired or revoked one, or one held by another session
	CodeStale        = 1201 // the screenshot id is unknown or no longer matches the desktop
	CodeOutOfScope   = 1202 // the point or the keyboard's window is outside what the lease covers
	CodeProtected    = 1203 // a window of an app the daemon never touches (Daedalus itself)
	CodeSecure       = 1204 // a locked screen or a secure desktop
	CodeNoPermission = 1205 // the system has not granted a permission the call needs
	CodeRate         = 1206 // too many actions: per second, or for the lease
	CodeUnreachable  = 1207 // the system silently drops input to that window (an elevated program)
	CodeOperatorOnly = 1208 // a method only the operator's token may call
)

func refuse(code int, msg string, data any) *wire.Error {
	return &wire.Error{Code: code, Message: msg, Data: data}
}

// fromBackend turns a backend's typed errors into the contract's codes.
func fromBackend(err error) error {
	if err == nil {
		return nil
	}
	var pe *backend.PermissionError
	if errors.As(err, &pe) {
		return refuse(CodeNoPermission, pe.Error(), map[string]any{"which": pe.Which, "hint": PermissionHint(pe.Which)})
	}
	var ue *backend.UnreachableError
	if errors.As(err, &ue) {
		return refuse(CodeUnreachable, ue.Error(), map[string]any{"reason": ue.Reason})
	}
	if errors.Is(err, backend.ErrUnsupported) {
		return wire.Errorf(wire.CodeUnsupported, "%v", err)
	}
	var we *wire.Error
	if errors.As(err, &we) {
		return we
	}
	return err
}

// PermissionHint is what the operator is told about a missing grant. On macOS a grant belongs to the
// exact build while the app is signed ad hoc, so after every update System Settings may still show
// Daedalus switched on while the system has forgotten it: the cure is to remove the entry and add it
// again, and the operator must be told so rather than told to switch on what looks on already.
func PermissionHint(which string) string {
	switch which {
	case backend.PermScreen:
		return "Allow Screen Recording for Daedalus in System Settings → Privacy & Security. If it already looks allowed, the grant belongs to an earlier build: remove Daedalus from the list and add it again."
	case backend.PermAccessibility:
		return "Allow Accessibility for Daedalus in System Settings → Privacy & Security. If it already looks allowed, the grant belongs to an earlier build: remove Daedalus from the list and add it again."
	case backend.PermInputMonitoring:
		return "Allow Input Monitoring for Daedalus in System Settings → Privacy & Security to let the daemon notice your own typing."
	}
	return ""
}
