package rpc

import (
	"context"
	"encoding/json"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
)

// sample returns a measurement of the running terminals no older than maxAge, taking a new one when
// the last is older. Only running terminals are measured: an exited one has no processes left.
func (d *Daemon) sample(ids map[string]bool, maxAge time.Duration) procstat.Sample {
	d.statsMu.Lock()
	defer d.statsMu.Unlock()
	now := time.Now().UTC()
	if d.lastStat == nil || now.Sub(d.lastStat.At) > maxAge {
		var terms []procstat.Term
		for _, t := range d.Registry.List() {
			if t.Running() {
				terms = append(terms, procstat.Term{ID: t.ID, Pid: t.Pid, Tag: procenv.TagName + "=" + t.ID})
			}
		}
		s := d.sampler.Sample(terms, now)
		d.lastStat = &s
	}
	out := *d.lastStat
	out.Terminals = nil
	for _, ts := range d.lastStat.Terminals {
		if ids != nil && !ids[ts.ID] {
			continue
		}
		if t, err := d.Registry.Get(ts.ID); err == nil {
			ts.DaemonBytes = t.DaemonBytes()
		}
		out.Terminals = append(out.Terminals, ts)
	}
	if out.Terminals == nil {
		out.Terminals = []procstat.TermStats{}
	}
	return out
}

func (d *Daemon) stats(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		IDs []string `json:"ids"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	var ids map[string]bool
	if p.IDs != nil {
		ids = map[string]bool{}
		for _, id := range p.IDs {
			ids[id] = true
		}
	}
	// A sample younger than two seconds is fresh enough for a person looking at a bar, and reusing it
	// keeps a busy client from turning every call into a walk of the process table.
	return d.sample(ids, 2*time.Second), nil
}

// RunStats publishes a `terminal.stats` event every interval while any terminal runs. The event is
// one measurement of all of them, so its cost does not grow with the number of subscribers.
func (d *Daemon) RunStats(stop <-chan struct{}) {
	tick := time.NewTicker(config.StatsInterval)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			if running, _ := d.Registry.Counts(); running == 0 {
				continue
			}
			s := d.sample(nil, 0)
			d.Events.Publish("terminal.stats", "", s)
		}
	}
}
