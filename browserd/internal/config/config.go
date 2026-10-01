// Package config reads the daemon's flags and its optional JSON file.
package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// The defaults, one name each, as the contract's Limits table lists them.
const (
	DefaultMaxBrowsers         = 2
	DefaultMaxGroupsPerBrowser = 8
	DefaultMaxTabsPerGroup     = 8
	DefaultMaxViewersPerTab    = 8
	DefaultIdleClose           = 10 * time.Minute
	DefaultMemoryHardBytes     = 2 << 30
	DefaultMaxDownloadBytes    = 500 << 20
	DefaultMaxProfileDownloads = 2 << 30
	DefaultMaxUploadBytes      = 100 << 20
	DefaultFPSCap              = 15
	DefaultRecordMaxBytes      = 500 << 20
	DefaultRecordRetention     = 7 * 24 * time.Hour

	// The viewport a group gets unless the host asks for another, and the bounds of one it asks for.
	DefaultViewportW = 1280
	DefaultViewportH = 800
	MinViewport      = 320
	MaxViewport      = 3840

	// EventRingSize and EventRingBytes bound the event log, as ptyd's.
	EventRingSize  = 20000
	EventRingBytes = 64 << 20

	// StatsInterval is how often browser.stats is published while a browser runs.
	StatsInterval = 10 * time.Second
	// CloseGrace is how long a browser asked to close has before its processes are killed.
	CloseGrace = 3 * time.Second
	// HumanControlTTL is how long human control lasts without input, unless the host says.
	HumanControlTTL = 30 * time.Minute
	// ControlWait is how long an agent's call waits for a human to give control back.
	ControlWait    = 20 * time.Second
	MaxControlWait = time.Minute
	// ChromiumStart is how long a started browser has to give its first answer on the pipe. A warm
	// start answers in a fifth of a second, but the first start on a machine pages the 250 MB binary
	// in from disk, and on slow storage that is the whole cost: throttled to 100 reads a second, one
	// cold start took 18 s and two at once 36 s, while warm ones under the same throttle took 0.15 s.
	// A fresh CI runner has such a disk: there the one test that starts a single browser took 4 to
	// 12 s where it takes 1.5 s on a warm machine, and once two first starts at the same moment were
	// both still silent at 20 s, the fixed budget this replaces. It stays under the 45 s the host
	// gives browser.open, so the daemon's account of a browser that is really stuck arrives first.
	ChromiumStart = 40 * time.Second
)

// Config is everything `browserd serve` runs with.
type Config struct {
	Env        string
	RunDir     string
	StateDir   string
	Listen     string
	ConfigFile string
	LogFile    string
	LogLevel   string

	Limits   Limits
	Chromium Chromium
}

// Limits are the numbers a deployment may tune from the JSON file.
type Limits struct {
	MaxBrowsers         int           `json:"max_browsers"`
	MaxGroupsPerBrowser int           `json:"max_groups_per_browser"`
	MaxTabsPerGroup     int           `json:"max_tabs_per_group"`
	MaxViewersPerTab    int           `json:"max_viewers_per_tab"`
	IdleClose           time.Duration `json:"-"`
	IdleCloseMs         int64         `json:"idle_close_ms"`
	MemoryHardBytes     int64         `json:"memory_hard_bytes"`
	MaxDownloadBytes    int64         `json:"max_download_bytes"`
	MaxProfileDownloads int64         `json:"max_profile_download_bytes"`
	MaxUploadBytes      int64         `json:"max_upload_bytes"`
	FPSCap              int           `json:"fps_cap"`
	// Recorded keyframes: all of them together at most this many bytes, none older than this.
	RecordMaxBytes    int64 `json:"record_max_bytes"`
	RecordRetentionMs int64 `json:"record_retention_ms"`
}

// Chromium is which browser to run and how. Path empty means: find one (chrome.Find).
type Chromium struct {
	Path string
	Args []string
	// NoSandbox passes --no-sandbox. Only the operator's file can say so, and daemon.info reports it.
	NoSandbox bool
	// StartTimeout replaces ChromiumStart when it is not zero; only a test sets it.
	StartTimeout time.Duration
}

// DefaultLimits are the contract's defaults.
func DefaultLimits() Limits {
	return Limits{
		MaxBrowsers:         DefaultMaxBrowsers,
		MaxGroupsPerBrowser: DefaultMaxGroupsPerBrowser,
		MaxTabsPerGroup:     DefaultMaxTabsPerGroup,
		MaxViewersPerTab:    DefaultMaxViewersPerTab,
		IdleClose:           DefaultIdleClose,
		IdleCloseMs:         DefaultIdleClose.Milliseconds(),
		MemoryHardBytes:     DefaultMemoryHardBytes,
		MaxDownloadBytes:    DefaultMaxDownloadBytes,
		MaxProfileDownloads: DefaultMaxProfileDownloads,
		MaxUploadBytes:      DefaultMaxUploadBytes,
		FPSCap:              DefaultFPSCap,
		RecordMaxBytes:      DefaultRecordMaxBytes,
		RecordRetentionMs:   DefaultRecordRetention.Milliseconds(),
	}
}

// file is the JSON file's shape. Unknown keys are refused: a misspelt limit that silently keeps its
// default is how a deployment ends up not doing what its operator wrote.
type file struct {
	Limits struct {
		MaxBrowsers         *int   `json:"max_browsers"`
		MaxGroupsPerBrowser *int   `json:"max_groups_per_browser"`
		MaxTabsPerGroup     *int   `json:"max_tabs_per_group"`
		MaxViewersPerTab    *int   `json:"max_viewers_per_tab"`
		IdleCloseMs         *int64 `json:"idle_close_ms"`
		MemoryHardBytes     *int64 `json:"memory_hard_bytes"`
		MaxDownloadBytes    *int64 `json:"max_download_bytes"`
		MaxProfileDownloads *int64 `json:"max_profile_download_bytes"`
		MaxUploadBytes      *int64 `json:"max_upload_bytes"`
		FPSCap              *int   `json:"fps_cap"`
		RecordMaxBytes      *int64 `json:"record_max_bytes"`
		RecordRetentionMs   *int64 `json:"record_retention_ms"`
	} `json:"limits"`
	Chromium struct {
		Path      string   `json:"path"`
		Args      []string `json:"args"`
		NoSandbox bool     `json:"no_sandbox"`
	} `json:"chromium"`
}

// Parse reads the arguments of `serve`.
func Parse(args []string) (*Config, error) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	c := &Config{}
	var chromium string
	fs.StringVar(&c.Env, "env", "", "environment name (container or host)")
	fs.StringVar(&c.RunDir, "run-dir", "", "run directory: endpoint, token and socket")
	fs.StringVar(&c.StateDir, "state-dir", "", "state directory: profiles, downloads, uploads")
	fs.StringVar(&c.Listen, "listen", DefaultListen(runtime.GOOS), `"unix" or "tcp:127.0.0.1:<port>"`)
	fs.StringVar(&chromium, "chromium", "", "the Chromium to run")
	fs.StringVar(&c.ConfigFile, "config", "", "JSON file with limits and the Chromium")
	fs.StringVar(&c.LogFile, "log-file", "", "log file (default stderr)")
	fs.StringVar(&c.LogLevel, "log-level", "info", "debug, info, warn or error")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if c.Env == "" {
		return nil, errors.New("--env is required")
	}
	if c.RunDir == "" || c.StateDir == "" {
		return nil, errors.New("--run-dir and --state-dir are required")
	}
	var err error
	if c.RunDir, err = filepath.Abs(c.RunDir); err != nil {
		return nil, err
	}
	if c.StateDir, err = filepath.Abs(c.StateDir); err != nil {
		return nil, err
	}
	if c.Listen != "unix" && !strings.HasPrefix(c.Listen, "tcp:127.0.0.1:") {
		// Only loopback: the token is the whole of the authentication.
		return nil, fmt.Errorf("--listen must be unix or tcp:127.0.0.1:<port>, not %q", c.Listen)
	}
	c.Limits = DefaultLimits()
	if c.ConfigFile != "" {
		if err := c.load(c.ConfigFile); err != nil {
			return nil, fmt.Errorf("config %s: %w", c.ConfigFile, err)
		}
	}
	// The flag wins over the file: it is what a person typed last.
	if chromium != "" {
		c.Chromium.Path = chromium
	}
	return c, nil
}

func (c *Config) load(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var f file
	if err := dec.Decode(&f); err != nil {
		return err
	}
	intIn := func(name string, v *int, lo, hi int, dst *int) error {
		if v == nil {
			return nil
		}
		if *v < lo || *v > hi {
			return fmt.Errorf("limits.%s %d is outside %d..%d", name, *v, lo, hi)
		}
		*dst = *v
		return nil
	}
	int64In := func(name string, v *int64, lo, hi int64, dst *int64) error {
		if v == nil {
			return nil
		}
		if *v < lo || *v > hi {
			return fmt.Errorf("limits.%s %d is outside %d..%d", name, *v, lo, hi)
		}
		*dst = *v
		return nil
	}
	l := &c.Limits
	for _, err := range []error{
		intIn("max_browsers", f.Limits.MaxBrowsers, 1, 64, &l.MaxBrowsers),
		intIn("max_groups_per_browser", f.Limits.MaxGroupsPerBrowser, 1, 256, &l.MaxGroupsPerBrowser),
		intIn("max_tabs_per_group", f.Limits.MaxTabsPerGroup, 1, 256, &l.MaxTabsPerGroup),
		intIn("max_viewers_per_tab", f.Limits.MaxViewersPerTab, 1, 64, &l.MaxViewersPerTab),
		int64In("idle_close_ms", f.Limits.IdleCloseMs, 0, 7*24*3600*1000, &l.IdleCloseMs),
		int64In("memory_hard_bytes", f.Limits.MemoryHardBytes, 64<<20, 1<<40, &l.MemoryHardBytes),
		int64In("max_download_bytes", f.Limits.MaxDownloadBytes, 1, 1<<40, &l.MaxDownloadBytes),
		int64In("max_profile_download_bytes", f.Limits.MaxProfileDownloads, 1, 1<<42, &l.MaxProfileDownloads),
		int64In("max_upload_bytes", f.Limits.MaxUploadBytes, 1, 1<<34, &l.MaxUploadBytes),
		intIn("fps_cap", f.Limits.FPSCap, 1, 60, &l.FPSCap),
		int64In("record_max_bytes", f.Limits.RecordMaxBytes, 1<<20, 1<<40, &l.RecordMaxBytes),
		int64In("record_retention_ms", f.Limits.RecordRetentionMs, 3600*1000, 366*24*3600*1000, &l.RecordRetentionMs),
	} {
		if err != nil {
			return err
		}
	}
	l.IdleClose = time.Duration(l.IdleCloseMs) * time.Millisecond
	if f.Chromium.Path != "" && !filepath.IsAbs(f.Chromium.Path) {
		return fmt.Errorf("chromium.path %q is not an absolute path", f.Chromium.Path)
	}
	c.Chromium = Chromium{Path: f.Chromium.Path, Args: f.Chromium.Args, NoSandbox: f.Chromium.NoSandbox}
	return nil
}

// DefaultListen is where a daemon listens unless told otherwise: its run directory's socket, and on
// Windows a loopback TCP port, where the host's asyncio has no unix-socket client.
func DefaultListen(goos string) string {
	if goos == "windows" {
		return "tcp:127.0.0.1:0"
	}
	return "unix"
}
