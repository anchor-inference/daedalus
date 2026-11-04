package rpc

import (
	"errors"
	"fmt"
	"testing"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// A page command whose pipe ended under it is the browser gone, however the page package wrapped it:
// a snapshot whose relay dropped once came back as a tab that was not there, naming none.
func TestAPipeThatClosedUnderACallIsTheBrowserGone(t *testing.T) {
	for _, err := range []error{
		cdp.ErrClosed,
		fmt.Errorf("DOMDebugger.getEventListeners: %w", cdp.ErrClosed),
		wire.Errorf(wire.CodeInternal, "snapshot: %v", fmt.Errorf("x: %w", cdp.ErrClosed)),
	} {
		if !pipeClosed(err) {
			t.Errorf("%v is not read as a closed pipe", err)
		}
	}
	for _, err := range []error{
		errors.New("no tab \"t3\" in this browser"),
		wire.Errorf(browser.CodeBrowserGone, "the browser behind this group has exited (%s)", cdp.ErrClosed),
	} {
		if pipeClosed(err) {
			t.Errorf("%v is read as a closed pipe", err)
		}
	}
}
