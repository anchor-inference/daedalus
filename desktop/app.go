package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// App is the launcher's state: the folder it works in, what it is doing, and the last lines it
// printed. The same methods serve the command line and the buttons on the local page, so both
// paths do exactly the same thing to the stack.
type App struct {
	paths Paths
	// fetched are the checkouts' archives an update's prepare downloaded, for its move to apply.
	fetched map[string][]byte
	// mode is which of the two shapes this installation has. It is resolved before anything is
	// started and every method below asks it once: the launcher does the same things either way,
	// and only how it does them differs.
	mode   Mode
	native *Native

	mu      sync.Mutex
	lines   []string
	busy    string
	failure string

	// What each action became, by the id the caller was given. "Not busy" is not an outcome —
	// a launcher that never picked the work up and one that finished it look identical from
	// outside — so every action leaves a record of how it ended, and the app's bridge polls that
	// instead of watching a status field for a transition it may miss between two polls.
	jobs     map[string]*Job
	jobOrder []string
	jobSeq   int

	// Where a start has got to, and — while something with a known size is being downloaded — how
	// far through it is. Both are for the progress page: the log says what is happening, these say
	// how much of it is left.
	stage     Stage
	stageDone int64
	stageSize int64

	// startedAt is when the stack was last brought up, and paired records that a link which signs
	// the operator in has already been handed to a browser since. Both exist so that the launcher
	// offers a pairing link once, while it is fresh, and the app's own address afterwards.
	startedAt time.Time
	paired    bool

	// What the container last said about the agent's own changes, and when it said it. Reading it
	// means running a command inside the container, which is far too slow for every status poll.
	changeNotice ChangeNotice
	changeAt     time.Time

	// lock is the installation lock while this process is the launcher running the stack.
	lock *InstallLock

	// offer is a newer launcher release the check found (release.go), or nil. The launcher only
	// tells: `daedalus-desktop upgrade` is what installs it, after a yes and a checked backup.
	offer *Offer
}

// logLimit is how much of the running commentary the page keeps. It is a progress view, not a log
// file: the real logs come from docker.
const logLimit = 200

// ErrNotNative is the answer to an action only a native installation has. Not a failure and not a
// collision with another action: this installation is the other shape, and nothing it does here
// would help. The page and the app both need that apart from "the launcher is busy".
var ErrNotNative = errors.New("the runtime extras and the agent's own restart belong to native mode; in Docker mode the browser comes with the :browser image and compose restarts the containers")

// Job is one action of the launcher's, from the moment it is claimed to the moment it is over.
type Job struct {
	ID     string `json:"id"`
	Action string `json:"action"`
	// State is running, done or failed. Never empty: a job exists only once it has been claimed.
	State string `json:"state"`
	Error string `json:"error"`
}

// jobLimit is how many finished actions are remembered. The caller polls its own job seconds after
// it asked for it; this is generous for that and bounded for a launcher left open for weeks.
const jobLimit = 32

func NewApp(p Paths) *App {
	app := &App{paths: p, mode: StoredMode(p), jobs: map[string]*Job{}}
	app.native = NewNative(p, app.log)
	app.native.OnProgress(app.enter, app.downloaded)
	return app
}

// enter records which piece of a start the launcher has reached. Any progress already counted
// belongs to the piece that is over, so it goes with it.
func (a *App) enter(stage Stage) {
	a.mu.Lock()
	a.stage, a.stageDone, a.stageSize = stage, 0, 0
	a.mu.Unlock()
}

// downloaded records how much of a download of a known size is done. Only the runtime has one: an
// image pull reports its own progress to a terminal nobody is reading, and an honest bar that does
// not know is better than a made-up one that does.
func (a *App) downloaded(done, total int64) {
	a.mu.Lock()
	a.stageDone, a.stageSize = done, total
	a.mu.Unlock()
}

// SetMode is how the command line and the setup page tell the launcher which shape to run in. It is
// set before anything starts and does not change while it is running.
func (a *App) SetMode(mode Mode) {
	a.mu.Lock()
	a.mode = mode
	a.mu.Unlock()
}

// Mode is the shape in force.
func (a *App) Mode() Mode {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.mode
}

// Native reports whether this installation runs without a container.
func (a *App) Native() bool { return a.Mode() == ModeNative }

// Lang is the language this installation chose, and English until it has. Everything the launcher
// hands to a browser — the app's address above all — is in it.
func (a *App) Lang() Lang { return LangFor(a.paths, "") }

// log records a line and echoes it to the terminal, so the operator sees the same progress whether
// they are watching the window the launcher runs in or the page in the browser.
func (a *App) log(format string, args ...any) {
	line := fmt.Sprintf(format, args...)
	fmt.Println(line)
	a.mu.Lock()
	defer a.mu.Unlock()
	a.lines = append(a.lines, time.Now().Format("15:04:05")+"  "+line)
	if len(a.lines) > logLimit {
		a.lines = a.lines[len(a.lines)-logLimit:]
	}
}

// begin claims the launcher for one action. Two starts at once — one from the terminal, one from an
// impatient click — would race over the same compose project.
func (a *App) begin(action string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.busy != "" {
		return "", fmt.Errorf("%s is already running", a.busy)
	}
	a.busy = action
	a.failure = ""
	a.jobSeq++
	id := fmt.Sprintf("j%d", a.jobSeq)
	a.jobs[id] = &Job{ID: id, Action: action, State: "running"}
	a.jobOrder = append(a.jobOrder, id)
	for len(a.jobOrder) > jobLimit {
		delete(a.jobs, a.jobOrder[0])
		a.jobOrder = a.jobOrder[1:]
	}
	return id, nil
}

func (a *App) end(id string, err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.busy = ""
	a.stage, a.stageDone, a.stageSize = StageIdle, 0, 0
	if err != nil {
		a.failure = err.Error()
	}
	if job := a.jobs[id]; job != nil {
		job.State = "done"
		if err != nil {
			job.State, job.Error = "failed", err.Error()
		}
	}
}

// JobStatus is how an action ended, for a caller that is not looking at the page. The second value
// is false for an id this launcher never minted or has since forgotten.
func (a *App) JobStatus(id string) (Job, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	job, ok := a.jobs[id]
	if !ok {
		return Job{}, false
	}
	return *job, true
}

// Telegram reports whether the Telegram containers belong in this installation.
func (a *App) Telegram() bool {
	return strings.TrimSpace(readEnv(readFile(a.paths.Env))["TELEGRAM_BOT_TOKEN"]) != ""
}

// Start brings the stack up: the checkouts, the override, the images, the containers, and then the
// wait for the app to answer. It is safe to call on a running stack — compose reconciles.
func (a *App) Start(ctx context.Context) error {
	id, err := a.begin("start")
	if err != nil {
		return err
	}
	err = a.start(ctx)
	a.end(id, err)
	return err
}

func (a *App) start(ctx context.Context) error {
	if a.Native() {
		return a.startNative(ctx)
	}
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	a.enter(StageCheckouts)
	if err := EnsureRepos(ctx, a.paths, dockerGit(a.paths), a.log); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	if err := SyncBotEnv(a.paths, a.Mode()); err != nil {
		return err
	}
	telegram := a.Telegram()
	// From here on the containers may restart, and the server writes a new pairing link when they
	// do. The moment is taken before the start rather than after it, so a link written while the
	// stack was coming up still counts as this start's.
	a.mu.Lock()
	a.startedAt, a.paired = time.Now(), false
	a.mu.Unlock()
	a.enter(StageImages)
	a.log("pulling images")
	if _, err := compose(ctx, a.paths, telegram, "pull"); err != nil {
		// No published image for this platform, or no network. Building takes several minutes the
		// first time, mostly for the headless browser the agent's tools use.
		a.log("no image to pull; building locally, which takes a few minutes the first time")
		if err := composeStream(ctx, a.paths, telegram, "up", "-d", "--build"); err != nil {
			return err
		}
	} else if err := composeStream(ctx, a.paths, telegram, "up", "-d"); err != nil {
		return err
	}
	a.enter(StageStart)
	a.log("waiting for the app to answer")
	// Compose owns the containers, and their answer is taken as it always was (ready.go).
	expectBoot(APIPort(a.paths), bootExpectation{anyAnswer: true})
	if err := WaitReady(ctx, APIPort(a.paths), readyTimeout); err != nil {
		return err
	}
	a.log("the app is up at %s", AppURL(APIPort(a.paths), a.Lang()))
	a.mountProjects(ctx) // projects: folders the container cannot see yet (desktop/projects.go)
	return nil
}

// startNative is the same sequence without a container in it: the runtime instead of the images,
// the launcher's own child processes instead of compose, and the same wait for /app to answer.
func (a *App) startNative(ctx context.Context) error {
	a.mu.Lock()
	a.startedAt, a.paired = time.Now(), false
	a.mu.Unlock()
	if err := a.native.Start(ctx); err != nil {
		return err
	}
	a.log("the app is up at %s", AppURL(APIPort(a.paths), a.Lang()))
	return nil
}

// Stop leaves the containers in place but stopped. The restart policy is unless-stopped, so they
// stay down until the launcher is asked to start them again. A Stop followed by a Start applies
// whatever the checkout holds without preflighting it — Apply is the one that checks first.
func (a *App) Stop(ctx context.Context) error {
	id, err := a.begin("stop")
	if err != nil {
		return err
	}
	err = a.stop(ctx)
	a.end(id, err)
	return err
}

func (a *App) stop(ctx context.Context) error {
	if a.Native() {
		a.log("stopping the agent")
		a.native.Stop(ctx)
		// And what a launcher that died left running, which no Process of this one knows about.
		return StopOrphans(ctx, a.paths, a.log)
	}
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	a.log("stopping the stack")
	// The Telegram profile is always on for a stop: a container started under it must be stopped
	// even when the token was removed from the configuration since.
	_, err := compose(ctx, a.paths, true, "stop")
	return err
}

// Apply restarts the stack onto the change the agent committed to the checkout — through the
// supervisor, which is what makes it an apply rather than a restart. The supervisor checks the
// commit on a detached copy of itself first and keeps the running code when the checks do not pass;
// stopping and starting the containers instead would come back on whatever the checkout says with
// nothing having looked at it. That path is still here, as the fallback for a stack whose
// supervisor cannot be reached, and it says what it is giving up.
func (a *App) Apply(ctx context.Context) error {
	id, err := a.begin("apply")
	if err != nil {
		return err
	}
	err = a.apply(ctx)
	a.end(id, err)
	return err
}

func (a *App) apply(ctx context.Context) error {
	a.log("checking the change and restarting onto it")
	answer, err := a.supervisorRestart(ctx)
	if err == nil {
		if answer != "" {
			a.log("%s", answer)
		}
		a.forgetChange()
		return nil
	}
	a.log("the supervisor could not be asked (%s); starting the stack over instead, which applies the change without checking it first", err)
	if err := a.stop(ctx); err != nil {
		return err
	}
	if err := a.start(ctx); err != nil {
		return err
	}
	a.forgetChange()
	return nil
}

// supervisorRestart carries the request to the supervisor. It is the same request either way; what
// differs is whether it has to cross into a container to get there.
func (a *App) supervisorRestart(ctx context.Context) (string, error) {
	if a.Native() {
		return a.native.Apply(ctx, "the launcher's Apply")
	}
	return SupervisorRestart(ctx, a.paths, a.Telegram())
}

// forgetChange drops the cached answer about the agent's own code: the container has just been
// restarted, or has just been told to restart, and whatever it said before that is from before.
func (a *App) forgetChange() {
	a.mu.Lock()
	a.changeAt = time.Time{}
	a.mu.Unlock()
}

// StopOnQuit ends what the launcher is holding up when the launcher itself is going away. It is a
// no-op in Docker mode, where the containers are the operator's to leave running.
func (a *App) StopOnQuit() {
	if !a.Native() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	a.log("stopping the agent, because the launcher is closing")
	a.native.Stop(ctx)
}

// InstallExtra fetches one of the optional halves of the portable runtime. Only native mode has
// them: a Docker installation gets node and the browser from the image tag it runs.
func (a *App) InstallExtra(ctx context.Context, name string) error {
	id, err := a.StartExtra(name)
	if err != nil {
		return err
	}
	return a.RunExtra(ctx, id, name)
}

// RunExtra does the work a StartExtra claim was made for and records how it ended. Split from the
// claim so the page's handler can answer before the download starts and still report its outcome.
func (a *App) RunExtra(ctx context.Context, id, name string) error {
	err := a.native.InstallExtra(ctx, name)
	if err != nil {
		a.log("%s could not be installed: %v", name, err)
	}
	a.end(id, err)
	return err
}

// StartExtra claims the launcher for one extra and returns the job id to poll it by, without
// running it. The claim is what the answer to the app's bridge is made of: a launcher already busy
// with something else has to say so in the answer, because from outside a launcher that never
// picked the work up and one that has already finished it look exactly the same.
func (a *App) StartExtra(name string) (string, error) {
	if !a.Native() {
		return "", ErrNotNative
	}
	return a.begin("installing " + name)
}

// StartRestart claims the launcher for a restart, on the same terms.
func (a *App) StartRestart() (string, error) {
	if !a.Native() {
		return "", ErrNotNative
	}
	return a.begin("restart")
}

// Restart stops the agent and starts it again, so that what has been added to the installation
// since it started is in force. Node lands in the runtime folder and the headless browser in its
// own, and both are reached through the PATH and the environment the supervisor was given when the
// launcher started it: a process cannot give itself either. Native only — a container is restarted
// by compose, and in Docker mode neither piece is installable from here in the first place.
func (a *App) Restart(ctx context.Context) error {
	id, err := a.StartRestart()
	if err != nil {
		return err
	}
	return a.RunRestart(ctx, id)
}

// RunRestart does the work a StartRestart claim was made for, on the same terms as RunExtra.
func (a *App) RunRestart(ctx context.Context, id string) error {
	a.native.Stop(ctx)
	err := a.native.Start(ctx)
	if err != nil {
		a.log("the agent could not be restarted: %v", err)
	}
	a.end(id, err)
	return err
}

// Update moves both checkouts to what is published, refreshes the images and restarts. The agent's
// own merged pull requests arrive this way. It is the command line's `update` and the page's button,
// and it never changes the stack without a checked backup to go back to (update.go): the app's
// migrations run on the start that follows, and they are not assumed to be reversible.
func (a *App) Update(ctx context.Context) error {
	id, err := a.begin("update")
	if err != nil {
		return err
	}
	err = a.safeUpdate(ctx)
	a.end(id, err)
	return err
}

// update is the move itself, with no backup around it. Only safeUpdate and an upgrade's --finish —
// which each hold a verified backup and a journal — call it.
func (a *App) update(ctx context.Context) error {
	if a.Native() {
		// The processes are stopped first: an update rewrites the tree they are running out of, and
		// a running bot half-way through a file swap is a crash with a confusing log.
		a.native.Stop(ctx)
		if err := a.native.Ensure(ctx); err != nil {
			return err
		}
		// What prepareUpdate fetched, when it ran: the environment was built for exactly these trees.
		a.mu.Lock()
		archives := a.fetched
		a.fetched = nil
		a.mu.Unlock()
		if archives == nil {
			var err error
			if archives, err = FetchRepos(ctx, a.paths); err != nil {
				return err
			}
		}
		if err := ApplyRepos(ctx, a.paths, a.native.gitRunner(), a.log, archives); err != nil {
			return err
		}
		return a.start(ctx)
	}
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := EnsureRepos(ctx, a.paths, dockerGit(a.paths), a.log); err != nil {
		return err
	}
	if err := UpdateRepos(ctx, a.paths, dockerGit(a.paths), a.log); err != nil {
		return err
	}
	return a.start(ctx)
}

// Uninstall removes the containers and networks. The volumes — the database, the workspaces, the
// agent's memory — go with them unless the operator asked to keep the data.
func (a *App) Uninstall(ctx context.Context, keepData bool) error {
	id, err := a.begin("uninstall")
	if err != nil {
		return err
	}
	err = a.uninstall(ctx, keepData)
	a.end(id, err)
	return err
}

func (a *App) uninstall(ctx context.Context, keepData bool) error {
	if a.Native() {
		a.native.Stop(ctx)
		if keepData {
			a.log("the agent is stopped; %s still holds the state and your keys, and %s the runtime", a.paths.Data, a.paths.Runtime)
			a.noteKeptCopies()
			return nil
		}
		// The runtime is a cache and the local state this machine's own record of the installation
		// (logs, the daemons' endpoints, the browser profiles) — both went with the runtime folder
		// when it lived inside the data folder, and both go here.
		a.log("removing the downloaded runtime and this machine's local state")
		for _, dir := range []string{a.paths.Runtime, a.paths.Local} {
			if err := os.RemoveAll(dir); err != nil {
				return err
			}
		}
		a.log("the runtime is gone; %s still holds the checkouts, the state and your keys — delete it by hand when you are done with it", a.paths.Data)
		a.noteKeptCopies()
		return nil
	}
	if err := CheckDocker(ctx); err != nil {
		return err
	}
	if err := WriteOverride(a.paths); err != nil {
		return err
	}
	args := []string{"down", "--remove-orphans"}
	if !keepData {
		args = append(args, "--volumes")
	}
	a.log("removing the containers")
	if _, err := compose(ctx, a.paths, true, args...); err != nil {
		return err
	}
	if keepData {
		a.log("the volumes and %s are kept; run the launcher again to start over from them", a.paths.Data)
		return nil
	}
	a.log("the volumes are gone; %s still holds the checkouts and your keys — delete it by hand when you are done with it", a.paths.Data)
	return nil
}

// noteKeptCopies names the control folder beside the data folder when it exists. It holds whole
// copies of the data from before updates — keys and the launcher's token included — and is not
// inside the data folder, so deleting that folder by hand would leave them behind unmentioned. It
// is named, not removed: like the data folder, it is the operator's to delete.
func (a *App) noteKeptCopies() {
	control := fenceControlPath(a.paths.Data)
	if exists(control) {
		a.log("%s holds copies of the data from before updates, your keys among them — delete it together with %s", control, a.paths.Data)
	}
}

// Open points the browser at the running app, at a link that signs the operator in when one is to
// be had.
func (a *App) Open(ctx context.Context) (string, error) {
	url := a.OpenURL(ctx)
	return url, OpenBrowser(ctx, url)
}

// OpenURL is the address to open. A pairing link is offered once per start and only while it is
// fresh: the file the server wrote when the stack came up, or a link minted on the spot when there
// is no such file. What is left is the app's own address, whose login screen is a better landing
// place than a link that has already been spent — and the only answer at all when the stack is not
// running.
func (a *App) OpenURL(ctx context.Context) string {
	app := AppURL(APIPort(a.paths), a.Lang())
	a.mu.Lock()
	started, paired := a.startedAt, a.paired
	a.mu.Unlock()
	if paired {
		return app
	}
	var url string
	if a.Native() {
		url = NativePairingURL(a.paths, started)
		if url == "" {
			url, _ = a.Pair(ctx)
		}
	} else {
		// Every compose call reads the override, and `open` on its own has not written it yet.
		if err := WriteOverride(a.paths); err != nil {
			return app
		}
		telegram := a.Telegram()
		url = PairingURL(ctx, a.paths, telegram, started)
		if url == "" {
			url, _ = MintPairing(ctx, a.paths, telegram)
		}
	}
	if url == "" {
		return app
	}
	a.mu.Lock()
	a.paired = true
	a.mu.Unlock()
	return url
}

// Pair mints a fresh link that signs the operator in, for a browser that holds no session and has
// no passkey enrolled yet. The link opens once and expires; the stack has to be running, because it
// is the container that mints it.
func (a *App) Pair(ctx context.Context) (string, error) {
	if a.Native() {
		out, err := a.native.RunPython(ctx, "-m", "daedalus", "auth", "pair")
		if err != nil {
			return "", err
		}
		if url := firstPairingURL(out); url != "" {
			return url, nil
		}
		return "", errors.New("no link was printed; check that the agent is running with `daedalus-desktop status`")
	}
	if err := CheckDocker(ctx); err != nil {
		return "", err
	}
	if err := WriteOverride(a.paths); err != nil {
		return "", err
	}
	url, err := MintPairing(ctx, a.paths, a.Telegram())
	if err != nil {
		return "", err
	}
	if url == "" {
		return "", errors.New("the container printed no link; check that the stack is running with `daedalus-desktop status`")
	}
	return url, nil
}

// Status is what the status command prints and what the page renders.
type Status struct {
	Data       string `json:"data"`
	Mode       string `json:"mode"`
	ModeDetail string `json:"mode_detail"`
	Ports      string `json:"ports"`
	Configured bool   `json:"configured"`
	Repos      bool   `json:"repos"`
	Docker     string `json:"docker"`
	Running    int    `json:"running"`
	AppURL     string `json:"app_url"`
	Telegram   bool   `json:"telegram"`
	Busy       string `json:"busy"`
	Failure    string `json:"failure"`
	// FailureKey is the sentence that explains the failure, where the launcher recognises it, and
	// empty where it does not. The page shows the sentence and keeps Failure behind the disclosure.
	FailureKey string   `json:"failure_key"`
	Log        []string `json:"log"`

	// What a start is doing and how far through the piece with a known size it is. The progress
	// page draws its list from Steps and ticks it off with Stage; Done and Size turn an
	// indeterminate bar into a determinate one, and are zero when nothing knows a size.
	Stage string   `json:"stage"`
	Steps []string `json:"steps"`
	Done  int64    `json:"done"`
	Size  int64    `json:"size"`

	// Change is the agent's own code: a commit waiting for a restart, or what became of the last
	// one. Empty unless the stack is running, because the container is what holds the answer.
	Change ChangeNotice `json:"change"`

	// Upgrade is a newer launcher release, when the check found one.
	Upgrade *Offer `json:"upgrade,omitempty"`

	// Switches is what the data folder's fenced switches kept, and anything about them that needs
	// the operator: a possible late write or loss, a switch that did not finish.
	Switches fenceSummary `json:"switches"`
	// Kept is every recorded copy with its size, for the card's sizes and its Remove buttons.
	Kept []keptCopy `json:"kept"`
}

func (a *App) Status(ctx context.Context) Status {
	a.mu.Lock()
	status := Status{
		Data:       a.paths.Data,
		Mode:       string(a.mode),
		ModeDetail: a.mode.Describe(),
		Configured: a.paths.Configured(),
		Repos:      exists(a.paths.Bot) && exists(a.paths.Core),
		AppURL:     AppURL(APIPort(a.paths), a.Lang()),
		Telegram:   a.Telegram(),
		Busy:       a.busy,
		Failure:    a.failure,
		FailureKey: FailureKey(a.failure),
		Log:        append(make([]string, 0, len(a.lines)), a.lines...),
		Stage:      string(a.stage),
		Done:       a.stageDone,
		Size:       a.stageSize,
		Upgrade:    a.offer,
	}
	status.Steps = Stages(a.mode)
	a.mu.Unlock()
	status.Switches = fenceSummarize(fenceControlPath(a.paths.Data))
	status.Kept = keptCopies(fenceControlPath(a.paths.Data))
	if a.Native() {
		status.Ports = NativePorts(a.paths)
		status.Running = a.native.Running()
		status.Change = a.change(ctx)
		return status
	}
	status.Docker = DockerVersion(ctx)
	if status.Docker != "" && status.Configured {
		status.Running = a.running(ctx)
	}
	if status.Running > 0 {
		status.Change = a.change(ctx)
	}
	return status
}

// running counts the containers of this project that are up. A compose error here means the
// project is not created yet, which reads as zero.
func (a *App) running(ctx context.Context) int {
	if err := WriteOverride(a.paths); err != nil {
		return 0
	}
	out, err := compose(ctx, a.paths, true, "ps", "--quiet", "--status", "running")
	if err != nil {
		return 0
	}
	count := 0
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if strings.TrimSpace(line) != "" {
			count++
		}
	}
	return count
}

// PrintStatus writes the status as the terminal wants it.
func (a *App) PrintStatus(ctx context.Context) {
	status := a.Status(ctx)
	docker := status.Docker
	if docker == "" {
		docker = "not available"
	}
	setup := "ready"
	if !status.Configured {
		setup = "not set up (run `daedalus-desktop setup`)"
	}
	repos := "cloned"
	if !status.Repos {
		repos = "missing (cloned on the first start)"
	}
	telegram := "off — the browser app only"
	if status.Telegram {
		telegram = "on"
	}
	fmt.Printf("data         %s\n", status.Data)
	fmt.Printf("mode         %s\n", status.ModeDetail)
	fmt.Printf("setup        %s\n", setup)
	fmt.Printf("checkouts    %s\n", repos)
	if a.Native() {
		fmt.Printf("processes    %d running\n", status.Running)
		fmt.Printf("ports        %s\n", status.Ports)
	} else {
		fmt.Printf("docker       %s\n", docker)
		fmt.Printf("containers   %d running\n", status.Running)
	}
	fmt.Printf("telegram     %s\n", telegram)
	fmt.Printf("app          %s\n", status.AppURL)
	if status.Change.Pending {
		fmt.Printf("changes      ready — restart to apply: %s\n", status.Change.Summary)
	} else if status.Change.Commit != "" {
		fmt.Printf("changes      last change %s: %s\n", status.Change.Status, status.Change.Summary)
	}
	if status.Failure != "" {
		fmt.Fprintf(os.Stderr, "last error   %s\n", status.Failure)
	}
}
