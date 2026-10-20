package browser

import (
	"os"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/procstat"
)

// BrowserStats is one browser's cost.
type BrowserStats struct {
	ID         string  `json:"id"`
	Pid        int     `json:"pid"`
	Processes  int     `json:"processes"`
	RSSBytes   int64   `json:"rss_bytes"`
	CPUPercent float64 `json:"cpu_percent"`
	Tabs       int     `json:"tabs"`
}

// Stats is browser.stats's result. MemoryBasis says what the browsers' rss_bytes are: "cgroup" (the
// kernel's own charge for the daemon's cgroup, shared out over the browsers), "private" (the sum of
// each process's anonymous and shared pages) or "rss" (the resident size, where nothing better can
// be read).
type Stats struct {
	At          time.Time            `json:"at"`
	Supported   bool                 `json:"supported"`
	MemoryBasis string               `json:"memory_basis"`
	Browsers    []BrowserStats       `json:"browsers"`
	Daemon      procstat.DaemonStats `json:"daemon"`
	Machine     procstat.Machine     `json:"machine"`
}

// maxForeignProcs is how many processes that are neither the daemon's browsers nor the daemon may
// share its cgroup for the cgroup's figure to be used. A container or the launcher's scope holds the
// daemon, its init and a health check; a login session's cgroup holds everything the operator runs,
// and subtracting all of that from its total would leave noise.
const maxForeignProcs = 64

// Sample measures every browser.
//
// Memory is the question "what would closing this browser free". The resident size answers it
// worst: it counts Chromium's shared code once per process, four to five times over for a browser of
// a dozen processes. Each process's anonymous and shared pages (chrome.PrivateBytes) still count the
// pages a renderer shares with the zygote it was forked from once per renderer, and read 1.4–1.8
// times what the kernel charged (measured: 611 MB against 385 MB for four tabs). So where the daemon
// has a cgroup of its own — the container, or the launcher's scope — the kernel's charge for it,
// less what the other processes in it hold, is the browsers' memory, shared out over them in
// proportion to their private figures; elsewhere the private figure stands.
func (m *Manager) Sample(s *procstat.Sampler, now time.Time) Stats {
	bs := m.ownBrowsers()
	terms := make([]procstat.Term, len(bs))
	for i, b := range bs {
		terms[i] = procstat.Term{ID: b.ID, Pid: b.Pid()}
	}
	sample := s.Sample(terms, now)
	out := Stats{At: sample.At, Supported: sample.Supported, Daemon: sample.Daemon, Machine: sample.Machine,
		Browsers: make([]BrowserStats, 0, len(bs)), MemoryBasis: "rss"}
	var table map[int]procstat.Proc
	if procstat.Supported {
		table = procstat.Table()
	}
	inTree := map[int]bool{}
	allPrivate := table != nil
	for i, b := range bs {
		ts := sample.Terminals[i]
		st := BrowserStats{ID: b.ID, Pid: ts.Pid, Processes: ts.Processes, RSSBytes: ts.RSSBytes, CPUPercent: ts.CPUPercent}
		if table != nil {
			var private int64
			ok := true
			for _, p := range procstat.Tree(table, b.Pid()) {
				inTree[p.Pid] = true
				v := chrome.PrivateBytes(p.Pid)
				if v < 0 {
					ok = false
					continue
				}
				private += v
			}
			if ok {
				st.RSSBytes = private
			} else {
				allPrivate = false
			}
		}
		for _, g := range b.groupList() {
			st.Tabs += len(g.Tabs())
		}
		out.Browsers = append(out.Browsers, st)
	}
	if allPrivate && len(bs) > 0 {
		out.MemoryBasis = "private"
		if cg, ok := chrome.OwnCgroup(); ok {
			var others int64
			foreign := 0
			for _, pid := range cg.Procs {
				if inTree[pid] {
					continue
				}
				if pid != os.Getpid() {
					foreign++
				}
				others += max(0, chrome.PrivateBytes(pid))
			}
			if foreign <= maxForeignProcs {
				estimates := make([]int64, len(out.Browsers))
				for i, st := range out.Browsers {
					estimates[i] = st.RSSBytes
				}
				if fitted, ok := fitToCgroup(estimates, others, cg.AnonShmem); ok {
					for i := range out.Browsers {
						out.Browsers[i].RSSBytes = fitted[i]
					}
					out.MemoryBasis = "cgroup"
				}
			}
		}
	}
	if self, ok := table[os.Getpid()]; ok {
		if v := chrome.PrivateBytes(self.Pid); v >= 0 {
			out.Daemon.RSSBytes = v
		}
	}
	return out
}

// ownBrowsers are the browsers whose processes are the daemon's: the operator's Chrome is neither
// measured as the daemon's cost nor ever ended for the memory limit.
func (m *Manager) ownBrowsers() []*Browser {
	var out []*Browser
	for _, b := range m.Browsers() {
		if !b.attached {
			out = append(out, b)
		}
	}
	return out
}

// fitToCgroup shares what the kernel charged the cgroup, less what its other processes hold, over
// the browsers in proportion to their private estimates. It is false when there is nothing to share
// or the figures contradict each other (the others alone hold more than was charged), and the
// estimates then stand.
func fitToCgroup(estimates []int64, others, charged int64) ([]int64, bool) {
	var sum int64
	for _, e := range estimates {
		sum += e
	}
	browsers := charged - others
	if sum <= 0 || browsers <= 0 {
		return estimates, false
	}
	out := make([]int64, len(estimates))
	for i, e := range estimates {
		out[i] = int64(float64(e) * float64(browsers) / float64(sum))
	}
	return out, true
}

// RunStats publishes browser.stats every interval while a browser runs, ends a browser past the hard
// memory limit, and asks the first renderer whether it runs sandboxed.
func (m *Manager) RunStats(stop <-chan struct{}) {
	s := procstat.NewSampler()
	tick := time.NewTicker(config.StatsInterval)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
		}
		bs := m.ownBrowsers()
		if len(bs) == 0 {
			continue
		}
		st := m.Sample(s, time.Now())
		m.publish("browser.stats", st)
		for _, b := range bs {
			m.checkSandbox(b)
		}
		for _, x := range st.Browsers {
			if m.lim.MemoryHardBytes > 0 && x.RSSBytes > m.lim.MemoryHardBytes {
				for _, b := range bs {
					if b.ID == x.ID {
						m.log.Warn("browser over its memory limit", "browser", b.ID, "bytes", x.RSSBytes)
						b.setClosing("memory")
						go b.proc.Kill()
					}
				}
			}
		}
	}
}

// probeSandbox asks b's renderers about the sandbox until one answers, a quarter of a second apart,
// for as long as the first page takes to come up. Waiting for the ten-second statistics instead left
// daemon.info saying "unknown" for a browser that had been running, and a doctor asked in between
// could not say whether the agent's pages were sandboxed.
func (m *Manager) probeSandbox(b *Browser) {
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	deadline := time.After(30 * time.Second)
	for {
		m.checkSandbox(b)
		if m.Sandbox() != "unknown" {
			return
		}
		select {
		case <-tick.C:
		case <-deadline:
			return
		case <-b.gone:
			return
		}
	}
}

// checkSandbox records whether a renderer of b runs under the seccomp filter, once there is one.
func (m *Manager) checkSandbox(b *Browser) {
	m.mu.Lock()
	known := m.sandbox != "unknown"
	m.mu.Unlock()
	if known || !procstat.Supported {
		return
	}
	var pids []int
	for _, p := range procstat.Tree(procstat.Table(), b.Pid()) {
		pids = append(pids, p.Pid)
	}
	sandboxed, ok := chrome.RendererSandboxed(pids)
	if !ok {
		return
	}
	m.mu.Lock()
	if sandboxed {
		m.sandbox = "ok"
	} else {
		m.sandbox = "a renderer runs without its seccomp filter"
	}
	m.mu.Unlock()
}
