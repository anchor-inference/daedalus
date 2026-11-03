package exec

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/sidechan"
)

// Jobs are commands this machine keeps running past the call that started them: an exec.run that
// names a job and was still running at its hand-over time, or one handed over at once (a background
// job). The server knows each by the id it chose and asks after it with job.status, job.tail and
// job.kill, between turns as well as in them, until it has heard how it ended.
//
// A job is the process's own: it is tied to this client like every command (package tether), so a
// client that exits takes its jobs with it. A record of each is kept on disk as well, and that is
// what lets another daedalus-cli of the same machine answer for a job it did not start: the server
// sends a session's calls to whichever connection of the device carries it, which with two
// terminals open need not be the process that started the job. Without the record it would answer
// "no such job", and the server would report a job that is still running as lost.

// jobKeep is how long a finished job's record is kept, in memory and on disk: long enough for a server
// that was down to come back and hear how it ended.
const jobKeep = 7 * 24 * time.Hour

// maxJobTail bounds what job.tail reads from the end of a log.
const maxJobTail = 64 << 10

type job struct {
	record jobRecord
	run    *sidechan.Job
	file   *os.File
}

// jobRecord is a job as the disk keeps it. Exit fields are set once it has ended.
type jobRecord struct {
	ID       string  `json:"id"`
	Session  string  `json:"session"`
	Log      string  `json:"log"`
	Pid      int     `json:"pid"`
	Started  float64 `json:"started"`
	Ended    float64 `json:"ended,omitempty"`
	ExitCode *int    `json:"exit_code,omitempty"`
	Signal   string  `json:"signal,omitempty"`
	Stopped  bool    `json:"stopped,omitempty"`
}

type jobTable struct {
	mu   sync.Mutex
	dir  string
	jobs map[string]*job
}

func newJobTable(dir string) *jobTable {
	t := &jobTable{dir: dir, jobs: map[string]*job{}}
	t.prune()
	return t
}

// defaultJobDir is where this machine's job records are kept, one file per job.
func defaultJobDir() string {
	base, err := os.UserCacheDir()
	if err != nil || base == "" {
		base = os.TempDir()
	}
	return filepath.Join(base, "daedalus-cli", "jobs")
}

// prune drops the records of jobs that ended, or were started, longer ago than jobKeep.
func (t *jobTable) prune() {
	entries, err := os.ReadDir(t.dir)
	if err != nil {
		return
	}
	cutoff := time.Now().Add(-jobKeep)
	for _, e := range entries {
		if info, err := e.Info(); err == nil && info.ModTime().Before(cutoff) {
			_ = os.Remove(filepath.Join(t.dir, e.Name()))
		}
	}
}

func (t *jobTable) path(id string) string { return filepath.Join(t.dir, id+".json") }

func (t *jobTable) save(r jobRecord) {
	if err := os.MkdirAll(t.dir, 0o700); err != nil {
		return
	}
	raw, err := json.Marshal(r)
	if err != nil {
		return
	}
	tmp := t.path(r.ID) + ".tmp"
	if os.WriteFile(tmp, raw, 0o600) == nil {
		_ = os.Rename(tmp, t.path(r.ID))
	}
}

func (t *jobTable) load(id string) (jobRecord, bool) {
	raw, err := os.ReadFile(t.path(id))
	if err != nil {
		return jobRecord{}, false
	}
	var r jobRecord
	if json.Unmarshal(raw, &r) != nil || r.ID != id {
		return jobRecord{}, false
	}
	return r, true
}

// validJobID refuses an id that could name a file outside the record folder.
func validJobID(id string) bool {
	if id == "" || len(id) > 128 {
		return false
	}
	return !strings.ContainsAny(id, `/\:.`+"\x00")
}

// jobParams is exec.run's optional job.
type jobParams struct {
	ID      string `json:"id"`
	Log     string `json:"log"`
	AfterMs int64  `json:"after_ms"`
}

// openJobLog makes the log a job writes into: inside the session's folder, checked as every path is,
// before and after its folder is made.
func (e *Executor) openJobLog(root string, p *jobParams) (*os.File, error) {
	if !validJobID(p.ID) {
		return nil, &Error{Code: CodeInvalidParams, Message: "a job id is letters, digits and dashes: " + p.ID}
	}
	if p.AfterMs < 0 {
		return nil, &Error{Code: CodeInvalidParams, Message: "after_ms must not be negative"}
	}
	if err := e.jail(root, p.Log); err != nil {
		return nil, err
	}
	dir := filepath.Dir(filepath.Clean(p.Log))
	if err := os.MkdirAll(dir, 0o777); err != nil {
		return nil, osError(err)
	}
	if err := e.jail(root, dir); err != nil {
		return nil, err
	}
	e.jobs.mu.Lock()
	_, taken := e.jobs.jobs[p.ID]
	e.jobs.mu.Unlock()
	if taken {
		return nil, &Error{Code: CodeInvalidParams, Message: "this terminal already runs a job " + p.ID}
	}
	f, err := os.OpenFile(filepath.Clean(p.Log), os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o666)
	if err != nil {
		return nil, osError(err)
	}
	return f, nil
}

// runJob is exec.run with a job: the command runs until it ends or until after_ms, and is then kept.
func (e *Executor) runJob(ctx context.Context, session, root string, p *jobParams, req sidechan.ExecRequest) (any, error) {
	f, err := e.openJobLog(root, p)
	if err != nil {
		return nil, err
	}
	log := &syncedFile{f: f}
	res, run, err := e.exec.RunOrHandOver(ctx, req, sidechan.HandOver{After: time.Duration(p.AfterMs) * time.Millisecond, Log: log})
	if err != nil || run == nil {
		// Ended in time, or never started: the answer carries the output and no job is left behind.
		_ = f.Close()
		_ = os.Remove(f.Name())
		if err != nil {
			return nil, sideError(err)
		}
		return res, nil
	}
	j := &job{record: jobRecord{ID: p.ID, Session: session, Log: f.Name(), Pid: run.Pid, Started: unix(run.Started)}, run: run, file: f}
	e.jobs.mu.Lock()
	e.jobs.jobs[p.ID] = j
	e.jobs.mu.Unlock()
	e.jobs.save(j.record)
	go func() {
		end := run.Result()
		_ = f.Close()
		e.jobs.mu.Lock()
		code := end.ExitCode
		j.record.ExitCode, j.record.Signal, j.record.Ended = &code, end.Signal, unix(time.Now())
		record := j.record
		e.jobs.mu.Unlock()
		e.jobs.save(record)
	}()
	return res, nil
}

func unix(t time.Time) float64 { return float64(t.UnixMilli()) / 1000 }

// syncedFile is a job's log: its writes come from the command's two output streams.
type syncedFile struct {
	mu sync.Mutex
	f  *os.File
}

func (s *syncedFile) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.f.Write(p)
}

// jobRequest is the params of job.status, job.tail and job.kill.
type jobRequest struct {
	Job   string `json:"job"`
	Lines int    `json:"lines"`
}

// findJob is a job of this session: this process's own, or one another process of this machine
// recorded. A job of another session is as unknown as one that never was.
func (e *Executor) findJob(session, id string) (*job, jobRecord, error) {
	if !validJobID(id) {
		return nil, jobRecord{}, &Error{Code: CodeInvalidParams, Message: "no job id"}
	}
	e.jobs.mu.Lock()
	j := e.jobs.jobs[id]
	var r jobRecord
	if j != nil {
		r = j.record
	}
	e.jobs.mu.Unlock()
	if j == nil {
		var ok bool
		if r, ok = e.jobs.load(id); !ok {
			return nil, jobRecord{}, &Error{Code: CodeNotFound, Message: "this terminal holds no job " + id}
		}
	}
	if r.Session != session {
		return nil, jobRecord{}, &Error{Code: CodeNotFound, Message: "this terminal holds no job " + id}
	}
	return j, r, nil
}

// jobStatus is job.status: running, exited (with its code) or lost (it was another client's, that
// client is gone, and it never recorded an end: the job went with it).
func (e *Executor) jobStatus(session string, raw json.RawMessage) (any, error) {
	var p jobRequest
	if err := decode(raw, &p); err != nil {
		return nil, err
	}
	j, r, err := e.findJob(session, p.Job)
	if err != nil {
		return nil, err
	}
	return describeJob(j, r), nil
}

func describeJob(j *job, r jobRecord) map[string]any {
	out := map[string]any{"job": r.ID, "pid": r.Pid, "started_at": r.Started, "size": fileSize(r.Log)}
	switch {
	case r.ExitCode != nil:
		out["state"], out["exit_code"], out["signal"], out["ended_at"] = "exited", *r.ExitCode, r.Signal, r.Ended
	case j != nil:
		out["state"] = "running"
	case processAlive(r.Pid):
		out["state"] = "running"
	default:
		out["state"] = "lost"
	}
	return out
}

func fileSize(path string) int64 {
	if st, err := os.Stat(path); err == nil {
		return st.Size()
	}
	return -1
}

// jobTail is job.tail: the last lines of the job's log, read from its last 64 KiB.
func (e *Executor) jobTail(session, root string, raw json.RawMessage) (any, error) {
	var p jobRequest
	if err := decode(raw, &p); err != nil {
		return nil, err
	}
	_, r, err := e.findJob(session, p.Job)
	if err != nil {
		return nil, err
	}
	if err := e.jail(root, r.Log); err != nil {
		return nil, err
	}
	lines := p.Lines
	if lines <= 0 {
		lines = 100
	}
	text, err := tailLines(r.Log, lines)
	if err != nil {
		return nil, osError(err)
	}
	return map[string]any{"text": text}, nil
}

func tailLines(path string, lines int) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return "", err
	}
	start := max(int64(0), st.Size()-maxJobTail)
	if _, err := f.Seek(start, io.SeekStart); err != nil {
		return "", err
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return "", err
	}
	all := strings.Split(strings.TrimRight(strings.ToValidUTF8(string(data), "�"), "\n"), "\n")
	if start > 0 && len(all) > 1 {
		all = all[1:] // the first line was cut by the 64 KiB window
	}
	if len(all) > lines {
		all = all[len(all)-lines:]
	}
	return strings.Join(all, "\n"), nil
}

// jobKill is job.kill: the job's group ended, the hangup and the kill after a grace, and its status.
func (e *Executor) jobKill(session string, raw json.RawMessage) (any, error) {
	var p jobRequest
	if err := decode(raw, &p); err != nil {
		return nil, err
	}
	j, r, err := e.findJob(session, p.Job)
	if err != nil {
		return nil, err
	}
	switch {
	case j != nil:
		j.run.Stop()
		<-j.run.Done()
		// The goroutine of runJob records the end; wait for it so the answer says how it ended.
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			e.jobs.mu.Lock()
			r = j.record
			e.jobs.mu.Unlock()
			if r.ExitCode != nil {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
	case r.ExitCode == nil && processAlive(r.Pid):
		if err := killGroup(r.Pid); err != nil {
			return nil, &Error{Code: CodeInternal, Message: fmt.Sprintf("could not end job %s: %v", r.ID, err)}
		}
		// Another process's job: that process records its end, if it is still there to see it.
		if again, ok := e.jobs.load(r.ID); ok {
			r = again
		}
	}
	return describeJob(j, r), nil
}

// JobMethods are the job calls this executor serves; device.hello advertises them as capabilities.jobs.
var JobMethods = []string{"job.status", "job.tail", "job.kill"}
