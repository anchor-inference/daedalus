//go:build windows

package memcap

import (
	"errors"
	"fmt"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Manager on Windows is one job object every program the daemon starts is put in first, before the
// job of its own that ends its tree; that job then nests inside this one, and the job-wide memory
// limit holds them all. Windows enforces it on committed memory: an allocation past the cap fails
// in the program that asked, rather than the largest program being killed, and there is no count of
// programs ended for it.
type Manager struct {
	mu        sync.Mutex
	job       windows.Handle
	reason    string
	note      string
	limit     int64
	requested *Request
	stateFile string
}

// Setup creates the shared job and applies the cap saved by the last run, if any.
func Setup(stateFile string) *Manager {
	m := &Manager{stateFile: stateFile}
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		m.reason = fmt.Sprintf("the shared job object could not be created: %v", err)
		return m
	}
	m.job = job
	if saved := loadRequest(stateFile); saved != nil {
		_, _ = m.Apply(*saved)
	}
	return m
}

// Assign puts a just-created program in the shared job. Best effort: a program the system will not
// nest (an older Windows, a job of the launcher's that forbids it) runs uncapped, as it ran before.
func (m *Manager) Assign(process windows.Handle) {
	if m == nil || m.job == 0 {
		return
	}
	if err := windows.AssignProcessToJobObject(m.job, process); err != nil {
		m.mu.Lock()
		m.note = fmt.Sprintf("a program could not be put under the cap: %v", err)
		m.mu.Unlock()
	}
}

// Assign is Manager.Assign for the installed cap.
func Assign(process windows.Handle) { Installed().Assign(process) }

// AssignPid is Assign for a process known by its id, as os/exec reports one.
func AssignPid(pid int) {
	m := Installed()
	if m == nil || m.job == 0 {
		return
	}
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return
	}
	defer windows.CloseHandle(h)
	m.Assign(h)
}

func (m *Manager) Apply(r Request) (Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.job == 0 {
		return m.status(), errors.New(m.reason)
	}
	limits, note, err := Resolve(r, machineBytes())
	if err != nil {
		return m.status(), err
	}
	m.requested, m.note = &r, note
	_ = saveRequest(m.stateFile, r)
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	if limits.Max > 0 {
		info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_JOB_MEMORY
		info.JobMemoryLimit = uintptr(limits.Max)
	}
	if _, err := windows.SetInformationJobObject(m.job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		return m.status(), fmt.Errorf("setting the job's memory limit: %w", err)
	}
	m.limit = limits.Max
	return m.status(), nil
}

func (m *Manager) Status() Status {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.status()
}

func (m *Manager) status() Status {
	st := Status{Supported: m.job != 0, Requested: m.requested, MachineBytes: machineBytes(), Reason: m.reason,
		CurrentBytes: -1, OOMKills: -1}
	if m.note != "" {
		st.Reason = m.note
	}
	if m.job == 0 {
		return st
	}
	st.Mode = "job_object"
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	if err := windows.QueryInformationJobObject(m.job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info)), nil); err == nil {
		if info.BasicLimitInformation.LimitFlags&windows.JOB_OBJECT_LIMIT_JOB_MEMORY != 0 {
			limit := int64(info.JobMemoryLimit)
			st.MaxBytes = &limit
		}
	}
	return st
}

func (m *Manager) Kills() int64    { return -1 }
func (m *Manager) JobsDir() string { return "" }

type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

var globalMemoryStatusEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")

func machineBytes() int64 {
	var status memoryStatusEx
	status.Length = uint32(unsafe.Sizeof(status))
	if ok, _, _ := globalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&status))); ok == 0 {
		return 0
	}
	return int64(status.TotalPhys)
}
