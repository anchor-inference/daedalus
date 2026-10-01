package main

// The ports choose themselves (ports.go). The plan is tested against a machine described in the
// test; the rest on real loopback listeners this test opens itself, on ports the system hands out —
// the documented defaults are swapped for them, so nothing here binds 8765 or any other port a
// program on this machine may really be using.

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fakeMachine is a probe over a set of taken ports and a set of ports that are the installation's.
func fakeMachine(taken map[int]bool, ours map[int]bool) portProbe {
	next := 40000
	return portProbe{
		taken: func(port int) bool { return taken[port] },
		ours:  func(_ string, port int) bool { return ours[port] },
		spare: func() (int, error) { next += 100; return next, nil },
	}
}

func values(vars []envVar) map[string]string {
	out := map[string]string{}
	for _, v := range vars {
		out[v.Key] = v.Value
	}
	return out
}

func takenBlock(lo, hi int) map[int]bool {
	out := map[int]bool{}
	for port := lo; port <= hi; port++ {
		out[port] = true
	}
	return out
}

func TestAQuietMachineKeepsTheDocumentedPorts(t *testing.T) {
	got, moves := planPorts(map[string]string{}, portRoles(ModeNative, true), fakeMachine(nil, nil), true)
	want := map[string]string{"API_PORT": "8765", "KEYPROXY_PORT": "3201", "DAEDALUS_SUPERVISOR_PORT": "8769", "SERVICES_PORT_RANGE": "8100-8119"}
	if fmt.Sprint(values(got)) != fmt.Sprint(want) || len(moves) != 0 {
		t.Fatalf("got %v, moves %v", values(got), moves)
	}
	got, _ = planPorts(map[string]string{}, portRoles(ModeDocker, false), fakeMachine(nil, nil), true)
	want = map[string]string{"API_PORT": "8765", "SERVICES_PORT_RANGE": "8100-8119", "TERMINALS_PORT_RANGE": "8120-8139"}
	if fmt.Sprint(values(got)) != fmt.Sprint(want) {
		t.Fatalf("docker: got %v", values(got))
	}
}

func TestEveryTakenDefaultMovesNearbyAndNothingCollides(t *testing.T) {
	taken := takenBlock(8100, 8139)
	for _, port := range []int{8765, 3201, 8769} {
		taken[port] = true
	}
	for _, mode := range []Mode{ModeNative, ModeDocker} {
		got, moves := planPorts(map[string]string{}, portRoles(mode, true), fakeMachine(taken, nil), true)
		v := values(got)
		if v["API_PORT"] != "8766" {
			t.Fatalf("%s: the app's port went to %s, not the next free one", mode, v["API_PORT"])
		}
		if mode == ModeNative && (v["KEYPROXY_PORT"] != "3202" || v["DAEDALUS_SUPERVISOR_PORT"] != "8771") {
			// 8770 is the launcher's own page: never handed to anything else.
			t.Fatalf("native: %v", v)
		}
		var blocks []block
		for key, value := range v {
			b, ok := parseBlock(value)
			if !ok {
				t.Fatalf("%s=%q is not a port", key, value)
			}
			for port := b.lo; port <= b.hi; port++ {
				if taken[port] || port == defaultPort {
					t.Fatalf("%s: %s=%s lands on a taken or reserved port %d", mode, key, value, port)
				}
			}
			for _, other := range blocks {
				if b.overlaps(other) {
					t.Fatalf("%s: %s=%s overlaps %s", mode, key, value, other)
				}
			}
			blocks = append(blocks, b)
		}
		if services, _ := parseBlock(v["SERVICES_PORT_RANGE"]); services.width() != 20 {
			t.Fatalf("the services' range changed its width: %s", v["SERVICES_PORT_RANGE"])
		}
		if len(moves) == 0 || !strings.Contains(moves[0].String(), "taken by another program") {
			t.Fatalf("the moves say nothing: %v", moves)
		}
	}
}

func TestARangeMovesAsOneBlockToAWhollyFreeOne(t *testing.T) {
	// One port taken in the middle of the terminals' range, and the block above it partly taken
	// too: the range does not split around them, and does not settle for a block with a hole in it.
	taken := map[int]bool{8125: true, 8150: true}
	settings := map[string]string{"API_PORT": "8765", "SERVICES_PORT_RANGE": "8100-8119", "TERMINALS_PORT_RANGE": "8120-8139"}
	got, moves := planPorts(settings, portRoles(ModeDocker, false), fakeMachine(taken, nil), false)
	terminals, _ := parseBlock(values(got)["TERMINALS_PORT_RANGE"])
	if terminals.width() != 20 || terminals.lo <= 8150 {
		t.Fatalf("terminals moved to %s", terminals)
	}
	if len(moves) != 1 || !strings.Contains(moves[0].String(), "partly taken") {
		t.Fatalf("moves %v", moves)
	}
}

func TestTheServicesRangeNativelyMovesOnlyWhenNothingInItIsLeft(t *testing.T) {
	settings := map[string]string{"API_PORT": "8765", "KEYPROXY_PORT": "3201", "SERVICES_PORT_RANGE": "8100-8119"}
	partly := takenBlock(8100, 8110)
	got, _ := planPorts(settings, portRoles(ModeNative, false), fakeMachine(partly, nil), false)
	if values(got)["SERVICES_PORT_RANGE"] != "8100-8119" {
		t.Fatalf("a range with free ports left moved: %v", values(got))
	}
	// On a first setup nothing is running yet, and the range starts out wholly free.
	got, _ = planPorts(settings, portRoles(ModeNative, false), fakeMachine(partly, nil), true)
	if values(got)["SERVICES_PORT_RANGE"] == "8100-8119" {
		t.Fatal("a first setup kept a range with ports already taken in it")
	}
	got, _ = planPorts(settings, portRoles(ModeNative, false), fakeMachine(takenBlock(8100, 8119), nil), false)
	if values(got)["SERVICES_PORT_RANGE"] == "8100-8119" {
		t.Fatal("a range with nothing left in it stayed")
	}
}

func TestAPortTheInstallationHoldsItselfIsKept(t *testing.T) {
	// Docker mode, the stack still up behind a closed launcher: its containers publish the ports.
	held := takenBlock(8100, 8139)
	held[8765] = true
	settings := map[string]string{"API_PORT": "8765", "SERVICES_PORT_RANGE": "8100-8119"}
	got, moves := planPorts(settings, portRoles(ModeDocker, false), fakeMachine(held, held), false)
	want := map[string]string{"API_PORT": "8765", "SERVICES_PORT_RANGE": "8100-8119", "TERMINALS_PORT_RANGE": "8120-8139"}
	if fmt.Sprint(values(got)) != fmt.Sprint(want) || len(moves) != 0 {
		t.Fatalf("got %v, moves %v", values(got), moves)
	}
}

func TestAPortChosenByHandIsKeptWhileItIsFree(t *testing.T) {
	settings := map[string]string{"API_PORT": "18765", "KEYPROXY_PORT": "13201", "SERVICES_PORT_RANGE": "18100-18109"}
	got, moves := planPorts(settings, portRoles(ModeNative, false), fakeMachine(map[int]bool{8765: true}, nil), false)
	if fmt.Sprint(values(got)) != fmt.Sprint(settings) || len(moves) != 0 {
		t.Fatalf("got %v, moves %v", values(got), moves)
	}
	// Two doors written onto one port by hand: the first keeps it, the second moves.
	settings["KEYPROXY_PORT"] = "18765"
	got, moves = planPorts(settings, portRoles(ModeNative, false), fakeMachine(nil, nil), false)
	if v := values(got); v["API_PORT"] != "18765" || v["KEYPROXY_PORT"] == "18765" || len(moves) != 1 || !strings.Contains(moves[0].why, "overlaps the app's port") {
		t.Fatalf("got %v, moves %v", v, moves)
	}
}

func TestNothingNearbyFallsBackToWhatTheSystemHandsOut(t *testing.T) {
	taken := takenBlock(8765, 8765+nearbySearch+5)
	got, _ := planPorts(map[string]string{"API_PORT": "8765"}, []portRole{{key: "API_PORT", name: "the app's port"}}, fakeMachine(taken, nil), false)
	if values(got)["API_PORT"] != "40100" {
		t.Fatalf("got %v", values(got))
	}
}

func TestComposeSaysWhichPortsItsContainersPublishInEitherShape(t *testing.T) {
	array := `[{"Name":"daedalus-daedalus-1","Publishers":[{"URL":"127.0.0.1","TargetPort":8765,"PublishedPort":8765,"Protocol":"tcp"},{"URL":"","TargetPort":8100,"PublishedPort":0}]}]`
	lines := `{"Name":"a","Publishers":[{"PublishedPort":8765}]}` + "\n" + `{"Name":"b","Publishers":[{"PublishedPort":8120},{"PublishedPort":8121}]}` + "\n"
	if got := publishedPorts(array); len(got) != 1 || !got[8765] {
		t.Fatalf("array: %v", got)
	}
	if got := publishedPorts(lines); len(got) != 3 || !got[8121] {
		t.Fatalf("lines: %v", got)
	}
	if got := publishedPorts(""); len(got) != 0 {
		t.Fatalf("empty: %v", got)
	}
}

// --- on real listeners ---

// foreign is a program that is not Daedalus, listening on a loopback port until the test ends.
func foreign(t *testing.T, port int) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("holding %d: %v", port, err)
	}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}
	}()
	t.Cleanup(func() { listener.Close() })
	return listener
}

// freeRun is the first port of width consecutive free ports the system says nothing uses.
func freeRun(t *testing.T, width int, avoid ...block) int {
	t.Helper()
	for try := 0; try < 50; try++ {
		start, err := systemPort()
		if err != nil {
			t.Fatal(err)
		}
		b := block{start, start + width - 1}
		ok := b.hi <= 65535
		for _, a := range avoid {
			ok = ok && !b.overlaps(a)
		}
		for port := b.lo; ok && port <= b.hi; port++ {
			ok = !portTaken(port)
		}
		if ok {
			return start
		}
	}
	t.Fatal("no free run of ports on this machine")
	return 0
}

// testDefaults points the documented defaults at free ports for one test, and returns them.
func testDefaults(t *testing.T) (api, keyproxy, supervisor, services int) {
	t.Helper()
	api = freeRun(t, 1)
	keyproxy = freeRun(t, 1, block{api, api})
	supervisor = freeRun(t, 1, block{api, api}, block{keyproxy, keyproxy})
	services = freeRun(t, 20, block{api, api}, block{keyproxy, keyproxy}, block{supervisor, supervisor})
	saved := portDefaults
	portDefaults = map[string]string{
		"API_PORT":                 strconv.Itoa(api),
		"KEYPROXY_PORT":            strconv.Itoa(keyproxy),
		"DAEDALUS_SUPERVISOR_PORT": strconv.Itoa(supervisor),
		"SERVICES_PORT_RANGE":      fmt.Sprintf("%d-%d", services, services+19),
		"TERMINALS_PORT_RANGE":     saved["TERMINALS_PORT_RANGE"],
	}
	t.Cleanup(func() { portDefaults = saved })
	return
}

func TestAFirstSetupOnAMachineHoldingTheDefaultsWritesOthers(t *testing.T) {
	api, keyproxy, _, services := testDefaults(t)
	foreign(t, api)
	foreign(t, keyproxy)
	for port := services; port < services+20; port++ {
		foreign(t, port)
	}
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	var said []string
	if err := writeSetup(paths, Setup{}, ModeNative, func(format string, args ...any) { said = append(said, fmt.Sprintf(format, args...)) }); err != nil {
		t.Fatal(err)
	}
	env := readEnv(readFile(paths.Env))
	for key, held := range map[string]int{"API_PORT": api, "KEYPROXY_PORT": keyproxy} {
		port, _ := strconv.Atoi(env[key])
		if port == held || port == 0 || portTaken(port) {
			t.Fatalf("%s=%s: held %d", key, env[key], held)
		}
	}
	if b, _ := parseBlock(env["SERVICES_PORT_RANGE"]); b.overlaps(block{services, services + 19}) || b.width() != 20 {
		t.Fatalf("services %s", env["SERVICES_PORT_RANGE"])
	}
	if !strings.Contains(strings.Join(said, "\n"), "the app's port (the default "+strconv.Itoa(api)+") is taken by another program; it moves to") {
		t.Fatalf("the setup said %q", said)
	}
	// Setup again: the ports are written, and the second answer keeps them.
	before := readFile(paths.Env)
	if err := WriteSetup(paths, Setup{USDPerDay: "5"}, ModeNative); err != nil {
		t.Fatal(err)
	}
	if after := readEnv(readFile(paths.Env)); after["API_PORT"] != env["API_PORT"] || after["SERVICES_PORT_RANGE"] != env["SERVICES_PORT_RANGE"] {
		t.Fatalf("a second setup moved the ports:\n%s\n%s", before, readFile(paths.Env))
	}
}

// quietInstallation is a native data folder whose env file names free ports of its own.
func quietInstallation(t *testing.T) (Paths, int) {
	t.Helper()
	api, keyproxy, _, services := testDefaults(t)
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	os.MkdirAll(p.Data, 0o755)
	env := fmt.Sprintf("API_PORT=%d\nKEYPROXY_PORT=%d\nSERVICES_PORT_RANGE=%d-%d\nUSD_PER_DAY=20\n", api, keyproxy, services, services+19)
	if err := os.WriteFile(p.Env, []byte(env), 0o600); err != nil {
		t.Fatal(err)
	}
	return p, api
}

func TestANativeStartMovesOffAPortAnotherProgramTookAndTheAppComesUp(t *testing.T) {
	p, api := quietInstallation(t)
	// Not Daedalus at all: an HTTP server of someone else's, answering 200 without a boot id.
	other, line := helper(t, "serve", "HELPER_PORT="+strconv.Itoa(api))
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	var said []string
	n := NewNative(p, func(format string, args ...any) { said = append(said, fmt.Sprintf(format, args...)) })
	port, portWasFree, err := n.clearTheWay(context.Background())
	if err != nil {
		t.Fatalf("the start refused: %v", err)
	}
	if port == strconv.Itoa(api) || !portWasFree || APIPort(p) != port || portAnswers(port) {
		t.Fatalf("port %s (was %d), free %v, written %s", port, api, portWasFree, APIPort(p))
	}
	if !strings.Contains(strings.Join(said, "\n"), fmt.Sprintf("the app's port %d is taken by another program; it moves to %s (written to %s)", api, port, p.Env)) {
		t.Fatalf("the move was not said: %q", said)
	}
	if !processAlive(other.Process.Pid) || !portAnswers(strconv.Itoa(api)) {
		t.Fatal("the other program was touched")
	}
	// The stack this start brings up listens where the env file now says, and is recognised there.
	_, line = helper(t, "serve", "HELPER_PORT="+APIPort(p), "DAEDALUS_BOOT_ID=this-start")
	if line != "serving" {
		t.Fatalf("stack: %q", line)
	}
	expectBoot(APIPort(p), bootExpectation{id: "this-start"})
	if err := waitReady(context.Background(), APIPort(p), 3*time.Second, 50*time.Millisecond); err != nil {
		t.Fatalf("the app did not come up on the moved port: %v", err)
	}
	if got := AppURL(APIPort(p), LangEN); !strings.Contains(got, ":"+port+"/app/") {
		t.Fatalf("the window would be sent to %s", got)
	}
}

func TestABotThisInstallationStartedKeepsItsPortAndTheStartRefuses(t *testing.T) {
	p, api := quietInstallation(t)
	// A bot whose supervisor died first: in a session of its own, no record names it, and it
	// still serves this installation's data — with the boot id this installation last gave out.
	writeBootRecord(p, "the-last-start")
	_, line := helper(t, "serve", "HELPER_PORT="+strconv.Itoa(api), "DAEDALUS_BOOT_ID=the-last-start")
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	_, _, err := NewNative(p, func(string, ...any) {}).clearTheWay(context.Background())
	if err == nil || !strings.Contains(err.Error(), "this installation started earlier still answers") {
		t.Fatalf("err = %v", err)
	}
	if APIPort(p) != strconv.Itoa(api) {
		t.Fatalf("the port moved away from our own bot to %s", APIPort(p))
	}
}

func TestAnotherDaedalusIsAnotherProgram(t *testing.T) {
	// An older installation elsewhere on the machine answers with a boot id too — not ours.
	p, api := quietInstallation(t)
	writeBootRecord(p, "ours")
	_, line := helper(t, "serve", "HELPER_PORT="+strconv.Itoa(api), "DAEDALUS_BOOT_ID=an-older-installation")
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	port, _, err := NewNative(p, func(string, ...any) {}).clearTheWay(context.Background())
	if err != nil || port == strconv.Itoa(api) {
		t.Fatalf("port %s, err %v", port, err)
	}
}

func TestAMovedPortStaysAcrossRestartsAndMovesAgainOnlyWhenTaken(t *testing.T) {
	p, api := quietInstallation(t)
	foreign(t, api)
	log := func(string, ...any) {}
	if err := settlePorts(p, ModeNative, nil, log); err != nil {
		t.Fatal(err)
	}
	moved := APIPort(p)
	if moved == strconv.Itoa(api) {
		t.Fatal("did not move")
	}
	// The default comes free again: the installation stays where it is, so the app's origin — and
	// what the window keeps for it — does not change under the operator.
	if err := settlePorts(p, ModeNative, nil, log); err != nil {
		t.Fatal(err)
	}
	if APIPort(p) != moved {
		t.Fatalf("moved again to %s", APIPort(p))
	}
	movedPort, _ := strconv.Atoi(moved)
	foreign(t, movedPort)
	if err := settlePorts(p, ModeNative, nil, log); err != nil {
		t.Fatal(err)
	}
	if APIPort(p) == moved {
		t.Fatal("a port taken since the last start was kept")
	}
}
