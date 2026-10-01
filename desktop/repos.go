package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// The two repositories the stack runs: the bot and the core it is built on. A fork is used by
// setting DAEDALUS_GIT_REMOTE or DAEDALUS_CORE_GIT_REMOTE before starting the launcher.
const (
	defaultBotRemote  = "https://github.com/anchor-inference/daedalus"
	defaultCoreRemote = "https://github.com/anchor-inference/protocore-exp"
)

// branch is what a checkout follows. There is one: the published code is what main says it is.
const branch = "main"

// tarballLimit caps what a single download may expand to. Both repositories are a few megabytes;
// this is the wall between a bad URL and a full disk, not a size anyone should approach.
const tarballLimit = 512 << 20

func botRemote() string {
	if remote := strings.TrimSpace(os.Getenv("DAEDALUS_GIT_REMOTE")); remote != "" {
		return remote
	}
	return defaultBotRemote
}

func coreRemote() string {
	if remote := strings.TrimSpace(os.Getenv("DAEDALUS_CORE_GIT_REMOTE")); remote != "" {
		return remote
	}
	return defaultCoreRemote
}

// repoPath is the owner/repo of a git remote, whatever form the remote is written in — and only
// when the remote is on GitHub. The tarball comes from codeload.github.com, so a remote pointing
// anywhere else would be answered with a GitHub repository of the same owner and name: a different
// repository, downloaded and unpacked without a word. Dropping the host here is what makes
// fetchTarball's error the one the operator sees.
func repoPath(remote string) string {
	rest := strings.TrimSuffix(strings.TrimSpace(remote), ".git")
	if _, after, ok := strings.Cut(rest, "://"); ok {
		rest = after
		if _, host, ok := strings.Cut(rest, "@"); ok { // ssh://git@host/owner/repo
			rest = host
		}
	} else if _, host, ok := strings.Cut(rest, "@"); ok { // git@host:owner/repo
		rest = strings.Replace(host, ":", "/", 1)
	}
	parts := strings.Split(strings.Trim(rest, "/"), "/")
	if len(parts) < 3 {
		return ""
	}
	host, _, _ := strings.Cut(parts[0], ":") // a port on the host, which GitHub never has
	if host != "github.com" && host != "www.github.com" {
		return ""
	}
	return parts[1] + "/" + parts[2]
}

// tarballURL is where GitHub serves a branch as a gzipped tar. A checkout is made from this rather
// than by cloning: it needs no git on the host and no git in a container of its own, and what
// arrives is the tree, which is all a checkout of a published branch ever was.
func tarballURL(remote string) string {
	owner := repoPath(remote)
	if owner == "" {
		return ""
	}
	return "https://codeload.github.com/" + owner + "/tar.gz/refs/heads/" + branch
}

// fetchTarball downloads the whole archive before anything is written: a download that fails
// halfway must leave the checkout it was going to replace exactly as it was.
func fetchTarball(ctx context.Context, url string) ([]byte, error) {
	return fetchTarballReporting(ctx, url, "", nil)
}

// fetchTarballReporting is fetchTarball with the page told how much has arrived. codeload streams
// the archive as it makes it and sends no length, so this is a count without a total: the page shows
// the megabytes going up rather than a bar it would have to invent.
func fetchTarballReporting(ctx context.Context, url, name string, report reporter) ([]byte, error) {
	if url == "" {
		return nil, errors.New("the checkouts are fetched as GitHub tarballs; DAEDALUS_GIT_REMOTE and DAEDALUS_CORE_GIT_REMOTE must name a github.com/owner/repo")
	}
	// There is no hash for this archive — it is whatever the branch holds today — so what comes back
	// is unpacked into the checkout and run as the supervisor and the key proxy on the next start.
	// The host is fixed by tarballURL, which builds it rather than taking it from the remote, and
	// onCodeload refuses a redirect that leaves https or leaves that host: without it, a downgrade
	// to http on the way is all it would take to choose the code.
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	response, err := onCodeload.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", url, response.StatusCode)
	}
	total := response.ContentLength
	if total < 0 {
		total = 0
	}
	return io.ReadAll(newCountingReader(io.LimitReader(response.Body, tarballLimit), Activity{Kind: "download", Name: name, Total: total}, report))
}

// unpackTarball writes the archive into dir, dropping the single folder GitHub wraps a tree in.
// Files already there are overwritten and everything else in dir is left alone, so a checkout keeps
// its .git, its .env and whatever the agent built in it.
func unpackTarball(archive []byte, dir string) error {
	return unpackTarballReporting(archive, dir, "", nil)
}

func unpackTarballReporting(archive []byte, dir, name string, report reporter) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	zipped, err := gzip.NewReader(newCountingReader(bytes.NewReader(archive), Activity{Kind: "unpack", Name: name, Total: int64(len(archive))}, report))
	if err != nil {
		return err
	}
	defer zipped.Close()
	reader := tar.NewReader(zipped)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		// Every path in the archive is under one folder named after the repository and the commit;
		// that folder is the tarball's, not the tree's, so the first segment goes.
		name := strings.TrimPrefix(header.Name, "./")
		_, name, ok := strings.Cut(name, "/")
		if !ok || name == "" {
			continue
		}
		target, err := safeJoin(dir, name)
		if err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := ensureParents(dir, target); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := ensureParents(dir, filepath.Dir(target)); err != nil {
				return err
			}
			mode := os.FileMode(header.Mode).Perm()
			if mode == 0 {
				mode = 0o644
			}
			// A link where the file goes is not followed: it is taken out and the file written in
			// its place. Cleaning the entry's own name is only half of keeping an archive inside
			// the checkout it is unpacked into.
			if info, err := os.Lstat(target); err == nil && info.Mode()&os.ModeSymlink != 0 {
				if err := os.Remove(target); err != nil {
					return err
				}
			}
			file, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
			if err != nil {
				return err
			}
			if _, err := io.Copy(file, io.LimitReader(reader, tarballLimit)); err != nil {
				file.Close()
				return err
			}
			if err := file.Close(); err != nil {
				return err
			}
		default:
			// A published source tree is files and directories. Everything else an archive may
			// carry — a symlink, a hard link, a device node — is refused rather than skipped: a
			// checkout missing a link it expected is a failure worth seeing, and a link is how an
			// archive nobody checked writes outside the folder it was unpacked into.
			return fmt.Errorf("archive entry %q is not a file or a directory (type %q); the checkouts are plain source trees", name, string(header.Typeflag))
		}
	}
}

// ensureParents makes the directories from root down to target, one segment at a time, and refuses
// to walk through a symbolic link. MkdirAll would happily follow one that is already there — put
// there by the agent, or by an archive unpacked before this check existed — and every file below
// that segment would then be written outside the checkout.
func ensureParents(root, target string) error {
	relative, err := filepath.Rel(root, target)
	if err != nil {
		return err
	}
	if relative == "." {
		return nil
	}
	current := root
	for _, segment := range strings.Split(relative, string(os.PathSeparator)) {
		current = filepath.Join(current, segment)
		info, err := os.Lstat(current)
		switch {
		case os.IsNotExist(err):
			if err := os.Mkdir(current, 0o755); err != nil {
				return err
			}
		case err != nil:
			return err
		case info.Mode()&os.ModeSymlink != 0:
			return fmt.Errorf("%q is a symbolic link; an archive is not unpacked through one", current)
		case !info.IsDir():
			return fmt.Errorf("%q is not a directory", current)
		}
	}
	return nil
}

// safeJoin refuses a name that would land outside dir. An archive is downloaded code and is treated
// as such: "../../.ssh/authorized_keys" is a plausible entry in an archive nobody checked.
func safeJoin(dir, name string) (string, error) {
	clean := path.Clean("/" + name)
	if clean == "/" {
		return "", fmt.Errorf("archive entry %q has no name", name)
	}
	return filepath.Join(dir, filepath.FromSlash(clean[1:])), nil
}

// gitRunner runs git against one checkout under the data folder. There are two of them: Docker mode
// runs git inside the agent's own image, so a host with no git still gets a checkout with a history;
// native mode runs the git in the portable runtime. Everything that makes a checkout is written
// against this one signature, so neither path has a copy of the other's steps.
type gitRunner func(ctx context.Context, name string, args ...string) (string, error)

// dockerGit is the Docker-mode runner.
func dockerGit(p Paths) gitRunner {
	return func(ctx context.Context, name string, args ...string) (string, error) {
		return runDocker(ctx, gitArgs(p, append([]string{"-C", "/work/" + name}, args...)...)...)
	}
}

// gitArgs runs git inside the agent's own container image: git is not assumed to be installed on
// the host, and that image is there by definition — it is the one the stack runs.
func gitArgs(p Paths, args ...string) []string {
	run := []string{"run", "--rm", "-v", p.Data + ":/work", "-w", "/work"}
	// The container writes into the data folder as whoever runs it. Left to itself that is root, and a
	// checkout root owns is one the launcher (and the operator) can no longer write .env into; so on the
	// systems that have a uid, git runs as the launcher's own user. Windows has none, and Docker Desktop
	// maps the files to the desktop user there anyway.
	if uid := os.Getuid(); uid >= 0 {
		run = append(run, "--user", fmt.Sprintf("%d:%d", uid, os.Getgid()), "-e", "HOME=/tmp")
	}
	return append(append(run, "--entrypoint", "git", agentImage()), args...)
}

// commitAll records the tree as one commit. The checkouts have a real history and no remote: that
// is what local self-development works against — the agent commits, and a bad commit is undone by
// going back to the one before it — and an update is the next commit on top, so the history stays
// linear and nothing the agent did is thrown away by a fetch.
func commitAll(ctx context.Context, git gitRunner, name, message string) error {
	if _, err := git(ctx, name, "add", "-A"); err != nil {
		return err
	}
	pending, err := git(ctx, name, "status", "--porcelain")
	if err != nil {
		return err
	}
	if strings.TrimSpace(pending) == "" {
		return nil
	}
	_, err = git(ctx, name,
		"-c", "user.name=daedalus-desktop",
		"-c", "user.email=daedalus-desktop@localhost",
		"commit", "-q", "-m", message)
	return err
}

// removeTracked deletes everything the last commit holds, so files dropped upstream do not survive
// an update. Untracked and ignored files — the .env, a virtualenv, whatever a session left — are
// not touched, which is the difference between this and emptying the folder.
func removeTracked(ctx context.Context, git gitRunner, dir, name string) error {
	listing, err := git(ctx, name, "ls-files", "-z")
	if err != nil {
		return err
	}
	for _, entry := range strings.Split(listing, "\x00") {
		if entry == "" {
			continue
		}
		target, err := safeJoin(dir, entry)
		if err != nil {
			return err
		}
		if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
			return err
		}
		pruneEmpty(dir, filepath.Dir(target))
	}
	return nil
}

// pruneEmpty removes the directories a deleted file leaves behind, up to but never including the
// checkout root. A directory dropped upstream would otherwise survive every update as an empty
// shell. A directory that still holds something — an untracked file, a virtualenv — makes Remove
// fail, which is the signal to stop climbing.
func pruneEmpty(root, dir string) {
	for dir != root && strings.HasPrefix(dir, root+string(os.PathSeparator)) {
		if os.Remove(dir) != nil {
			return
		}
		dir = filepath.Dir(dir)
	}
}

type repo struct {
	dir    string
	name   string
	remote string
}

func repos(p Paths) []repo {
	return []repo{
		{p.Bot, "daedalus", botRemote()},
		{p.Core, "protocore-exp", coreRemote()},
	}
}

// EnsureRepos makes what is missing. An existing checkout is left as it is — it may hold the
// agent's own work in progress — and is only moved by Update.
func EnsureRepos(ctx context.Context, p Paths, git gitRunner, log func(string, ...any)) error {
	return ensureReposFrom(ctx, p, git, log, nil, nil)
}

// ensureReposFrom is EnsureRepos with the page told what is happening, and with the release's own
// copy of the code (seed.go) used instead of the network when the installation carries one. That copy
// is the tree codeload gives for the release's commit — the same git archive, the same export-ignore —
// so a first run from it is the first run a download would have made, only of the release's commit
// rather than of whatever main holds today; the next update moves it on as it moves any checkout.
func ensureReposFrom(ctx context.Context, p Paths, git gitRunner, log func(string, ...any), report reporter, seed *Seed) error {
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	for _, r := range repos(p) {
		if exists(r.dir) {
			continue
		}
		var archive []byte
		message := "the published " + branch
		if carried, ok := seed.repo(r.name, r.remote); ok {
			log("%s comes with the installation (commit %s)", r.name, shortCommit(carried.Commit))
			body, err := seed.read(carried, r.name, report)
			if err != nil {
				log("the installation's copy of %s is not usable (%v); downloading it instead", r.name, err)
			} else {
				archive = body
				message = "the release's " + r.name + " at " + carried.Commit
			}
		}
		if archive == nil {
			log("fetching %s", r.name)
			body, err := fetchTarballReporting(ctx, tarballURL(r.remote), r.name, report)
			if err != nil {
				return err
			}
			archive = body
			log("fetched %s (%.1f MB)", r.name, float64(len(body))/1e6)
		}
		if err := os.MkdirAll(r.dir, 0o755); err != nil {
			return err
		}
		if err := unpackTarballReporting(archive, r.dir, r.name, report); err != nil {
			return err
		}
		if _, err := git(ctx, r.name, "init", "-q", "-b", branch); err != nil {
			return err
		}
		if err := commitAll(ctx, git, r.name, message); err != nil {
			return err
		}
	}
	return nil
}

// shortCommit is a commit as the log shows one.
func shortCommit(commit string) string {
	if len(commit) > 12 {
		return commit[:12]
	}
	return commit
}

// UpdateRepos moves both checkouts to what is published, as a commit on top of what they hold. The
// agent's own merged work arrives this way, and so does everything else in the published tree.
func UpdateRepos(ctx context.Context, p Paths, git gitRunner, log func(string, ...any)) error {
	archives, err := FetchRepos(ctx, p)
	if err != nil {
		return err
	}
	return ApplyRepos(ctx, p, git, log, archives)
}

// fixtureTarball is set by fixture_stack.go in a build tagged upgradefixture, and nil otherwise: it
// lets an end-to-end run move the checkouts to a tree of its own instead of what GitHub publishes.
var fixtureTarball func(name string) ([]byte, bool)

// FetchRepos downloads what both checkouts would be moved to, and changes nothing. An update
// fetches first so that the next version's environment can be built from exactly these trees
// before the data folder is switched, and the move itself then applies the same bytes.
func FetchRepos(ctx context.Context, p Paths) (map[string][]byte, error) {
	archives := make(map[string][]byte)
	for _, r := range repos(p) {
		if !exists(r.dir) {
			continue
		}
		if fixtureTarball != nil {
			if archive, ok := fixtureTarball(r.name); ok {
				archives[r.name] = archive
				continue
			}
		}
		archive, err := fetchTarball(ctx, tarballURL(r.remote))
		if err != nil {
			return nil, err
		}
		archives[r.name] = archive
	}
	return archives, nil
}

// ApplyRepos moves each checkout to its fetched archive, as a commit on top of what it holds.
func ApplyRepos(ctx context.Context, p Paths, git gitRunner, log func(string, ...any), archives map[string][]byte) error {
	for _, r := range repos(p) {
		archive, ok := archives[r.name]
		if !ok || !exists(r.dir) {
			continue
		}
		log("updating %s", r.name)
		if err := removeTracked(ctx, git, r.dir, r.name); err != nil {
			return err
		}
		if err := unpackTarball(archive, r.dir); err != nil {
			return err
		}
		if err := commitAll(ctx, git, r.name, "the published "+branch); err != nil {
			return err
		}
	}
	return nil
}
