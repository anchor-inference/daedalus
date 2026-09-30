package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

func TestComposeArgsPutTheServerFileFirst(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	got := composeArgs(paths, false, "up", "-d")
	want := []string{
		"compose",
		"-f", paths.Compose,
		"-f", paths.Override,
		"--env-file", paths.Env,
		"--project-name", "daedalus",
		"up", "-d",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("args are\n%v\nwant\n%v", got, want)
	}
	// Compose resolves the relative mounts of a compose file against the project directory, which
	// is the directory of the first -f. Anything but the checkout's deploy/ breaks every mount.
	if filepath.Dir(got[2]) != filepath.Join(paths.Bot, "deploy") {
		t.Fatalf("the project directory would be %s", filepath.Dir(got[2]))
	}
}

func TestComposeArgsEnableTheTelegramProfileOnlyWithAToken(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	with := strings.Join(composeArgs(paths, true, "up"), " ")
	if !strings.Contains(with, "--profile telegram") {
		t.Fatalf("no profile with a token: %s", with)
	}
	without := strings.Join(composeArgs(paths, false, "up"), " ")
	if strings.Contains(without, "--profile") {
		t.Fatalf("a profile without a token: %s", without)
	}
	if !strings.HasSuffix(with, "up") || !strings.HasSuffix(without, "up") {
		t.Fatal("the subcommand must stay last")
	}
}

func TestOverrideNamesThePublishedImagesAndFreesTelegram(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	if err := paths.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if err := WriteOverride(paths); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(paths.Override)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	for _, want := range []string{
		agentImage(),
		// The browser service is the image's other target, published under its own tag.
		"  browser:\n    image: " + browserImage(),
		`profiles: ["telegram"]`,
		// Without required:false the whole project refuses to load while the profile is off,
		// because daedalus depends on a service that is not in the project.
		"required: false",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("the override is missing %q:\n%s", want, text)
		}
	}
}

// A profile the env file names survives the telegram flag: compose reads COMPOSE_PROFILES only
// when no --profile is given, so the launcher passes each one itself.
func TestComposeArgsCarryTheProfilesTheEnvFileNames(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(paths.Env), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(paths.Env, []byte("API_PORT=8765\nCOMPOSE_PROFILES= browser, telegram ,,search\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	with := strings.Join(composeArgs(paths, true, "up"), " ")
	if !strings.Contains(with, "--profile telegram --profile browser --profile search up") {
		t.Fatalf("with a token: %s", with)
	}
	without := strings.Join(composeArgs(paths, false, "up"), " ")
	if !strings.Contains(without, "--profile browser --profile telegram --profile search up") {
		t.Fatalf("without a token: %s", without)
	}
}

func TestGitRunsInTheAgentImageOverTheDataFolder(t *testing.T) {
	paths, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Join(gitArgs(paths, "-C", "/work/daedalus", "status"), " ")
	if !strings.Contains(args, paths.Data+":/work") {
		t.Fatalf("git would not see the data folder: %s", args)
	}
	// The agent image, not an image of its own: a second one would be a second download for one binary.
	if !strings.Contains(args, "--entrypoint git "+agentImage()) {
		t.Fatalf("git does not run in the agent image: %s", args)
	}
	if os.Getuid() >= 0 && !strings.Contains(args, fmt.Sprintf("--user %d:%d", os.Getuid(), os.Getgid())) {
		t.Fatalf("the checkout would belong to root, and the launcher could not write .env into it: %s", args)
	}
}

func TestAForkOverridesTheRemote(t *testing.T) {
	t.Setenv("DAEDALUS_GIT_REMOTE", "https://example.invalid/fork")
	t.Setenv("DAEDALUS_CORE_GIT_REMOTE", "https://example.invalid/core")
	if botRemote() != "https://example.invalid/fork" || coreRemote() != "https://example.invalid/core" {
		t.Fatal("the fork remotes are ignored")
	}
}

// A fork's checkout running upstream's image is a mismatch nothing would report, so the image
// follows the remote the code comes from.
func TestTheImageFollowsTheRemote(t *testing.T) {
	t.Setenv("DAEDALUS_GIT_REMOTE", "https://github.com/Someone-Else/daedalus")
	if got := agentImage(); got != "ghcr.io/someone-else/daedalus:latest" {
		t.Fatalf("got %q", got)
	}
	for remote, want := range map[string]string{
		"https://github.com/anchor-inference/daedalus":     "anchor-inference",
		"https://github.com/anchor-inference/daedalus.git": "anchor-inference",
		"git@github.com:a-fork/daedalus.git":               "a-fork",
		"ssh://git@github.com/a-fork/daedalus":             "a-fork",
		"https://github.com:443/a-fork/daedalus":           "a-fork",
		"nonsense":                                         defaultImageOwner,
	} {
		if got := imageOwner(remote); got != want {
			t.Fatalf("%s names owner %q, want %q", remote, got, want)
		}
	}
}

// A launcher started from Finder inherits a PATH without Docker Desktop's client in it. Telling an
// operator whose Docker is running that Docker is missing is the worst answer available, so the
// installers' own locations are searched before giving up.
func TestDockerIsFoundWherePATHDoesNotName(t *testing.T) {
	never := func(string) (string, error) { return "", errors.New("not in PATH") }
	found := func(want string) func(string) bool {
		return func(path string) bool { return path == want }
	}
	for _, c := range []struct{ goos, home, want string }{
		{"darwin", "/Users/someone", "/usr/local/bin/docker"},
		{"darwin", "/Users/someone", "/opt/homebrew/bin/docker"},
		{"darwin", "/Users/someone", "/Users/someone/.docker/bin/docker"},
		{"darwin", "/Users/someone", "/Applications/Docker.app/Contents/Resources/bin/docker"},
		{"linux", "/home/someone", "/usr/bin/docker"},
		{"linux", "/home/someone", "/usr/local/bin/docker"},
		{"linux", "/home/someone", "/snap/bin/docker"},
		{"windows", `C:\Users\someone`, `C:\Program Files\Docker\Docker\resources\bin\docker.exe`},
	} {
		if got := findDocker(c.goos, c.home, never, found(c.want)); got != c.want {
			t.Fatalf("%s: found %q, want %q", c.goos, got, c.want)
		}
	}
	// Nothing anywhere is an empty answer, which is what CheckDocker turns into its one message.
	if got := findDocker("darwin", "/Users/someone", never, func(string) bool { return false }); got != "" {
		t.Fatalf("found %q where there is no docker", got)
	}
	// A home the process cannot name must not turn into a path rooted at the disk.
	for _, candidate := range dockerCandidates("darwin", "") {
		if strings.HasPrefix(candidate, "/.docker") {
			t.Fatalf("a nameless home produced %s", candidate)
		}
	}
}

// PATH comes first: an operator who put docker somewhere of their own meant it.
func TestPATHWinsOverTheKnownLocations(t *testing.T) {
	mine := func(string) (string, error) { return "/opt/mine/docker", nil }
	if got := findDocker("darwin", "/Users/someone", mine, func(string) bool { return true }); got != "/opt/mine/docker" {
		t.Fatalf("got %q", got)
	}
}

func TestDockerRunsWithItsOwnFolderOnPATH(t *testing.T) {
	skipOnWindowsForAnotherSystemsPaths(t)
	path := dockerSearchPath("darwin", "/Users/o", "/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/bin:/bin")
	want := "/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin:/Users/o/.docker/bin:/usr/bin:/bin"
	if path != want {
		t.Fatalf("PATH = %q, want %q", path, want)
	}
}

func TestDockerPATHWithoutAClientStillNamesTheKnownFolders(t *testing.T) {
	skipOnWindowsForAnotherSystemsPaths(t)
	path := dockerSearchPath("linux", "", "", "")
	if path != "/usr/bin:/usr/local/bin:/snap/bin" {
		t.Fatalf("PATH = %q", path)
	}
}

// skipOnWindowsForAnotherSystemsPaths: dockerSearchPath joins with this machine's separators, as it
// must — it is only ever asked about the system it runs on. Asked on Windows about a Mac's or a
// Linux machine's folders it answers in backslashes and semicolons, which says nothing about the
// code; what these tests check is the order of the folders on those two systems.
func skipOnWindowsForAnotherSystemsPaths(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the paths are a Mac's and a Linux machine's, joined with this machine's separators")
	}
}
