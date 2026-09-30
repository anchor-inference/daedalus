package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// A release of the launcher is a GitHub release whose tag is desktop-v<major>.<minor>.<patch>. The
// launcher asks for the list the same way install.sh does, anonymously, and never installs what it
// finds on its own: the check only tells the operator, and `daedalus-desktop upgrade` is what
// installs — after a yes, and with the data protected first (protect.go, upgrade.go).
//
// What a release changes is two things with separate versions. The launcher binary is the release's
// own. The code the stack runs is whatever the launcher fetches into the checkouts — today the tip
// of main and the :latest image, whatever the release (repos.go). An upgrade therefore swaps the
// binary and then moves the checkouts the way the new binary does, and the data is protected before
// either — a whole kept copy behind the writer fence on Linux with ext4, a verified backup elsewhere —
// because the app's database migrations run on the start that follows and are not assumed to be
// reversible.
const releaseTagPrefix = "desktop-v"

// releaseCheckInterval is how often a running launcher asks. Twice a day is plenty for something
// the operator installs by hand, and is far inside the anonymous API's limit. releaseCheckDelay
// keeps the first check out of a start's own busiest minute. Variables for the tests.
var (
	releaseCheckInterval = 12 * time.Hour
	releaseCheckDelay    = 30 * time.Second
)

// releaseBodyLimit caps the release listing; thirty releases are a few hundred kilobytes.
const releaseBodyLimit = 8 << 20

// assetLimit caps a downloaded release archive. The archives are tens of megabytes.
const assetLimit = 512 << 20

// Release is one published launcher release, as much of it as the upgrade needs.
type Release struct {
	Tag     string         `json:"tag_name"`
	Draft   bool           `json:"draft"`
	Pre     bool           `json:"prerelease"`
	URL     string         `json:"html_url"`
	Assets  []ReleaseAsset `json:"assets"`
	version semver
}

// ReleaseAsset is one file attached to a release.
type ReleaseAsset struct {
	Name string `json:"name"`
	URL  string `json:"browser_download_url"`
	Size int64  `json:"size"`
}

// semver is major.minor.patch. A tag with anything after the patch number — -rc1, -preview — is a
// prerelease and is never offered as an upgrade: an operator who wants one installs it by hand.
type semver struct {
	major, minor, patch int
	ok                  bool
}

func parseVersion(tag string) semver {
	rest, found := strings.CutPrefix(strings.TrimSpace(tag), releaseTagPrefix)
	if !found {
		return semver{}
	}
	parts := strings.Split(rest, ".")
	if len(parts) != 3 {
		return semver{}
	}
	numbers := [3]int{}
	for i, part := range parts {
		// Digits only: Atoi takes a sign, and "+1" is not a version any installer would accept.
		n, err := strconv.Atoi(part)
		if err != nil || n < 0 || part == "" || strings.Trim(part, "0123456789") != "" || (len(part) > 1 && part[0] == '0') {
			return semver{}
		}
		numbers[i] = n
	}
	return semver{numbers[0], numbers[1], numbers[2], true}
}

func (v semver) newerThan(other semver) bool {
	if v.major != other.major {
		return v.major > other.major
	}
	if v.minor != other.minor {
		return v.minor > other.minor
	}
	return v.patch > other.patch
}

// releasesAPI is the repository API the check reads. DAEDALUS_RELEASES_API points it elsewhere — a
// fork's repository, or a local fixture server in the tests. A fork set with DAEDALUS_GIT_REMOTE
// gets its own releases.
func releasesAPI() string {
	if api := strings.TrimSpace(os.Getenv("DAEDALUS_RELEASES_API")); api != "" {
		return strings.TrimRight(api, "/")
	}
	repo := repoPath(botRemote())
	if repo == "" {
		repo = repoPath(defaultBotRemote)
	}
	return "https://api.github.com/repos/" + repo
}

// allowedURL is the rule for every address the upgrade reads from: https, or plain http only to this
// machine, which is what a fixture server is. A release listing that sends the download elsewhere
// over http is refused rather than followed.
func allowedURL(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil {
		return err
	}
	switch parsed.Scheme {
	case "https":
		return nil
	case "http":
		host := parsed.Hostname()
		if host == "localhost" {
			return nil
		}
		if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
			return nil
		}
	}
	return fmt.Errorf("%s: only https (or http to this machine) is read", raw)
}

// releaseClient refuses a redirect that breaks the rule above: GitHub sends release downloads to its
// object storage, which is https, and anything else on the way is not followed.
var releaseClient = &http.Client{
	Timeout: 10 * time.Minute,
	CheckRedirect: func(request *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return errors.New("too many redirects")
		}
		return allowedURL(request.URL.String())
	},
}

func getLimited(ctx context.Context, raw string, limit int64) ([]byte, error) {
	if err := allowedURL(raw); err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("User-Agent", "daedalus-desktop/"+version)
	response, err := releaseClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", raw, response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("%s: larger than %d bytes", raw, limit)
	}
	return body, nil
}

// FetchReleases lists the published launcher releases, newest version first. Drafts (which the
// anonymous API does not return anyway) and prereleases are left out.
func FetchReleases(ctx context.Context) ([]Release, error) {
	body, err := getLimited(ctx, releasesAPI()+"/releases?per_page=30", releaseBodyLimit)
	if err != nil {
		return nil, err
	}
	var all []Release
	if err := json.Unmarshal(body, &all); err != nil {
		return nil, fmt.Errorf("the release listing is not what GitHub returns: %w", err)
	}
	var out []Release
	for _, release := range all {
		release.version = parseVersion(release.Tag)
		if release.Draft || release.Pre || !release.version.ok {
			continue
		}
		out = append(out, release)
	}
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j].version.newerThan(out[j-1].version); j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out, nil
}

// platformAsset is the archive a machine like this one installs from, the same choice install.sh
// and install.ps1 make.
func platformAsset(goos, goarch string) (string, error) {
	switch goos {
	case "darwin":
		return "Daedalus-macOS.zip", nil
	case "linux":
		if goarch == "amd64" || goarch == "arm64" {
			return "daedalus-desktop-linux-" + goarch + ".tar.gz", nil
		}
	case "windows":
		if goarch == "amd64" {
			return "daedalus-desktop-windows-amd64.zip", nil
		}
	}
	return "", fmt.Errorf("there is no published build for %s/%s", goos, goarch)
}

func (r Release) asset(name string) (ReleaseAsset, bool) {
	for _, asset := range r.Assets {
		if asset.Name == name {
			return asset, true
		}
	}
	return ReleaseAsset{}, false
}

// Offer is what the check found: a release newer than this launcher that has everything an upgrade
// needs attached. A release whose archive is not attached yet — the workflow creates the release a
// few minutes before it uploads to it — is not offered.
type Offer struct {
	From  string `json:"from"`
	To    string `json:"to"`
	URL   string `json:"url"`
	Asset string `json:"asset"`
	// Command is the exact command that installs it on this machine, for the page, the desktop
	// notification and the app to show: this launcher's own path and data folder, quoted for the
	// shell of this system.
	Command string `json:"command,omitempty"`
}

// upgradeCommandLine is `<this launcher> upgrade --data <data>` as it is typed on this system.
func upgradeCommandLine(p Paths) string {
	exe, err := os.Executable()
	if err != nil {
		return "daedalus-desktop upgrade"
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return commandLineFor(runtime.GOOS, exe, p.Data)
}

func commandLineFor(goos, exe, data string) string {
	if goos == "windows" {
		return "& " + powershellQuote(exe) + " upgrade --data " + powershellQuote(data)
	}
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }
	return quote(exe) + " upgrade --data " + quote(data)
}

// FindUpgrade answers whether there is anything to upgrade to. A launcher built without a release
// version (a dev build) has nothing to compare, and is told so rather than offered everything.
func FindUpgrade(ctx context.Context, current string) (*Offer, *Release, error) {
	running := parseVersion(current)
	if !running.ok {
		return nil, nil, fmt.Errorf("this launcher is %q, not a release build; there is nothing to compare it with", current)
	}
	releases, err := FetchReleases(ctx)
	if err != nil {
		return nil, nil, err
	}
	name, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return nil, nil, err
	}
	// A newer release this launcher will not take because it is not signed is said as such — not
	// passed over as if there were nothing newer: a publishing mistake or a stripped
	// signature must be visible, not look like "up to date".
	unsigned := ""
	nothing := func() (*Offer, *Release, error) {
		if unsigned != "" {
			return nil, nil, fmt.Errorf("%w: %s is published but not signed by this launcher's release key, so it is not installed", errUnsignedRelease, unsigned)
		}
		return nil, nil, nil
	}
	for i := range releases {
		release := releases[i]
		if !release.version.newerThan(running) {
			return nothing()
		}
		if _, ok := release.asset(name); !ok {
			continue
		}
		if _, ok := release.asset("SHA256SUMS"); !ok {
			continue
		}
		if _, ok := release.asset(signatureAsset); !ok && len(trustedReleaseKeys) > 0 {
			if unsigned == "" {
				unsigned = release.Tag
			}
			continue
		}
		return &Offer{From: current, To: release.Tag, URL: release.URL, Asset: name}, &release, nil
	}
	return nothing()
}

// errUnsignedRelease is FindUpgrade's answer when a newer release exists and is not signed.
var errUnsignedRelease = errors.New("a newer release is not signed")

// authenticationNote says what an offer's checks will be worth in this build.
func authenticationNote() string {
	if len(trustedReleaseKeys) == 0 {
		return "this launcher carries no release key: the release will be checked against its SHA256SUMS only, which catches a broken download, not a forged release"
	}
	return "the release must be signed by this launcher's release key"
}

// updateCheckOff is the operator's way to keep the launcher from asking at all.
func updateCheckOff() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("DAEDALUS_UPDATE_CHECK"))) {
	case "off", "0", "false", "no":
		return true
	}
	return false
}

func upgradeDir(p Paths) string { return filepath.Join(p.Data, "upgrade") }

func notifiedFile(p Paths) string { return filepath.Join(upgradeDir(p), "notified") }

// notified reports whether this version was already announced on the desktop, and markNotified
// records that it was — only after the notification was actually shown, so one that could not be
// shown is tried again at the next check rather than never.
func notified(p Paths, tag string) bool {
	body, err := os.ReadFile(notifiedFile(p))
	return err == nil && strings.TrimSpace(string(body)) == tag
}

func markNotified(p Paths, tag string) {
	if err := os.MkdirAll(upgradeDir(p), 0o700); err == nil {
		_ = os.WriteFile(notifiedFile(p), []byte(tag+"\n"), 0o600)
	}
}

// UpgradeNotification is the desktop notification for an offer, in the launcher's language. It
// says what to do, because installing is never done for the operator. It carries no link: a click
// focuses the launcher's own window, which is no place for a page on GitHub.
func UpgradeNotification(lang Lang, offer Offer) Notification {
	body := fmt.Sprintf(Translate(lang, "upgrade.notify.body"), strings.TrimPrefix(offer.From, releaseTagPrefix))
	if offer.Command != "" {
		body += "\n" + offer.Command
	}
	return Notification{
		Title: fmt.Sprintf(Translate(lang, "upgrade.notify.title"), strings.TrimPrefix(offer.To, releaseTagPrefix)),
		Body:  body,
	}
}

// CheckReleases runs for as long as the launcher does: it asks once shortly after the start and then
// every releaseCheckInterval, records what it found for the status page, and hands a notification
// for a release it has not announced before to show. A failed check is a line in the log and
// nothing else — a laptop offline is not a problem worth a notification.
func (a *App) CheckReleases(ctx context.Context, show func(Notification) error) {
	if updateCheckOff() || !parseVersion(version).ok {
		return
	}
	wait := releaseCheckDelay
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		wait = releaseCheckInterval
		offer, _, err := FindUpgrade(ctx, version)
		if err != nil {
			a.log("could not check for a newer launcher: %v", err)
			continue
		}
		if offer != nil {
			offer.Command = upgradeCommandLine(a.paths)
		}
		a.mu.Lock()
		same := offer != nil && a.offer != nil && a.offer.To == offer.To
		a.offer = offer
		a.mu.Unlock()
		if offer == nil {
			continue
		}
		if !same {
			a.log("%s is available (this is %s): close the launcher and run %s — it keeps the data from before first", offer.To, offer.From, offer.Command)
		}
		if !notified(a.paths, offer.To) {
			if err := show(UpgradeNotification(a.Lang(), *offer)); err == nil {
				markNotified(a.paths, offer.To)
			}
		}
	}
}

// CheckUpdateCommand is `daedalus-desktop check-update`: the same check, once, in a terminal. It
// never installs anything.
func CheckUpdateCommand(ctx context.Context, pathsForCommand Paths) error {
	offer, _, err := FindUpgrade(ctx, version)
	if err != nil {
		return err
	}
	if offer == nil {
		fmt.Printf("%s is the newest launcher release\n", version)
		return nil
	}
	defer fmt.Printf("note: %s\n", authenticationNote())
	fmt.Printf("%s is available (this is %s)\nrelease notes: %s\nto install it, close the launcher and run:\n  %s\nit asks first, and keeps the data and the launcher from before so that a failure puts both back\n", offer.To, offer.From, offer.URL, upgradeCommandLine(pathsForCommand))
	return nil
}

// powershellQuote is a PowerShell single-quoted string. PowerShell takes not only ' but also the
// typographic single quotes ‘ ’ ‚ ‛ as the quote character, and each is escaped by doubling it — a
// path like C:\Users\O’Brien would otherwise end the string early.
func powershellQuote(s string) string {
	var b strings.Builder
	b.WriteByte('\'')
	for _, r := range s {
		switch r {
		case '\'', '\u2018', '\u2019', '\u201A', '\u201B':
			b.WriteRune(r)
		}
		b.WriteRune(r)
	}
	b.WriteByte('\'')
	return b.String()
}
