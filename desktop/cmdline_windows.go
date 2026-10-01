//go:build windows

package main

// Both are read from the process itself through its handle. They used to be asked of CIM through a
// PowerShell started for each question: a quarter of a second on an idle machine, and on a cold,
// busy one — a fresh CI runner preparing PowerShell's modules for first use — longer than ten
// seconds. A child's record waits for its start marker, so for all that time a child was running
// with no record a crashed launcher could be found by, and a test waiting for the record gave up.

import (
	"runtime"
	"strconv"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// commandLineOf is a process's command line, as the process holds it.
func commandLineOf(pid int) (string, bool) {
	process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", false
	}
	defer windows.CloseHandle(process)
	size := uint32(1024)
	for {
		// Whole words, so the UNICODE_STRING at the head of the answer is aligned; its text
		// follows it in the same buffer.
		buffer := make([]uint64, (size+7)/8)
		var needed uint32
		err := windows.NtQueryInformationProcess(process, windows.ProcessCommandLineInformation, unsafe.Pointer(&buffer[0]), uint32(len(buffer)*8), &needed)
		if err == nil {
			line := strings.TrimSpace((*windows.NTUnicodeString)(unsafe.Pointer(&buffer[0])).String())
			runtime.KeepAlive(buffer)
			return line, line != ""
		}
		if (err == windows.STATUS_INFO_LENGTH_MISMATCH || err == windows.STATUS_BUFFER_TOO_SMALL || err == windows.STATUS_BUFFER_OVERFLOW) && needed > size {
			size = needed
			continue
		}
		return "", false
	}
}

// dotnetEpochOffset is the 100-nanosecond intervals from 0001-01-01, where .NET counts its ticks,
// to 1601-01-01, where a FILETIME starts.
const dotnetEpochOffset = 504911232000000000

// processStartMarker is the process's creation time. It is written as the value CIM gave — .NET
// ticks in UTC, to the microsecond CIM keeps — so a record written by a launcher that still asked
// CIM names the same process to this one.
func processStartMarker(pid int) (string, bool) {
	process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return "", false
	}
	defer windows.CloseHandle(process)
	var creation, exit, kernel, user windows.Filetime
	if err := windows.GetProcessTimes(process, &creation, &exit, &kernel, &user); err != nil {
		return "", false
	}
	ticks := int64(creation.HighDateTime)<<32 | int64(creation.LowDateTime)
	if ticks == 0 {
		return "", false
	}
	ticks += dotnetEpochOffset
	return strconv.FormatInt(ticks-ticks%10, 10), true
}
