//go:build linux

package containment

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const cpuPeriod = 100000

type Controller struct {
	root        string
	instance    string
	mu          sync.Mutex
	groups      map[string]*group
	probeMu     sync.Mutex
	probeAt     time.Time
	probeStatus Status
}

type group struct {
	controller   *Controller
	path         string
	file         *os.File
	scope        Scope
	limits       Limits
	refs         int
	sealed       bool
	acknowledged bool
}

// NewController locates this daemon's own cgroup. It never creates a child in another service's
// cgroup or accepts a filesystem path from the host.
func NewController(instance string) (*Controller, error) {
	root, err := currentRoot()
	if err != nil {
		return nil, err
	}
	return &Controller{root: root, instance: instance, groups: map[string]*group{}}, nil
}

// NewTestController uses a scratch cgroup-shaped directory to test validation and state parsing.
// It cannot make a regular directory enforce a kernel limit.
func NewTestController(root, instance string) *Controller {
	return &Controller{root: root, instance: instance, groups: map[string]*group{}}
}

func currentRoot() (string, error) {
	data, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return "", fmt.Errorf("own cgroup: %w", err)
	}
	cgroup := ""
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "0::/") {
			cgroup = strings.TrimPrefix(line, "0::")
			break
		}
	}
	if cgroup == "" {
		return "", fmt.Errorf("%w: no unified cgroup", ErrUnavailable)
	}
	mounts, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return "", fmt.Errorf("cgroup mount: %w", err)
	}
	defer mounts.Close()
	scan := bufio.NewScanner(mounts)
	for scan.Scan() {
		parts := strings.SplitN(scan.Text(), " - ", 2)
		if len(parts) != 2 || !strings.HasPrefix(parts[1], "cgroup2 ") {
			continue
		}
		fields := strings.Fields(parts[0])
		if len(fields) < 5 {
			continue
		}
		mountRoot, mountPoint := fields[3], fields[4]
		if cgroup != mountRoot && !strings.HasPrefix(cgroup, strings.TrimSuffix(mountRoot, "/")+"/") {
			continue
		}
		relative := strings.TrimPrefix(strings.TrimPrefix(cgroup, mountRoot), "/")
		path := filepath.Join(mountPoint, relative)
		if _, err := os.Stat(filepath.Join(path, "cgroup.procs")); err == nil {
			return path, nil
		}
	}
	if err := scan.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("%w: own cgroup mount not found", ErrUnavailable)
}

func (c *Controller) available() error {
	data, err := os.ReadFile(filepath.Join(c.root, "cgroup.subtree_control"))
	if err != nil {
		return fmt.Errorf("%w: delegation is not readable: %v", ErrUnavailable, err)
	}
	delegated := map[string]bool{}
	for _, name := range strings.Fields(string(data)) {
		delegated[name] = true
	}
	for _, need := range []string{"cpu", "memory", "pids"} {
		if !delegated[need] {
			return fmt.Errorf("%w: %s controller is not delegated", ErrUnavailable, need)
		}
	}
	if _, err := os.Stat(filepath.Join(c.root, "cgroup.kill")); err != nil {
		return fmt.Errorf("%w: cgroup.kill is unavailable", ErrUnavailable)
	}
	return nil
}

// Probe proves both writable delegation and atomic CLONE_INTO_CGROUP with a short-lived subprocess.
func (c *Controller) Probe() Status {
	c.probeMu.Lock()
	defer c.probeMu.Unlock()
	if time.Since(c.probeAt) < 30*time.Second {
		return c.probeStatus
	}
	status := c.probe()
	c.probeStatus, c.probeAt = status, time.Now()
	return status
}

func (c *Controller) probe() Status {
	if err := c.available(); err != nil {
		return Status{Kind: "cgroup_v2", Reason: err.Error()}
	}
	path, err := os.MkdirTemp(c.root, "daedalus-probe-")
	if err != nil {
		return Status{Kind: "cgroup_v2", Reason: fmt.Sprintf("delegated child: %v", err)}
	}
	defer os.Remove(path)
	if err := install(path, Limits{MemoryBytes: 32 << 20, CPUMillis: 1000, ProcessCount: 8}); err != nil {
		return Status{Kind: "cgroup_v2", Reason: fmt.Sprintf("installing limits: %v", err)}
	}
	file, err := os.Open(path)
	if err != nil {
		return Status{Kind: "cgroup_v2", Reason: fmt.Sprintf("opening handle: %v", err)}
	}
	defer file.Close()
	cmd := exec.Command("/bin/true")
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: int(file.Fd())}
	if err := cmd.Run(); err != nil {
		return Status{Kind: "cgroup_v2", Reason: fmt.Sprintf("atomic process placement: %v", err)}
	}
	return Status{Kind: "cgroup_v2", Available: true}
}

func install(path string, limits Limits) error {
	if limits.DiskBytes != 0 {
		return fmt.Errorf("%w: cgroup v2 has no disk-space quota", ErrUnavailable)
	}
	if limits.MemoryBytes < 0 || limits.CPUMillis < 0 || limits.ProcessCount < 0 ||
		limits.MemoryBytes > 1<<50 || limits.CPUMillis > 100000 || limits.ProcessCount > 65536 ||
		limits.MemoryBytes > 0 && limits.MemoryBytes < 16<<20 ||
		limits.CPUMillis > 0 && limits.CPUMillis < 10 {
		return errors.New("resource ceilings are outside supported bounds")
	}
	memory, cpu, processes := "max", fmt.Sprintf("max %d", cpuPeriod), "max"
	if limits.MemoryBytes > 0 {
		memory = strconv.FormatInt(limits.MemoryBytes, 10)
	}
	if limits.CPUMillis > 0 {
		cpu = fmt.Sprintf("%d %d", limits.CPUMillis*cpuPeriod/1000, cpuPeriod)
	}
	if limits.ProcessCount > 0 {
		processes = strconv.FormatInt(limits.ProcessCount, 10)
	}
	settings := map[string]string{
		"memory.max":       memory,
		"memory.oom.group": "1",
		"cpu.max":          cpu,
		"pids.max":         processes,
	}
	for name, value := range settings {
		if err := os.WriteFile(filepath.Join(path, name), []byte(value), 0o600); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
	}
	return nil
}

func key(scope Scope) string {
	sum := sha256.Sum256([]byte(scope.AttemptID + "\x00" + scope.HostGeneration + "\x00" + scope.LaunchID))
	return hex.EncodeToString(sum[:16])
}

// Preflight proves these particular ceilings can be installed before the host commits a launch.
func (c *Controller) Preflight(limits Limits) error {
	if err := c.available(); err != nil {
		return err
	}
	path, err := os.MkdirTemp(c.root, "daedalus-resource-check-")
	if err != nil {
		return err
	}
	defer os.Remove(path)
	return install(path, limits)
}

func valid(scope Scope) bool {
	for _, value := range []string{scope.AttemptID, scope.LaunchID} {
		if len(value) < 8 || len(value) > 64 {
			return false
		}
		for _, ch := range value {
			if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '-' || ch == '_') {
				return false
			}
		}
	}
	if len(scope.HostGeneration) < 1 || len(scope.HostGeneration) > 20 {
		return false
	}
	for _, ch := range scope.HostGeneration {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}

// Claim creates the attempt's single parent cgroup before its first companion or main process.
// Subsequent terminals must present the same scope and the same immutable ceilings.
func (c *Controller) Claim(scope Scope, limits Limits) (Handle, error) {
	if !valid(scope) || c.instance == "" {
		return nil, errors.New("invalid attempt containment identity")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	id := key(scope)
	if existing := c.groups[id]; existing != nil {
		if existing.scope != scope || existing.limits != limits {
			return nil, errors.New("attempt containment limits changed")
		}
		if existing.sealed || existing.acknowledged {
			return nil, errors.New("attempt containment was stopped")
		}
		existing.refs++
		return existing, nil
	}
	if err := c.available(); err != nil {
		return nil, err
	}
	path := filepath.Join(c.root, "daedalus-"+c.instance+"-"+id)
	if err := os.Mkdir(path, 0o700); err != nil {
		return nil, fmt.Errorf("creating attempt containment: %w", err)
	}
	if err := install(path, limits); err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	file, err := os.Open(path)
	if err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	claimed := &group{controller: c, path: path, file: file, scope: scope, limits: limits, refs: 1}
	c.groups[id] = claimed
	return claimed, nil
}

// Lookup accepts only a handle already created by this daemon instance. It never reconstructs a
// stale cgroup from a path or attempts to kill a similarly named process after restart.
func (c *Controller) Lookup(scope Scope) (Handle, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	found := c.groups[key(scope)]
	if found == nil || found.scope != scope {
		return nil, ErrUnavailable
	}
	found.refs++
	return found, nil
}

func (g *group) FD() int        { return int(g.file.Fd()) }
func (g *group) Scope() Scope   { return g.scope }
func (g *group) Limits() Limits { return g.limits }

func readNumber(path string) int64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	value, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil {
		return -1
	}
	return value
}

func readKey(path, wanted string) int64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == wanted {
			value, err := strconv.ParseInt(fields[1], 10, 64)
			if err == nil {
				return value
			}
		}
	}
	return -1
}

func (g *group) Observe() (Evidence, error) {
	populated := readKey(filepath.Join(g.path, "cgroup.events"), "populated")
	if populated < 0 {
		return Evidence{}, fmt.Errorf("attempt containment handle is unreadable")
	}
	return Evidence{Kind: "cgroup_v2", Enforced: true, Populated: populated != 0,
		MemoryPeakBytes: readNumber(filepath.Join(g.path, "memory.peak")),
		CPUUsageUsec:    readKey(filepath.Join(g.path, "cpu.stat"), "usage_usec"),
		Processes:       readNumber(filepath.Join(g.path, "pids.current")),
		OOMKills:        readKey(filepath.Join(g.path, "memory.events"), "oom_kill"),
		PIDsMaxEvents:   readKey(filepath.Join(g.path, "pids.events"), "max")}, nil
}

// Kill acts only through this open attempt handle. A PID or process group is never the authority.
func (g *group) Kill(grace time.Duration) (Evidence, error) {
	g.controller.mu.Lock()
	g.sealed = true
	g.controller.mu.Unlock()
	if err := os.WriteFile(filepath.Join(g.path, "cgroup.kill"), []byte("1"), 0o600); err != nil {
		return Evidence{}, err
	}
	deadline := time.Now().Add(grace)
	for {
		result, err := g.Observe()
		if err != nil || !result.Populated {
			return result, err
		}
		if time.Now().After(deadline) {
			return result, fmt.Errorf("attempt containment still has processes")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func (g *group) Close() error {
	g.controller.mu.Lock()
	defer g.controller.mu.Unlock()
	if g.refs > 0 {
		g.refs--
	}
	if g.refs != 0 || !g.acknowledged {
		return nil
	}
	return g.removeEmpty()
}

// Release follows a durable host observation. An exited main process without this acknowledgement
// is retained so a lost stop response can still be reconciled against the exact handle.
func (c *Controller) Release(scope Scope) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	g := c.groups[key(scope)]
	if g == nil || g.scope != scope {
		return ErrUnavailable
	}
	state, err := g.Observe()
	if err != nil || state.Populated {
		return ErrUnavailable
	}
	g.acknowledged = true
	if g.refs != 0 {
		return nil
	}
	return g.removeEmpty()
}

func (g *group) removeEmpty() error {
	state, err := g.Observe()
	if err != nil || state.Populated {
		return ErrUnavailable
	}
	delete(g.controller.groups, key(g.scope))
	err = g.file.Close()
	if remove := os.Remove(g.path); err == nil {
		err = remove
	}
	return err
}
