package browser

import (
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The daemon's error codes beyond ptyd's. The numbers are the contract; the messages are for the
// agent that reads them, so they say what to do next.
const (
	CodeHumanDriving   = 1101
	CodeBlocked        = 1102
	CodeStaleRef       = 1103
	CodeNoSuchTab      = 1104
	CodeFieldForbidden = 1105
	CodePaused         = 1106
	CodeDialogOpen     = 1107
	CodeBrowserGone    = 1108
)

func errWith(code int, data any, format string, args ...any) *wire.Error {
	e := wire.Errorf(code, format, args...)
	e.Data = data
	return e
}

// ErrHumanDriving is what an agent's call gets after waiting for a person to give control back.
func ErrHumanDriving(c Control) *wire.Error {
	return errWith(CodeHumanDriving, c.View(""),
		"The operator has taken control of this browser. Do not act on it; end your turn or do other work. "+
			"You will get a message when they hand it back.")
}

// ErrPaused is what an agent's call gets while the operator has paused it.
func ErrPaused(reason string) *wire.Error {
	return errWith(CodePaused, map[string]any{"reason": reason}, "The operator paused the agent in this browser.")
}

func errNoSuchTab(id string) *wire.Error {
	return errWith(CodeNoSuchTab, map[string]any{"tab_id": id}, "no tab %q in this browser; list the tabs", id)
}

// ErrDetached is a call to a tab whose debugger was detached (Tab.Detached): the tab is still open in
// the operator's Chrome, and still listed and closable, but nothing on it can be read or done.
func ErrDetached(t *Tab) *wire.Error {
	return errWith(CodeNoSuchTab, map[string]any{"tab_id": t.ID, "reason": "detached"},
		"tab %s no longer answers: its debugger was detached (it opened a page the extension cannot drive, such as a PDF viewer "+
			"or a browser page); close it with tab.close and open another tab", t.ID)
}

func errBrowserGone(reason string) *wire.Error {
	return errWith(CodeBrowserGone, map[string]any{"reason": reason},
		"the browser behind this group has exited (%s); open it again", reason)
}

// ErrDialogOpen names the dialog that blocks the page.
func ErrDialogOpen(d *Dialog) *wire.Error {
	return errWith(CodeDialogOpen, map[string]any{"dialog": d},
		"the page shows a %s dialog (%q); answer it with dialog.answer first", d.Type, d.Message)
}

func errLimit(limit string, max int, format string, args ...any) *wire.Error {
	return errWith(wire.CodeLimit, map[string]any{"limit": limit, "max": max}, format, args...)
}
