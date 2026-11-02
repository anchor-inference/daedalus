package tui

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
)

// maxImageBytes bounds a pasted picture. A screenshot of a 5K display as PNG is under this; anything
// larger is almost always a mistake (a RAW photo, a whole PDF rendered), and the upload would stall
// the prompt.
const maxImageBytes = 20 << 20

// clipboardWall bounds a clipboard helper. An X11 tool with no display or a compositor that stopped
// answering would otherwise hang the paste while somebody waits on a key press.
const clipboardWall = 5 * time.Second

var imageTypes = []string{"image/png", "image/jpeg", "image/gif", "image/webp"}

// pasteImage reads a picture from the system clipboard for Ctrl+V. A terminal cannot carry an image
// in a bracketed paste, so the client asks the platform's own helper, the way Claude Code does.
func (m *Model) pasteImage() tea.Cmd {
	read := m.options.Clipboard
	if read == nil {
		read = readClipboardImage
	}
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(m.ctx, clipboardWall*2)
		defer cancel()
		image, err := read(ctx)
		return imageMsg{image: image, err: err}
	}
}

// errNoImage is the ordinary answer when the clipboard holds text: Ctrl+V then says so instead of
// pretending to paste.
var errNoImage = errors.New("No image on the clipboard")

func readClipboardImage(ctx context.Context) (Image, error) {
	switch runtime.GOOS {
	case "darwin":
		return readViaTempFile(ctx, "osascript", func(path string) []string {
			script := fmt.Sprintf(`set f to open for access POSIX file %q with write permission
write (the clipboard as «class PNGf») to f
close access f`, path)
			return []string{"-e", script}
		})
	case "windows":
		return readViaTempFile(ctx, "powershell", func(path string) []string {
			script := fmt.Sprintf(`Add-Type -AssemblyName System.Windows.Forms; $i = [System.Windows.Forms.Clipboard]::GetImage(); if ($i -eq $null) { exit 1 }; $i.Save('%s', [System.Drawing.Imaging.ImageFormat]::Png)`, path)
			return []string{"-NoProfile", "-Command", script}
		})
	}
	// Wayland before X11: a Wayland session usually also has XWayland, and xclip there reads a bridge
	// the application that owns the selection may never have written to.
	tools := []struct {
		name string
		list []string
		read func(mime string) []string
	}{
		{"wl-paste", []string{"--list-types"}, func(mime string) []string { return []string{"--no-newline", "--type", mime} }},
		{"xclip", []string{"-selection", "clipboard", "-t", "TARGETS", "-o"}, func(mime string) []string {
			return []string{"-selection", "clipboard", "-t", mime, "-o"}
		}},
	}
	var failures []string
	for _, tool := range tools {
		path, err := exec.LookPath(tool.name)
		if err != nil {
			continue
		}
		listed, err := runCapped(ctx, path, tool.list)
		if err != nil {
			failures = append(failures, tool.name+": "+err.Error())
			continue
		}
		types := strings.Fields(string(listed))
		for _, mime := range imageTypes {
			if !contains(types, mime) {
				continue
			}
			data, err := runCapped(ctx, path, tool.read(mime))
			if err != nil {
				failures = append(failures, tool.name+": "+err.Error())
				break
			}
			if len(data) > 0 {
				return Image{Name: imageName(mime), MIME: mime, Data: data}, nil
			}
		}
		return Image{}, errNoImage
	}
	if len(failures) > 0 {
		return Image{}, errors.New(strings.Join(failures, "; "))
	}
	return Image{}, errors.New("No clipboard tool found (install wl-clipboard or xclip)")
}

func readViaTempFile(ctx context.Context, tool string, args func(path string) []string) (Image, error) {
	f, err := os.CreateTemp("", "daedalus-paste-*.png")
	if err != nil {
		return Image{}, err
	}
	path := f.Name()
	_ = f.Close()
	defer os.Remove(path)
	if _, err := runCapped(ctx, tool, args(path)); err != nil {
		return Image{}, errNoImage
	}
	data, err := os.ReadFile(path)
	if err != nil || len(data) == 0 {
		return Image{}, errNoImage
	}
	if len(data) > maxImageBytes {
		return Image{}, fmt.Errorf("The image is %s, over the %s limit", formatBytes(len(data)), formatBytes(maxImageBytes))
	}
	return Image{Name: imageName("image/png"), MIME: "image/png", Data: data}, nil
}

func runCapped(ctx context.Context, path string, args []string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, clipboardWall)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		if msg := strings.TrimSpace(stderr.String()); msg != "" {
			return nil, errors.New(firstLine(msg))
		}
		return nil, err
	}
	if stdout.Len() > maxImageBytes {
		return nil, fmt.Errorf("the clipboard holds %s, over the %s limit", formatBytes(stdout.Len()), formatBytes(maxImageBytes))
	}
	return stdout.Bytes(), nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if strings.EqualFold(s, want) {
			return true
		}
	}
	return false
}

func imageName(mime string) string {
	ext := strings.TrimPrefix(mime, "image/")
	if ext == "jpeg" {
		ext = "jpg"
	}
	return "pasted-" + time.Now().Format("20060102-150405") + "." + ext
}

// isImagePath reports a pasted path to a picture: dragging a file onto a terminal pastes its path,
// and Claude Code treats a dragged image as an attachment rather than as text.
func isImagePath(s string) bool {
	s = strings.Trim(s, `'"`)
	switch strings.ToLower(filepath.Ext(s)) {
	case ".png", ".jpg", ".jpeg", ".gif", ".webp":
	default:
		return false
	}
	info, err := os.Stat(s)
	return err == nil && info.Mode().IsRegular()
}

func (m *Model) loadImageFile(path string) tea.Cmd {
	path = strings.Trim(path, `'"`)
	return func() tea.Msg {
		data, err := os.ReadFile(path)
		if err != nil {
			return imageMsg{err: err}
		}
		if len(data) > maxImageBytes {
			return imageMsg{err: fmt.Errorf("The image is %s, over the %s limit", formatBytes(len(data)), formatBytes(maxImageBytes))}
		}
		return imageMsg{image: Image{Name: filepath.Base(path), MIME: http.DetectContentType(data), Data: data}}
	}
}
