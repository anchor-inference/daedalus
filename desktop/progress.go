package main

import (
	"io"
	"time"
)

// Activity is the one piece of work inside a stage that the progress page shows in detail: which
// archive is being downloaded or unpacked, how far through it is, and — for the environment — what
// uv is doing. A stage says where a start has got to; an activity says that it is still moving.
//
// A first run on a slow line once spent four minutes on one download with nothing on the page but
// an indeterminate bar, and was taken for hung. Every long step reports itself through this now,
// with its size where the size is known, and the page adds a heartbeat on top of it for the moments
// nothing reports at all.
type Activity struct {
	// Kind is what is happening: download (from the network), bundled (read out of the installation
	// itself), unpack, python (uv installing the interpreter), packages (uv building the environment)
	// or wheels (the installation's own package cache being laid out).
	Kind string `json:"kind"`
	// Name is what it is happening to, in the launcher's own words: "uv 0.12.15", "daedalus".
	Name string `json:"name"`
	// Done and Total are how far through it is, in Unit. Total is zero when nobody knows it — a
	// GitHub tarball is streamed without a length — and the page then shows the count without a bar.
	Done  int64  `json:"done"`
	Total int64  `json:"total"`
	Unit  string `json:"unit"`
}

// Units an activity is counted in.
const (
	unitBytes    = "bytes"
	unitPackages = "packages"
)

// reporter is how a piece of work tells the page where it is. A nil reporter is a command line with
// no page to draw, and every call site goes through report so that nil is always safe.
type reporter func(Activity)

func (r reporter) report(activity Activity) {
	if r != nil {
		r(activity)
	}
}

// progressEvery is how often a long copy reports. The page polls once a second; reporting on every
// 32 KB read would take the lock a few thousand times a second for nothing.
const progressEvery = 150 * time.Millisecond

// countingReader reports how much has been read through it, at most every progressEvery and once
// more at the end, so the last figure the page sees is the whole.
type countingReader struct {
	reader   io.Reader
	activity Activity
	report   reporter
	last     time.Time
}

func newCountingReader(reader io.Reader, activity Activity, report reporter) *countingReader {
	activity.Unit = unitBytes
	report.report(activity)
	return &countingReader{reader: reader, activity: activity, report: report, last: time.Now()}
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.reader.Read(p)
	c.activity.Done += int64(n)
	if err == io.EOF || time.Since(c.last) >= progressEvery {
		c.last = time.Now()
		c.report.report(c.activity)
	}
	return n, err
}
