package main

// Every loopback port the installation occupies chooses itself.
//
// The window does not care which port the app is on: the launcher tells the shell the address, a
// deep link is resolved against the env file, and the agent reads its ports from the environment
// the launcher gives it. So a port another program already holds — an older Daedalus, a dev server,
// anything — is not a reason to stop a first run. It was: a fresh Mac with something on 8765 ended
// its first start with "something this launcher did not start already answers on the app's port".
//
// The rule, for the app's port, the key proxy's, the supervisor's where it listens on TCP, and the
// services' and terminals' ranges:
//
//   - the documented default is kept whenever it is free, so an installation on a quiet machine
//     looks exactly like the documentation;
//   - a port is chosen once and written to the env file, and stays there across restarts — the
//     app's origin is part of what the browser keeps per site, and moving it for no reason would
//     cost the operator their drafts and settings in the window;
//   - at every start the written ports are checked again, and one that something else now holds
//     moves to a free one near it (or one the system hands out), is written down, and the move is
//     said in the log. The foreign process is never touched;
//   - a range moves as one block, to a block that is wholly free.
//
// "Something else" is decided by what the launcher already knows is its own: in native mode the
// orphans of a launcher that died are stopped before the check, and an answer carrying the boot id
// this installation last started its supervisor with is a bot it started, which keeps the port and
// is refused as before; in Docker mode a port the project's own running containers publish is theirs.

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// portRole is one door, or one block of doors, the installation needs.
type portRole struct {
	key  string // its name in the env file
	name string // how the log names it
	// spare marks a range the stack hands out a port at a time, skipping the ports that are taken —
	// the services natively. Such a range moves only once nothing in it can be had: a single
	// foreign port in it costs one service a port, while moving the block would leave the services
	// already running from an earlier start outside the range the browser's wall lets through.
	spare bool
}

// portDefaults are the documented defaults. A variable so that a test can point them at ports it
// holds itself, rather than occupying 8765 on a machine where something real may be listening.
var portDefaults = map[string]string{
	"API_PORT":                 "8765",
	"KEYPROXY_PORT":            defaultKeyproxyPort,
	"DAEDALUS_SUPERVISOR_PORT": defaultSupervisorPort,
	"SERVICES_PORT_RANGE":      "8100-8119",
	"TERMINALS_PORT_RANGE":     "8120-8139",
}

// portRoles are the doors of one installation shape. The key proxy and the supervisor publish
// nothing in Docker mode — they are reached inside the compose network — and the terminals' range
// is published only by the terminals container: a host terminal natively has no range of its own.
func portRoles(mode Mode, supervisorTCP bool) []portRole {
	roles := []portRole{{key: "API_PORT", name: "the app's port"}}
	if mode == ModeNative {
		roles = append(roles, portRole{key: "KEYPROXY_PORT", name: "the key proxy's port"})
		if supervisorTCP {
			roles = append(roles, portRole{key: "DAEDALUS_SUPERVISOR_PORT", name: "the supervisor's port"})
		}
		return append(roles, portRole{key: "SERVICES_PORT_RANGE", name: "the services' ports", spare: true})
	}
	return append(roles,
		portRole{key: "SERVICES_PORT_RANGE", name: "the services' ports"},
		portRole{key: "TERMINALS_PORT_RANGE", name: "the terminals' ports"},
	)
}

// portProbe is how the plan learns about the machine; the tests give it a machine of their own.
type portProbe struct {
	taken func(port int) bool             // something listens there, or the port cannot be bound
	ours  func(key string, port int) bool // what holds it is this installation's own
	spare func() (int, error)             // a port the system hands out, for when nothing near is free
	warm  func(b block)                   // asks about a whole block at once; nil asks port by port
}

// portMove is one door that moved, for the log.
type portMove struct {
	name, from, to, why string
}

func (m portMove) String() string {
	return fmt.Sprintf("%s %s %s; it moves to %s", m.name, m.from, m.why, m.to)
}

// block is a run of ports, both ends included.
type block struct{ lo, hi int }

func (b block) width() int { return b.hi - b.lo + 1 }

func (b block) overlaps(o block) bool { return b.lo <= o.hi && o.lo <= b.hi }

func (b block) String() string {
	if b.lo == b.hi {
		return strconv.Itoa(b.lo)
	}
	return fmt.Sprintf("%d-%d", b.lo, b.hi)
}

// parseBlock reads "8765" or "8100-8119". Anything else is not a port and is not used.
func parseBlock(value string) (block, bool) {
	value = strings.TrimSpace(value)
	low, high, isRange := strings.Cut(value, "-")
	lo, err := strconv.Atoi(strings.TrimSpace(low))
	if err != nil {
		return block{}, false
	}
	hi := lo
	if isRange {
		if hi, err = strconv.Atoi(strings.TrimSpace(high)); err != nil {
			return block{}, false
		}
	}
	if lo < 1 || hi > 65535 || hi < lo {
		return block{}, false
	}
	return block{lo, hi}, true
}

// planPorts decides every role's ports. It returns the value each role is to have in the env file —
// all of them, so a key a previous version never wrote is written now — and what moved and why.
// fresh is a first setup: nothing of the installation runs yet, and a range has to be wholly free.
func planPorts(settings map[string]string, roles []portRole, probe portProbe, fresh bool) ([]envVar, []portMove) {
	current := make([]block, len(roles))
	written := make([]bool, len(roles))
	for i, role := range roles {
		fallback, _ := parseBlock(portDefaults[role.key])
		if b, ok := parseBlock(settings[role.key]); ok {
			current[i], written[i] = b, true
		} else {
			current[i] = fallback
		}
	}
	// What no role may move into: the launcher's own page, and every other role's default — so a
	// door that moves does not land where another one will come back to on a quieter day.
	var reserved []block
	reserved = append(reserved, block{defaultPort, defaultPort})
	for _, value := range portDefaults {
		if b, ok := parseBlock(value); ok {
			reserved = append(reserved, b)
		}
	}
	// Two passes: first every role keeps what it has if it can, so a door that moves never takes a
	// port from one that did not need to; then the rest are placed around them.
	kept := make([]bool, len(roles))
	why := make([]string, len(roles))
	var placed []block
	for i, role := range roles {
		for j := 0; j < i; j++ {
			if kept[j] && current[i].overlaps(current[j]) {
				why[i] = "overlaps " + roles[j].name
			}
		}
		if why[i] != "" {
			continue
		}
		blocked := 0
		for port := current[i].lo; port <= current[i].hi; port++ {
			if probe.taken(port) && (probe.ours == nil || !probe.ours(role.key, port)) {
				blocked++
			}
		}
		if blocked == 0 || (role.spare && !fresh && blocked < current[i].width()) {
			kept[i] = true
			placed = append(placed, current[i])
			continue
		}
		why[i] = "is taken by another program"
		if blocked < current[i].width() {
			why[i] = fmt.Sprintf("is partly taken by another program (%d of %d)", blocked, current[i].width())
		}
	}
	values := make([]envVar, len(roles))
	var moves []portMove
	for i, role := range roles {
		chosen := current[i]
		if !kept[i] {
			avoid := append(append([]block(nil), reserved...), placed...)
			if next, ok := nearbyBlock(current[i], avoid, probe); ok {
				chosen = next
			}
			placed = append(placed, chosen)
			if chosen != current[i] {
				from := current[i].String()
				if !written[i] {
					from = "(the default " + from + ")"
				}
				moves = append(moves, portMove{name: role.name, from: from, to: chosen.String(), why: why[i]})
			}
		}
		values[i] = envVar{role.key, chosen.String()}
	}
	return values, moves
}

// nearbySearch is how far past the current port the plan looks before it asks the system instead.
const nearbySearch = 400

// nearbyBlock finds a block as wide as from, wholly free and clear of avoid: the nearest one above
// it first, then one starting where the system says a port is free.
func nearbyBlock(from block, avoid []block, probe portProbe) (block, bool) {
	width := from.width()
	usable := func(b block) (bool, int) {
		if b.lo < 1024 || b.hi > 65535 {
			return false, b.lo
		}
		for _, a := range avoid {
			if b.overlaps(a) {
				return false, a.hi
			}
		}
		for port := b.lo; port <= b.hi; port++ {
			if probe.taken(port) {
				return false, port
			}
		}
		return true, 0
	}
	for start := from.lo + 1; start <= from.lo+nearbySearch && start+width-1 <= 65535; start++ {
		ok, past := usable(block{start, start + width - 1})
		if ok {
			return block{start, start + width - 1}, true
		}
		// Whatever made this start unusable makes every start up to it unusable too.
		if past > start {
			start = past
		}
	}
	for try := 0; try < 20 && probe.spare != nil; try++ {
		port, err := probe.spare()
		if err != nil {
			break
		}
		if b := (block{port, port + width - 1}); b.hi <= 65535 {
			if ok, _ := usable(b); ok {
				return b, true
			}
		}
	}
	return from, false
}

// portTaken reports whether a loopback port is unavailable. Binding it is the question that
// matters, and it is asked first because it is instant everywhere. A successful bind is not the
// whole answer: on macOS and Windows a program listening on every interface does not stop a bind
// to 127.0.0.1 alone, and two listeners would then split the connections — so a connection is tried
// as well. Nothing is bound to all interfaces here: that would ask the operator's firewall a
// question for a probe that lasts a millisecond.
func portTaken(port int) bool {
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return true
	}
	listener.Close()
	// Short, because Windows answers a connection to a closed loopback port by retrying for about a
	// second instead of refusing at once, and a range is twenty of them.
	conn, err := net.DialTimeout("tcp", address, 300*time.Millisecond)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// systemPort is a port the operating system says is free right now.
func systemPort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

// machineProbe asks the real machine. Each port is asked at most once per plan, and a range is
// asked in parallel, so a twenty-port block costs one probe's time rather than twenty.
func machineProbe(ours func(key string, port int) bool) portProbe {
	var mu sync.Mutex
	answers := map[int]bool{}
	taken := func(port int) bool {
		mu.Lock()
		answer, known := answers[port]
		mu.Unlock()
		if known {
			return answer
		}
		answer = portTaken(port)
		mu.Lock()
		answers[port] = answer
		mu.Unlock()
		return answer
	}
	if ours == nil {
		ours = func(string, int) bool { return false }
	}
	return portProbe{taken: taken, ours: ours, spare: systemPort, warm: func(b block) {
		var wg sync.WaitGroup
		for port := b.lo; port <= b.hi; port++ {
			wg.Add(1)
			go func(port int) { defer wg.Done(); taken(port) }(port)
		}
		wg.Wait()
	}}
}

// settlePorts checks the installation's ports before a start and moves the ones something else
// holds, writing the result to the env file and saying every move in the log.
func settlePorts(p Paths, mode Mode, ours func(key string, port int) bool, log func(string, ...any)) error {
	settings := readEnv(readFile(p.Env))
	roles := portRoles(mode, supervisorOverTCP(p, runtime.GOOS))
	probe := machineProbe(ours)
	warmRoles(probe, settings, roles)
	values, moves := planPorts(settings, roles, probe, false)
	for _, move := range moves {
		log("%s (written to %s)", move, p.Env)
	}
	var changed []envVar
	for _, value := range values {
		if settings[value.Key] != value.Value {
			changed = append(changed, value)
		}
	}
	if len(changed) == 0 {
		return nil
	}
	if err := os.WriteFile(p.Env, []byte(mergeEnv(readFile(p.Env), changed)), 0o600); err != nil {
		return fmt.Errorf("the ports this start chose could not be written down: %w", err)
	}
	return nil
}

// warmRoles asks about every port the roles hold now, all at once.
func warmRoles(probe portProbe, settings map[string]string, roles []portRole) {
	if probe.warm == nil {
		return
	}
	var wg sync.WaitGroup
	for _, role := range roles {
		b, ok := parseBlock(settings[role.key])
		if !ok {
			b, _ = parseBlock(portDefaults[role.key])
		}
		wg.Add(1)
		go func(b block) { defer wg.Done(); probe.warm(b) }(b)
	}
	wg.Wait()
}

// bootRecord is the boot id this installation last started its supervisor with. It outlives the
// stop on purpose: a bot whose supervisor died first is in a session of its own, no record of a
// child names it, and its answer carrying this id is the one way left to know it is ours. Beside the
// pids folder and not in it: FindOrphans drops every file there that is not a child's record.
func bootRecord(p Paths) string { return filepath.Join(p.Local, "boot-id") }

func writeBootRecord(p Paths, id string) {
	if err := os.MkdirAll(p.Local, 0o700); err == nil {
		_ = os.WriteFile(bootRecord(p), []byte(id+"\n"), 0o600)
	}
}

// answersWithOurBoot reports whether the app answering on a port is one this installation started:
// it carries the boot id last recorded. A port held by that bot is not moved away from — the bot
// would go on serving the same data beside the new one — and the start refuses as it always did.
func answersWithOurBoot(p Paths, port int) bool {
	want := strings.TrimSpace(readFile(bootRecord(p)))
	if want == "" {
		return false
	}
	client := &http.Client{
		Timeout:       2 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	response, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/app", port))
	if err != nil {
		return false
	}
	response.Body.Close()
	return response.Header.Get(bootHeader) == want
}

// publishedPorts reads the host ports the project's running containers publish out of `docker
// compose ps --format json`, which prints one JSON array in older compose releases and one object
// per line in newer ones.
func publishedPorts(out string) map[int]bool {
	type container struct {
		Publishers []struct {
			PublishedPort int `json:"PublishedPort"`
		} `json:"Publishers"`
	}
	var all []container
	text := strings.TrimSpace(out)
	if strings.HasPrefix(text, "[") {
		_ = json.Unmarshal([]byte(text), &all)
	} else {
		for _, line := range strings.Split(text, "\n") {
			var one container
			if json.Unmarshal([]byte(strings.TrimSpace(line)), &one) == nil {
				all = append(all, one)
			}
		}
	}
	ports := map[int]bool{}
	for _, c := range all {
		for _, publisher := range c.Publishers {
			if publisher.PublishedPort > 0 {
				ports[publisher.PublishedPort] = true
			}
		}
	}
	return ports
}
