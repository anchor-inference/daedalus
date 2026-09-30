package main

import (
	"strings"
	"testing"
)

// Every character PowerShell reads as a single quote is doubled.
func TestTheWindowsCommandSurvivesEveryPowerShellQuote(t *testing.T) {
	for _, quote := range []string{"'", "\u2018", "\u2019", "\u201A", "\u201B"} {
		exe := `C:\Users\O` + quote + `Brien\Daedalus\daedalus-desktop.exe`
		got := commandLineFor("windows", exe, `C:\Users\O`+quote+`Brien\Daedalus\data`)
		want := "& '" + strings.ReplaceAll(exe, quote, quote+quote) + "' upgrade --data '" + strings.ReplaceAll(`C:\Users\O`+quote+`Brien\Daedalus\data`, quote, quote+quote) + "'"
		if got != want {
			t.Errorf("%U: %s", []rune(quote)[0], got)
		}
		// Read back the way PowerShell reads a single-quoted string: a doubled quote character is one.
		if back := readPowerShellSingleQuoted(t, strings.TrimPrefix(got, "& ")); back != exe {
			t.Errorf("%U: read back as %q", []rune(quote)[0], back)
		}
	}
}

func isPSQuote(r rune) bool {
	return r == '\'' || r == '\u2018' || r == '\u2019' || r == '\u201A' || r == '\u201B'
}

// readPowerShellSingleQuoted parses the first single-quoted string, by PowerShell's rules.
func readPowerShellSingleQuoted(t *testing.T, s string) string {
	runes := []rune(s)
	if len(runes) == 0 || !isPSQuote(runes[0]) {
		t.Fatalf("not a quoted string: %s", s)
	}
	var out []rune
	for i := 1; i < len(runes); i++ {
		if isPSQuote(runes[i]) {
			if i+1 < len(runes) && isPSQuote(runes[i+1]) {
				out = append(out, runes[i])
				i++
				continue
			}
			return string(out)
		}
		out = append(out, runes[i])
	}
	t.Fatalf("unterminated: %s", s)
	return ""
}

// install.ps1's ConvertTo-WindowsArgument, ported line for line, checked against the rules a
// Windows program's C runtime (CommandLineToArgvW) splits a command line by. This checks the
// algorithm; the PowerShell text itself has not been run (no PowerShell here).
func convertToWindowsArgument(arg string) string {
	if arg != "" && !strings.ContainsAny(arg, " \t\n\v\"") {
		return arg
	}
	var b strings.Builder
	b.WriteByte('"')
	slashes := 0
	for _, c := range arg {
		if c == '\\' {
			slashes++
			continue
		}
		if c == '"' {
			b.WriteString(strings.Repeat(`\`, slashes*2+1) + `"`)
		} else {
			b.WriteString(strings.Repeat(`\`, slashes))
			b.WriteRune(c)
		}
		slashes = 0
	}
	b.WriteString(strings.Repeat(`\`, slashes*2) + `"`)
	return b.String()
}

// commandLineToArgv splits by the documented CommandLineToArgvW rules (arguments after the first).
func commandLineToArgv(line string) []string {
	var args []string
	var cur strings.Builder
	inQuotes, have := false, false
	runes := []rune(line)
	for i := 0; i < len(runes); i++ {
		c := runes[i]
		switch {
		case c == '\\':
			n := 0
			for i < len(runes) && runes[i] == '\\' {
				n++
				i++
			}
			if i < len(runes) && runes[i] == '"' {
				cur.WriteString(strings.Repeat(`\`, n/2))
				if n%2 == 1 {
					cur.WriteRune('"')
				} else {
					inQuotes = !inQuotes
				}
				have = true
			} else {
				cur.WriteString(strings.Repeat(`\`, n))
				i--
				have = true
			}
		case c == '"':
			if inQuotes && i+1 < len(runes) && runes[i+1] == '"' {
				cur.WriteRune('"')
				i++
			} else {
				inQuotes = !inQuotes
			}
			have = true
		case (c == ' ' || c == '\t') && !inQuotes:
			if have {
				args = append(args, cur.String())
				cur.Reset()
				have = false
			}
		default:
			cur.WriteRune(c)
			have = true
		}
	}
	if have {
		args = append(args, cur.String())
	}
	return args
}

func TestWindowsArgumentsSurviveTheCRuntime(t *testing.T) {
	args := []string{
		"upgrade", "--bridge", "--root", `C:\`, "--data", `C:\Users\A B\Daedalus\data`,
		"--archive", `C:\Users\A B\AppData\Local\Temp\x\daedalus-desktop-windows-amd64.zip`,
		`ends with two \\`, `has "quotes" inside`, `\\server\share\`, "", `a\"b`, `trailing space `,
	}
	var parts []string
	for _, a := range args {
		parts = append(parts, convertToWindowsArgument(a))
	}
	got := commandLineToArgv(strings.Join(parts, " "))
	if len(got) != len(args) {
		t.Fatalf("split into %d, want %d: %q", len(got), len(args), got)
	}
	for i := range args {
		if got[i] != args[i] {
			t.Errorf("%q came back as %q", args[i], got[i])
		}
	}
	// And wrapping in quotes only, as an earlier version did, breaks on the drive root.
	naive := commandLineToArgv(`"--root" "C:\" "--data" "x"`)
	if len(naive) == 4 {
		t.Fatalf("the naive quoting was expected to break: %q", naive)
	}
}
