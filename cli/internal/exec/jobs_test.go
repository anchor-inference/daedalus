//go:build unix

package exec

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Another daedalus-cli of the same machine answers for a job it did not start, from the record the
// first one keeps: with two terminals open the server's question can reach either.
func TestAnotherProcessAnswersForAJobFromItsRecord(t *testing.T) {
	first, root := newExecutor(t)
	log := filepath.Join(root, ".jobs", "job-1-7.log")
	res, err := call(t, first, "exec.run", map[string]any{"argv": []string{"bash", "-c", "echo up; sleep 30"},
		"job": map[string]any{"id": "job-1-7", "log": log, "after_ms": 0}})
	if err != nil || res["handed_over"] != true {
		t.Fatalf("%v %v", res, err)
	}
	second, serr := New(nil)
	if serr != nil {
		t.Fatal(serr)
	}
	if err := second.Bind("s1", root); err != nil {
		t.Fatal(err)
	}
	status, rerr := call(t, second, "job.status", map[string]any{"job": "job-1-7"})
	if rerr != nil || status["state"] != "running" {
		t.Fatalf("the other process: %v %v", status, rerr)
	}
	killed, rerr := call(t, second, "job.kill", map[string]any{"job": "job-1-7"})
	if rerr != nil {
		t.Fatal(rerr)
	}
	_ = killed
	deadline := time.Now().Add(10 * time.Second)
	for {
		status, rerr = call(t, first, "job.status", map[string]any{"job": "job-1-7"})
		if rerr != nil {
			t.Fatal(rerr)
		}
		if status["state"] == "exited" || time.Now().After(deadline) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if status["state"] != "exited" {
		t.Fatalf("the job outlived a kill from the other process: %v", status)
	}
	// Another session's job is no job at all.
	if err := first.Bind("s2", root); err != nil {
		t.Fatal(err)
	}
	if _, rerr := callAs(t, first, "s2", "job.status", map[string]any{"job": "job-1-7"}); rerr == nil || rerr.Code != CodeNotFound {
		t.Fatalf("another session read the job: %v", rerr)
	}
}

// A job recorded by a client that has gone, with no end recorded, went with that client: lost.
func TestAJobOfAClientThatIsGoneIsLost(t *testing.T) {
	e, root := newExecutor(t)
	e.jobs.save(jobRecord{ID: "job-3-3", Session: "s1", Log: filepath.Join(root, ".jobs", "job-3-3.log"), Pid: 1 << 22, Started: 1})
	status, err := call(t, e, "job.status", map[string]any{"job": "job-3-3"})
	if err != nil || status["state"] != "lost" {
		t.Fatalf("%v %v", status, err)
	}
}

func TestAJobLogOutsideTheFolderIsRefusedAndAQuickCommandLeavesNoJob(t *testing.T) {
	e, root := newExecutor(t)
	_, err := call(t, e, "exec.run", map[string]any{"argv": []string{"bash", "-c", "true"},
		"job": map[string]any{"id": "job-4-4", "log": "/tmp/elsewhere.log", "after_ms": 1000}})
	if err == nil || reasonOf(err) != ReasonOutsideRoot {
		t.Fatalf("%v", err)
	}
	log := filepath.Join(root, ".jobs", "job-5-5.log")
	res, err := call(t, e, "exec.run", map[string]any{"argv": []string{"bash", "-c", "echo quick"},
		"job": map[string]any{"id": "job-5-5", "log": log, "after_ms": 5000}})
	if err != nil || res["handed_over"] == true || res["stdout"] != "quick\n" {
		t.Fatalf("%v %v", res, err)
	}
	if _, serr := os.Stat(log); !os.IsNotExist(serr) {
		t.Fatal("a command that ended in time left its log behind")
	}
	if _, err := call(t, e, "job.status", map[string]any{"job": "job-5-5"}); err == nil || err.Code != CodeNotFound {
		t.Fatalf("a command that ended in time is a job: %v", err)
	}
}
