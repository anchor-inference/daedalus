package desk

import (
	"context"
	"math"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
)

// inputJitter is how much the system's last-input time may move between two reads with no input at
// all: it is computed from an idle counter in milliseconds, read at slightly different moments.
const inputJitter = 50 * time.Millisecond

// watchHuman looks once for the operator's hand: the pointer away from where the daemon left it, or
// input the system saw later than the daemon's own. Either pauses the agent at once; the pause ends
// when the operator resumes it or, if the configuration allows, after a quiet period with no input.
//
// It does not look while a step of an action is sending input, since the daemon's own events would
// be taken for the operator's; between the steps of a drag, a typed text or a wait it does, and the
// action stops at its next step.
func (d *Desk) watchHuman(ctx context.Context) {
	d.mu.Lock()
	if d.injecting || d.lease == nil {
		d.mu.Unlock()
		return
	}
	ourX, ourY, known, lastInject, lastSeen := d.ourX, d.ourY, d.ourKnown, d.lastInject, d.lastSeenInput
	serial := d.injectSerial
	d.mu.Unlock()

	human, how := false, ""
	cx, cy, err := d.be.Cursor(ctx)
	if err == nil && known && (math.Abs(cx-ourX) > humanTolerance || math.Abs(cy-ourY) > humanTolerance) {
		human, how = true, "pointer"
	}
	at, ok := d.be.LastInput(ctx)
	if ok && at.After(lastInject.Add(humanGrace)) && at.After(lastSeen.Add(inputJitter)) {
		human, how = true, "input"
	}

	now := d.now()
	d.mu.Lock()
	if d.injecting || d.injectSerial != serial || d.lease == nil {
		// A step sent input while the watch was reading: what it read may be the step's own.
		d.mu.Unlock()
		return
	}
	if err == nil {
		d.ourX, d.ourY, d.ourKnown = cx, cy, true
	}
	if ok && at.After(d.lastSeenInput) {
		d.lastSeenInput = at
	}
	leaseID := d.lease.ID
	newly, resumed := false, false
	if human {
		d.lastHuman = now
		if !d.paused {
			d.paused, d.pausedAt, newly = true, now, true
			if d.actCancel != nil {
				d.actCancel()
			}
		}
	} else if d.paused {
		if q := *d.cfg.HumanQuietMs; q > 0 && now.Sub(d.lastHuman) >= time.Duration(q)*time.Millisecond {
			d.resumeLocked()
			resumed = true
		}
	}
	state := d.humanStateLocked()
	d.mu.Unlock()

	if newly {
		d.events.Publish("human.input", "", map[string]any{"lease_id": leaseID, "how": how, "human": state})
		d.write(audit.Record{Role: "daemon", Event: "human.input", Lease: leaseID, Reason: how})
	}
	if resumed {
		d.events.Publish("human.resumed", "", map[string]any{"by": "quiet", "lease_id": leaseID})
	}
}
