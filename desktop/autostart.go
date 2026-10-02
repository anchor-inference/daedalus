package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Autostart is "start Daedalus when this user signs in". It is per user, needs no administrator,
// and Disable removes exactly what Enable wrote.
type Autostart interface {
	Enabled() (bool, error)
	Enable() error
	Disable() error
	// Where names the file or registry value, for the page to show and for the operator to find.
	Where() string
}

// runKey is the per-user Run key, HKCU\Software\Microsoft\Windows\CurrentVersion\Run. It is an
// interface so the Windows writer can be tested on any machine.
type runKey interface {
	Get(name string) (value string, found bool, err error)
	Set(name, value string) error
	Delete(name string) error
}

const (
	autostartName = "Daedalus"
	// autostartMarker is how the Linux writer recognises its own file: a hand-made daedalus.desktop
	// is the operator's and is neither reported as ours nor removed.
	autostartMarker = "X-Daedalus-Autostart=true"
	// autostartLabel follows the application's bundle identifier.
	autostartLabel = "io.github.anchor-inference.daedalus"
)

// NewAutostart picks the writer for an operating system. goos is a parameter so every writer can be
// tested on one machine; reg is only used for windows and defaults to the real registry.
func NewAutostart(goos, home string, command []string, reg runKey) Autostart {
	switch goos {
	case "linux":
		dir := os.Getenv("XDG_CONFIG_HOME")
		if dir == "" || !filepath.IsAbs(dir) {
			dir = filepath.Join(home, ".config")
		}
		return &desktopEntryAutostart{path: filepath.Join(dir, "autostart", "daedalus.desktop"), command: command}
	case "darwin":
		return &launchAgentAutostart{path: filepath.Join(home, "Library", "LaunchAgents", autostartLabel+".plist"), command: command}
	case "windows":
		if reg == nil {
			reg = systemRunKey()
		}
		return &runKeyAutostart{reg: reg, command: command}
	}
	return unsupportedAutostart{goos: goos}
}

// AutostartCommand is what to start at login: the installed application when this launcher sits
// beside one, because that is what the operator opens by hand, and otherwise the launcher itself
// with "start".
func AutostartCommand() []string {
	exe, err := os.Executable()
	if err != nil {
		return nil
	}
	if shell := shellExecutable(exe); shell != "" {
		return []string{shell}
	}
	return []string{exe, "start"}
}

type unsupportedAutostart struct{ goos string }

func (u unsupportedAutostart) err() error {
	return fmt.Errorf("starting at login is not supported on %s", u.goos)
}
func (u unsupportedAutostart) Enabled() (bool, error) { return false, u.err() }
func (u unsupportedAutostart) Enable() error          { return u.err() }
func (u unsupportedAutostart) Disable() error         { return u.err() }
func (u unsupportedAutostart) Where() string          { return "" }

// writeFileAtomic writes beside the target and renames, so a login that races the write reads the
// old file or the new one and never half of one.
func writeFileAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".daedalus-autostart-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(name)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(name)
		return err
	}
	// CreateTemp makes the file 0600; the entry is not a secret and some session managers run as
	// another uid-mapped helper.
	if err := os.Chmod(name, 0o644); err != nil {
		os.Remove(name)
		return err
	}
	if err := os.Rename(name, path); err != nil {
		os.Remove(name)
		return err
	}
	return nil
}

func requireCommand(command []string) error {
	if len(command) == 0 || command[0] == "" {
		return errors.New("there is no command to start at login")
	}
	return nil
}

// desktopEntryAutostart is the XDG autostart entry that GNOME, KDE, Xfce and the others read.
type desktopEntryAutostart struct {
	path    string
	command []string
}

func (d *desktopEntryAutostart) Where() string { return d.path }

func (d *desktopEntryAutostart) Enabled() (bool, error) {
	data, err := os.ReadFile(d.path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return strings.Contains(string(data), autostartMarker), nil
}

func (d *desktopEntryAutostart) Enable() error {
	if err := requireCommand(d.command); err != nil {
		return err
	}
	// A file we did not write is the operator's; replacing it would lose their edits.
	if data, err := os.ReadFile(d.path); err == nil && !strings.Contains(string(data), autostartMarker) {
		return fmt.Errorf("%s exists and was not written by Daedalus", d.path)
	}
	content := "[Desktop Entry]\n" +
		"Type=Application\n" +
		"Name=" + autostartName + "\n" +
		"Exec=" + desktopExec(d.command) + "\n" +
		"Terminal=false\n" +
		"NoDisplay=false\n" +
		"X-GNOME-Autostart-enabled=true\n" +
		autostartMarker + "\n"
	return writeFileAtomic(d.path, []byte(content))
}

func (d *desktopEntryAutostart) Disable() error {
	ours, err := d.Enabled()
	if err != nil || !ours {
		return err
	}
	if err := os.Remove(d.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// desktopExec quotes every argument for the Exec key. The specification asks for double quotes
// with backslash before the four characters the shell-like parser treats as special, and the file
// format then wants each backslash doubled; a percent sign would otherwise start a field code.
func desktopExec(command []string) string {
	parts := make([]string, len(command))
	for i, arg := range command {
		var b strings.Builder
		b.WriteByte('"')
		for _, r := range arg {
			switch r {
			case '"', '`', '$', '\\':
				b.WriteString(`\\`)
				b.WriteRune(r)
				if r == '\\' {
					b.WriteString(`\\`)
				}
			case '%':
				b.WriteString("%%")
			default:
				b.WriteRune(r)
			}
		}
		b.WriteByte('"')
		parts[i] = b.String()
	}
	return strings.Join(parts, " ")
}

// launchAgentAutostart is a per-user launchd agent that runs at login.
type launchAgentAutostart struct {
	path    string
	command []string
}

func (l *launchAgentAutostart) Where() string { return l.path }

func (l *launchAgentAutostart) Enabled() (bool, error) {
	data, err := os.ReadFile(l.path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return strings.Contains(string(data), "<string>"+autostartLabel+"</string>"), nil
}

func (l *launchAgentAutostart) Enable() error {
	if err := requireCommand(l.command); err != nil {
		return err
	}
	var b strings.Builder
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>` + autostartLabel + `</string>
	<key>ProgramArguments</key>
	<array>
`)
	for _, arg := range l.command {
		b.WriteString("\t\t<string>" + xmlEscape(arg) + "</string>\n")
	}
	b.WriteString(`	</array>
	<key>RunAtLoad</key>
	<true/>
</dict>
</plist>
`)
	return writeFileAtomic(l.path, []byte(b.String()))
}

func (l *launchAgentAutostart) Disable() error {
	ours, err := l.Enabled()
	if err != nil || !ours {
		return err
	}
	if err := os.Remove(l.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func xmlEscape(text string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&apos;").Replace(text)
}

// runKeyAutostart is the Windows Run value, which Explorer starts at sign-in.
type runKeyAutostart struct {
	reg     runKey
	command []string
}

const runKeyPath = `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

func (r *runKeyAutostart) Where() string { return runKeyPath + `\` + autostartName }

func (r *runKeyAutostart) Enabled() (bool, error) {
	_, found, err := r.reg.Get(autostartName)
	return found, err
}

func (r *runKeyAutostart) Enable() error {
	if err := requireCommand(r.command); err != nil {
		return err
	}
	parts := make([]string, len(r.command))
	for i, arg := range r.command {
		parts[i] = `"` + strings.ReplaceAll(arg, `"`, `\"`) + `"`
	}
	return r.reg.Set(autostartName, strings.Join(parts, " "))
}

func (r *runKeyAutostart) Disable() error {
	// Looked up first so a value that is not there is not an error whatever the registry says.
	found, err := r.Enabled()
	if err != nil || !found {
		return err
	}
	return r.reg.Delete(autostartName)
}
