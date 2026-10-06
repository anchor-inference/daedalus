//go:build windows

package ptyproc

import (
	"errors"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// A Windows terminal is a pseudoconsole (ConPTY): a console host the system runs for the program,
// fed by one pipe and read from another. It renders the program's console into VT sequences on its
// way out, which is what the scanner and the emulator then read, exactly as they read a Unix PTY.
//
// What Windows has no counterpart for is said here rather than faked: there is no foreground process
// group to ask about, and of the signals only an interrupt (a Ctrl+C typed into the console) and an
// ending (the console closed, then the job terminated) exist.

// statusControlCExit is the exit status Windows gives a console program that a close or a Ctrl+C
// ended without a handler of its own: STATUS_CONTROL_C_EXIT.
const statusControlCExit = 0xC000013A

// killedExitCode is what the job is terminated with. The code itself is never reported: an ending
// the daemon caused is reported by the signal it stood for.
const killedExitCode = 1

// Console is the daemon's side of a pseudoconsole: the pipe the program's input is written to, the
// pipe its rendered output is read from, and the console itself. It reads and writes like the
// master of a Unix PTY.
type Console struct {
	hpc windows.Handle
	in  *os.File
	out *os.File

	once   sync.Once
	closed atomic.Bool
}

func (c *Console) Read(b []byte) (int, error)  { return c.out.Read(b) }
func (c *Console) Write(b []byte) (int, error) { return c.in.Write(b) }

// Close ends the console first and the pipes after it. The order is the point: the console host
// holds the write end of the output pipe, so until it is gone a read of the output never returns,
// and closing a pipe that a read is blocked on waits for that read. Closing the console hangs up on
// the programs still attached to it (they receive CTRL_CLOSE_EVENT), which is the hangup a Unix PTY
// delivers when its master closes. On older Windows 10 builds ClosePseudoConsole waits until the
// output it still holds has been read; the terminal's reader goes on reading while it does.
func (c *Console) Close() error {
	c.once.Do(func() {
		c.closed.Store(true)
		windows.ClosePseudoConsole(c.hpc)
		_ = c.in.Close()
		_ = c.out.Close()
	})
	return nil
}

func (c *Console) resize(cols, rows int) error {
	if c.closed.Load() {
		return os.ErrClosed
	}
	return windows.ResizePseudoConsole(c.hpc, windows.Coord{X: int16(cols), Y: int16(rows)})
}

// Proc is a running program and its console.
type Proc struct {
	Master *Console
	Pid    int

	process windows.Handle
	job     *Job
	jobMu   sync.Mutex // guards job and process, which Close releases

	// ending is the signal name of an ending the daemon started ("HUP" once the console was closed on
	// the program, "KILL" once the job was terminated), so the exit it causes is reported as such.
	ending atomic.Value

	done chan struct{}
	exit Exit

	killOnce sync.Mutex
}

// Start runs spec on a new pseudoconsole, in a job of its own. The program is created suspended and
// resumed only once it is in the job, so nothing it starts can be born outside the job.
func Start(spec Spec) (*Proc, error) {
	if len(spec.Argv) == 0 {
		return nil, errors.New("empty argv")
	}
	env, err := EnvBlock(spec.Env)
	if err != nil {
		return nil, err
	}
	app, err := windows.UTF16PtrFromString(spec.Path)
	if err != nil {
		return nil, err
	}
	line, err := windows.UTF16PtrFromString(CommandLine(spec.Argv))
	if err != nil {
		return nil, err
	}
	var dir *uint16
	if spec.Dir != "" {
		if dir, err = windows.UTF16PtrFromString(spec.Dir); err != nil {
			return nil, err
		}
	}

	var inRead, inWrite, outRead, outWrite windows.Handle
	if err := windows.CreatePipe(&inRead, &inWrite, nil, 0); err != nil {
		return nil, fmt.Errorf("input pipe: %w", err)
	}
	if err := windows.CreatePipe(&outRead, &outWrite, nil, 0); err != nil {
		closeHandles(inRead, inWrite)
		return nil, fmt.Errorf("output pipe: %w", err)
	}
	var hpc windows.Handle
	size := windows.Coord{X: int16(spec.Cols), Y: int16(spec.Rows)}
	if err := windows.CreatePseudoConsole(size, inRead, outWrite, 0, &hpc); err != nil {
		closeHandles(inRead, inWrite, outRead, outWrite)
		return nil, fmt.Errorf("pseudoconsole: %w", err)
	}
	// The console host has its own duplicates of its ends; ours would only keep the pipes open after
	// the console is gone, and a read of the output would never see its end.
	closeHandles(inRead, outWrite)
	console := &Console{hpc: hpc, in: os.NewFile(uintptr(inWrite), "conpty-input"), out: os.NewFile(uintptr(outRead), "conpty-output")}

	job, err := NewJob()
	if err != nil {
		console.Close()
		return nil, fmt.Errorf("job object: %w", err)
	}
	processCtrlC()
	pi, err := createInConsole(app, line, env, dir, hpc)
	if err != nil {
		job.Close()
		console.Close()
		return nil, err
	}
	if err := job.Assign(pi.Process); err != nil {
		_ = windows.TerminateProcess(pi.Process, killedExitCode)
		closeHandles(pi.Thread, pi.Process)
		job.Close()
		console.Close()
		return nil, fmt.Errorf("job object: %w", err)
	}
	if _, err := windows.ResumeThread(pi.Thread); err != nil {
		_ = job.Terminate(killedExitCode)
		closeHandles(pi.Thread, pi.Process)
		job.Close()
		console.Close()
		return nil, fmt.Errorf("resuming %s: %w", spec.Path, err)
	}
	closeHandles(pi.Thread)
	p := &Proc{Master: console, Pid: int(pi.ProcessId), process: pi.Process, job: job, done: make(chan struct{})}
	p.ending.Store("")
	go p.wait()
	return p, nil
}

// createInConsole creates the program suspended, attached to the pseudoconsole hpc.
func createInConsole(app, line *uint16, env []uint16, dir *uint16, hpc windows.Handle) (*windows.ProcessInformation, error) {
	attrs, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		return nil, err
	}
	defer attrs.Delete()
	// The attribute's value is the console handle itself, not a pointer to it; the handle's bits are
	// handed over as the pointer-sized value the call expects.
	if err := attrs.Update(windows.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, *(*unsafe.Pointer)(unsafe.Pointer(&hpc)), unsafe.Sizeof(hpc)); err != nil {
		return nil, err
	}
	si := windows.StartupInfoEx{ProcThreadAttributeList: attrs.List()}
	si.Cb = uint32(unsafe.Sizeof(si))
	// Without this flag a daemon whose own standard handles are redirected (the launcher sends them
	// to a log file) passes those handles on, and the program writes to the daemon's log instead of
	// its console. With the flag and no handles, it takes the console's.
	si.Flags = windows.STARTF_USESTDHANDLES
	flags := uint32(windows.EXTENDED_STARTUPINFO_PRESENT | windows.CREATE_UNICODE_ENVIRONMENT | windows.CREATE_SUSPENDED)
	var pi windows.ProcessInformation
	if err := windows.CreateProcess(app, line, nil, nil, false, flags, &env[0], dir, &si.StartupInfo, &pi); err != nil {
		return nil, err
	}
	return &pi, nil
}

// processCtrlC turns the processing of Ctrl+C back on in the daemon, once, before its first program
// is created. Whether a process ignores Ctrl+C is inherited by every process it creates, and a process
// created in a new process group starts ignoring it: the launcher starts the daemon that way (so that
// it can be sent CTRL_BREAK_EVENT to stop), and so, without this, every program in every terminal
// ignored the Ctrl+C typed into its console, and an interrupt reached nothing. The daemon itself then
// takes a Ctrl+C in a console of its own as the interrupt it already stops on.
func processCtrlC() {
	ctrlC.Do(func() {
		_, _, _ = procSetConsoleCtrlHandler.Call(0, 0)
	})
}

var (
	ctrlC                     sync.Once
	procSetConsoleCtrlHandler = windows.NewLazySystemDLL("kernel32.dll").NewProc("SetConsoleCtrlHandler")
)

func closeHandles(hs ...windows.Handle) {
	for _, h := range hs {
		if h != 0 {
			_ = windows.CloseHandle(h)
		}
	}
}

func (p *Proc) wait() {
	_, _ = windows.WaitForSingleObject(p.process, windows.INFINITE)
	var code uint32
	if err := windows.GetExitCodeProcess(p.process, &code); err != nil {
		p.exit = Exit{Code: -1}
	} else if ending, _ := p.ending.Load().(string); ending == "KILL" || (ending != "" && code == statusControlCExit) {
		p.exit = Exit{Code: -1, Signal: ending}
	} else {
		p.exit = Exit{Code: int(int32(code))}
	}
	// The process handle stays open until Close: while it is open the pid cannot be given to another
	// process, so a late TerminateProcess can only reach this one, already gone.
	close(p.done)
}

// Done is closed when the program has exited.
func (p *Proc) Done() <-chan struct{} { return p.done }

// Wait blocks until the program exits and returns how it ended. It may be called any number of
// times.
func (p *Proc) Wait() Exit {
	<-p.done
	return p.exit
}

// Resize sets the console's size; the console host tells the program as a console does.
func (p *Proc) Resize(cols, rows, pxW, pxH int) error {
	return p.Master.resize(cols, rows)
}

// Foreground is not known on Windows: a console has no foreground process group to ask for.
func (p *Proc) Foreground() (int, error) { return 0, ErrUnsupported }

// ProgramGroup is the program itself. Nothing is wrapped on Windows.
func (p *Proc) ProgramGroup() int { return p.Pid }

// Signal delivers sig to the program alone: an interrupt as a typed Ctrl+C (the console host turns
// it into CTRL_C_EVENT for what is attached to it), an ending by terminating the program.
func (p *Proc) Signal(sig syscall.Signal) error { return p.signal(sig, false) }

// SignalForeground delivers sig to the terminal's whole tree: an interrupt as a typed Ctrl+C, which
// reaches every program attached to the console, an ending by terminating the job.
func (p *Proc) SignalForeground(sig syscall.Signal) error { return p.signal(sig, true) }

func (p *Proc) signal(sig syscall.Signal, tree bool) error {
	switch sig {
	case syscall.SIGINT:
		_, err := p.Master.Write([]byte{0x03})
		return err
	case syscall.SIGKILL, syscall.SIGTERM, syscall.SIGHUP, syscall.SIGQUIT:
		if p.exited() {
			return nil
		}
		p.ending.Store("KILL")
		if tree {
			return p.terminateJob()
		}
		p.jobMu.Lock()
		defer p.jobMu.Unlock()
		if p.process == 0 {
			return nil
		}
		return windows.TerminateProcess(p.process, killedExitCode)
	}
	return fmt.Errorf("%s: %w", SignalName(sig), ErrUnsupported)
}

func (p *Proc) terminateJob() error {
	p.jobMu.Lock()
	defer p.jobMu.Unlock()
	if p.job == nil {
		return nil
	}
	return p.job.Terminate(killedExitCode)
}

// KillTree ends the program and everything it started: the console is closed first, which is the
// hangup (every attached program receives CTRL_CLOSE_EVENT and may clean up), then after up to
// grace the job is terminated, which leaves nothing behind. It returns once the program has exited.
func (p *Proc) KillTree(grace time.Duration) Exit {
	p.killOnce.Lock()
	defer p.killOnce.Unlock()
	if !p.exited() {
		p.ending.CompareAndSwap("", "HUP")
		// Closing can take a while on older builds (it waits for its output to be read), so it runs
		// beside the grace rather than before it.
		go p.Master.Close()
		select {
		case <-p.done:
		case <-time.After(grace):
		}
	}
	if !p.exited() {
		p.ending.Store("KILL")
	}
	_ = p.terminateJob()
	return p.Wait()
}

func (p *Proc) exited() bool {
	select {
	case <-p.done:
		return true
	default:
		return false
	}
}

// Close releases the console and the job. Anything the program left running in the job ends with
// it, as a Unix PTY's hangup ends what still held the terminal.
func (p *Proc) Close() error {
	err := p.Master.Close()
	p.jobMu.Lock()
	if p.job != nil {
		_ = p.job.Close()
		p.job = nil
	}
	if p.process != 0 && p.exited() {
		closeHandles(p.process)
		p.process = 0
	}
	p.jobMu.Unlock()
	return err
}

// The signals the protocol names that Windows has no constant for. They parse, so a request for
// one is answered "not supported on this system" rather than "unknown signal".
const (
	sigTSTP  = syscall.Signal(0x14)
	sigCONT  = syscall.Signal(0x12)
	sigWINCH = syscall.Signal(0x1c)
	sigUSR1  = syscall.Signal(0x0a)
	sigUSR2  = syscall.Signal(0x0c)
)

var signalNames = map[syscall.Signal]string{
	syscall.SIGHUP: "HUP", syscall.SIGINT: "INT", syscall.SIGQUIT: "QUIT", syscall.SIGKILL: "KILL",
	syscall.SIGTERM: "TERM", sigTSTP: "TSTP", sigCONT: "CONT", sigWINCH: "WINCH", sigUSR1: "USR1",
	sigUSR2: "USR2",
}

// SignalName is the short name of a signal ("KILL"), or its number when it has none here.
func SignalName(s syscall.Signal) string {
	if n, ok := signalNames[s]; ok {
		return n
	}
	return fmt.Sprint(int(s))
}

// ParseSignal maps the names the protocol accepts to signals.
func ParseSignal(name string) (syscall.Signal, bool) {
	for sig, n := range signalNames {
		if n == name {
			return sig, true
		}
	}
	return 0, false
}
