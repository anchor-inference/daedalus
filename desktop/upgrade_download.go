package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

// An upgrade from the app comes in two halves. The first is this file: while the app stays in use,
// the launcher downloads the release beside the installation and checks it — the signature over
// SHA256SUMS, the archive against SHA256SUMS — and says how far it has got. The second is the
// upgrade itself (upgrade.go), which the app starts once the download is ready and which then takes
// its files from here instead of the network. The download is the long part — a quarter of a
// gigabyte, minutes on a slow link — and it used to happen after the window had already closed,
// with nothing on the screen to say it was happening at all.
//
// The files live in the runtime folder (local.go), which is outside both the data folder and the
// installation: neither the backup nor the swap ever carries them. The upgrade checks them again
// before using them, so what is trusted is the release key, never this folder.

// Download is what the app is told about the download, in the launcher's status.
type Download struct {
	// State is idle, running, done or failed.
	State string `json:"state"`
	Tag   string `json:"tag,omitempty"`
	// Done and Total are bytes of the archive; Total is zero while it is unknown.
	Done  int64  `json:"done"`
	Total int64  `json:"total"`
	Error string `json:"error,omitempty"`
	// Ready says the release the launcher offers is downloaded and checked, and can be installed.
	Ready bool `json:"ready"`
}

// downloadStall is how long a download may go without a byte before it is given up: a link that has
// died should end in a message and a Retry button, not in a bar that never moves again.
var downloadStall = 90 * time.Second

func stagedDir(p Paths, tag string) string { return filepath.Join(p.Runtime, "upgrade", tag) }

// stagedRelease is the three files of a release downloaded for tag, and whether they were checked.
type stagedRelease struct {
	archive, sums, sig string
	verified           string
}

func stagedFiles(p Paths, tag, asset string) stagedRelease {
	dir := stagedDir(p, tag)
	return stagedRelease{
		archive:  filepath.Join(dir, asset),
		sums:     filepath.Join(dir, "SHA256SUMS"),
		sig:      filepath.Join(dir, signatureAsset),
		verified: filepath.Join(dir, "verified"),
	}
}

// ready says the files for this offer are on disk and were checked when they were downloaded.
func (s stagedRelease) ready() bool {
	for _, name := range []string{s.archive, s.sums, s.verified} {
		if info, err := os.Stat(name); err != nil || !info.Mode().IsRegular() {
			return false
		}
	}
	return true
}

// downloadState is the app's half of the status: the download in progress or the last one, and
// whether the release on offer is ready.
func (a *App) downloadState() Download {
	a.mu.Lock()
	offer := a.offer
	state := a.download
	a.mu.Unlock()
	if state.State == "" {
		state.State = "idle"
	}
	if offer != nil && offer.Asset != "" {
		state.Ready = stagedFiles(a.paths, offer.To, offer.Asset).ready()
		if state.Ready && state.State != "running" {
			state.State, state.Tag = "done", offer.To
		}
	}
	if state.State == "running" {
		state.Done = a.downloaded.Load()
	}
	return state
}

// StartDownload begins downloading the release on offer and returns at once; the status says how it
// goes. It does not claim the launcher: nothing of the installation changes while it runs, so a
// restart or an extra from the app may run beside it.
func (a *App) StartDownload(ctx context.Context) error {
	a.mu.Lock()
	offer, release := a.offer, a.release
	if a.download.State == "running" {
		a.mu.Unlock()
		return errors.New("the release is already being downloaded")
	}
	if offer == nil || release == nil {
		a.mu.Unlock()
		return errors.New("there is no newer release to download; check for updates first")
	}
	if offer.Package != "" {
		a.mu.Unlock()
		return errors.New("this copy is installed by a package; install the new release's own file instead")
	}
	a.download = Download{State: "running", Tag: offer.To}
	a.downloaded.Store(0)
	a.mu.Unlock()
	go func() {
		err := a.runDownload(ctx, *offer, release)
		// The record first and the log line after, outside the lock: log takes it too.
		a.mu.Lock()
		if err != nil {
			a.download = Download{State: "failed", Tag: offer.To, Error: err.Error()}
		} else {
			a.download = Download{State: "done", Tag: offer.To, Done: a.downloaded.Load(), Total: a.download.Total}
		}
		a.mu.Unlock()
		if err != nil {
			a.log("the download of %s failed: %v", offer.To, err)
			return
		}
		a.log("%s is downloaded and checked, ready to install", offer.To)
	}()
	return nil
}

func (a *App) runDownload(ctx context.Context, offer Offer, release *Release) error {
	asset, ok := release.asset(offer.Asset)
	if !ok {
		return fmt.Errorf("%s has no %s", offer.To, offer.Asset)
	}
	sumsAsset, ok := release.asset("SHA256SUMS")
	if !ok {
		return fmt.Errorf("%s has no SHA256SUMS", offer.To)
	}
	files := stagedFiles(a.paths, offer.To, offer.Asset)
	// Releases other than this one are of no further use; one download at a time is kept.
	if entries, err := os.ReadDir(filepath.Dir(stagedDir(a.paths, offer.To))); err == nil {
		for _, entry := range entries {
			if entry.Name() != offer.To {
				_ = os.RemoveAll(filepath.Join(filepath.Dir(stagedDir(a.paths, offer.To)), entry.Name()))
			}
		}
	}
	_ = os.Remove(files.verified)
	if err := os.MkdirAll(stagedDir(a.paths, offer.To), 0o700); err != nil {
		return err
	}
	a.mu.Lock()
	a.download.Total = asset.Size
	a.mu.Unlock()
	if err := downloadTo(ctx, asset.URL, files.archive, assetLimit, &a.downloaded); err != nil {
		return err
	}
	sums, err := getLimited(ctx, sumsAsset.URL, 1<<20)
	if err != nil {
		return err
	}
	var sig []byte
	if sigAsset, ok := release.asset(signatureAsset); ok {
		if sig, err = getLimited(ctx, sigAsset.URL, 4096); err != nil {
			return err
		}
	}
	// Checked now, so a broken or forged download is said while the app is open and nothing has
	// closed; checked again by the upgrade before a file of it is used.
	if _, err := verifyRelease(releaseMessage(offer.To, sums), sig, trustedReleaseKeys); err != nil {
		return fmt.Errorf("the release was refused: %w", err)
	}
	expected := checksumFor(string(sums), offer.Asset)
	if expected == "" {
		return fmt.Errorf("SHA256SUMS of %s does not mention %s", offer.To, offer.Asset)
	}
	got, _, err := sha256File(files.archive)
	if err != nil {
		return err
	}
	if got != expected {
		_ = os.Remove(files.archive)
		return errors.New("the download does not match its checksum; download it again")
	}
	if err := os.WriteFile(files.sums, sums, 0o600); err != nil {
		return err
	}
	if sig != nil {
		if err := os.WriteFile(files.sig, sig, 0o600); err != nil {
			return err
		}
	} else {
		_ = os.Remove(files.sig)
	}
	return os.WriteFile(files.verified, []byte(expected+"\n"), 0o600)
}

// downloadClient is releaseClient without its overall timeout: a large archive on a slow link takes
// longer than any fixed limit, and the stall watchdog in downloadTo is what ends a dead one.
var downloadClient = &http.Client{CheckRedirect: releaseClient.CheckRedirect}

// downloadTo streams raw into name, counting bytes into done, through a temporary file renamed into
// place only when it is complete.
func downloadTo(ctx context.Context, raw, name string, limit int64, done *atomic.Int64) error {
	if err := allowedURL(raw); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return err
	}
	request.Header.Set("User-Agent", "daedalus-desktop/"+version)
	response, err := downloadClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: HTTP %d", raw, response.StatusCode)
	}
	partial := name + ".part"
	file, err := os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer os.Remove(partial)
	var stalled atomic.Bool
	moved := make(chan struct{}, 1)
	go func() {
		timer := time.NewTimer(downloadStall)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-moved:
				if !timer.Stop() {
					<-timer.C
				}
				timer.Reset(downloadStall)
			case <-timer.C:
				stalled.Store(true)
				cancel()
				return
			}
		}
	}()
	buffer := make([]byte, 256<<10)
	var total int64
	for {
		n, readErr := response.Body.Read(buffer)
		if n > 0 {
			total += int64(n)
			if total > limit {
				file.Close()
				return fmt.Errorf("%s: larger than %d bytes", raw, limit)
			}
			if _, err := file.Write(buffer[:n]); err != nil {
				file.Close()
				return err
			}
			done.Store(total)
			select {
			case moved <- struct{}{}:
			default:
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			file.Close()
			if stalled.Load() {
				return fmt.Errorf("the download stopped moving for %s; check the connection and try again", downloadStall)
			}
			return readErr
		}
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(partial, name)
}

// stagedSource is the release the app downloaded beforehand, for the upgrade to install without
// going to the network for it. Nil when there is none for this offer, or its files are not all there:
// the upgrade then downloads as it always did.
func stagedSource(p Paths, offer *Offer) *upgradeSource {
	files := stagedFiles(p, offer.To, offer.Asset)
	if !files.ready() {
		return nil
	}
	want, err := os.ReadFile(files.verified)
	if err != nil {
		return nil
	}
	return &upgradeSource{from: offer.From, to: offer.To, notes: offer.URL, asset: offer.Asset,
		fetch: func(context.Context) ([]byte, []byte, error) {
			body, err := readLimited(files.archive, assetLimit)
			if err != nil {
				return nil, nil, err
			}
			sum := sha256.Sum256(body)
			if hex.EncodeToString(sum[:]) != strings.TrimSpace(string(want)) {
				return nil, nil, errors.New("the downloaded release changed on disk since it was checked; download it again")
			}
			list, err := readLimited(files.sums, 1<<20)
			if err != nil {
				return nil, nil, err
			}
			return body, list, nil
		},
		signature: func(context.Context) ([]byte, error) {
			sig, err := readLimited(files.sig, 4096)
			if os.IsNotExist(err) {
				return nil, nil
			}
			return sig, err
		}}
}
