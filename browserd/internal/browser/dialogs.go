package browser

import (
	"context"
	"time"
	"unicode/utf8"
)

// AutoDialog is a page dialog the daemon answered by itself: an alert or a beforeunload question
// that came up while the agent held the page. Seq counts them per tab, so a reply that reports the
// ones its call met and the event that announces each can be matched by the host and told once.
type AutoDialog struct {
	Seq     int64  `json:"seq"`
	Type    string `json:"type"`
	Message string `json:"message"`
	URL     string `json:"url,omitempty"`
	At      int64  `json:"at"`
}

// autoDialogsKept bounds what a tab remembers of the dialogs it answered: enough for any one call
// to report, few enough that a page alerting in a loop costs nothing.
const autoDialogsKept = 20

// autoMessageMax bounds a dialog's text as it is kept and told; the page writes it.
const autoMessageMax = 1000

// autoAnswered says whether a dialog of this kind is answered without asking anyone. An alert has
// one answer, and it only stops the page until someone gives it: the agent spent a call on every
// "Saved!" and a page that alerted behind its back stopped every later call with 1107. A
// beforeunload question comes from the agent's own navigation, reload or closing, which is what it
// asked for. A confirm or a prompt is a decision ("Delete this?"), and stays the agent's.
func autoAnswered(kind string) bool {
	return kind == "alert" || kind == "beforeunload"
}

// autoDialog answers a dialog the daemon need not ask about, when the agent holds the page. While a
// person drives, or the agent is paused for one, the dialog is theirs to see and answer, as any
// other. It reports whether it took the dialog on; the answer itself is sent on its own goroutine,
// since this runs on the browser's event goroutine, which must not wait on the pipe.
func (t *Tab) autoDialog(d *Dialog, url string) bool {
	if !autoAnswered(d.Type) || t.Group.Control().Owner != OwnerAgent {
		return false
	}
	msg := d.Message
	if len(msg) > autoMessageMax {
		msg = msg[:autoMessageMax]
		for !utf8.ValidString(msg) {
			msg = msg[:len(msg)-1]
		}
	}
	t.mu.Lock()
	t.autoSeq++
	a := AutoDialog{Seq: t.autoSeq, Type: d.Type, Message: msg, URL: url, At: time.Now().UnixMilli()}
	t.auto = append(t.auto, a)
	if len(t.auto) > autoDialogsKept {
		t.auto = append([]AutoDialog(nil), t.auto[len(t.auto)-autoDialogsKept:]...)
	}
	t.autoOpen = true
	t.mu.Unlock()
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		err := t.Call(ctx, "Page.handleJavaScriptDialog", map[string]any{"accept": true}, nil)
		if err == nil {
			return
		}
		// Unanswered, the page stays stopped: better the agent sees the dialog and answers it than
		// every call it makes waits on a page that will never run again.
		t.mu.Lock()
		stuck := t.autoOpen && !t.closed
		if stuck {
			t.autoOpen = false
			t.dialog = d
			t.broadcastLocked()
		}
		t.mu.Unlock()
		if stuck {
			t.Group.m.log.Warn("answering a page's dialog", "tab", t.ID, "type", d.Type, "error", err.Error())
			t.Group.m.publish("dialog.opened", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "type": d.Type,
				"message": d.Message, "default_prompt": d.DefaultPrompt})
		}
	}()
	t.Group.m.publish("dialog.auto", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID, "seq": a.Seq, "type": a.Type,
		"message": a.Message, "url": a.URL, "accepted": true})
	return true
}

// AutoDialogMark is where the tab's count of answered dialogs stands, for AutoDialogsSince.
func (t *Tab) AutoDialogMark() int64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.autoSeq
}

// AutoDialogsSince are the dialogs the daemon answered on this tab after mark, oldest first.
func (t *Tab) AutoDialogsSince(mark int64) []AutoDialog {
	t.mu.Lock()
	defer t.mu.Unlock()
	var out []AutoDialog
	for _, a := range t.auto {
		if a.Seq > mark {
			out = append(out, a)
		}
	}
	return out
}
