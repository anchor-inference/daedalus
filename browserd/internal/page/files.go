package page

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"context"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// ChunkBytes bounds one download.read or upload.put.
const ChunkBytes = 512 << 10

// Download is the contract's Download.
type Download struct {
	ID         string     `json:"id"`
	GroupID    string     `json:"group_id"`
	TabID      string     `json:"tab_id"`
	Name       string     `json:"name"`
	URL        string     `json:"url"`
	Mime       string     `json:"mime,omitempty"`
	Size       int64      `json:"size"`
	State      string     `json:"state"`
	SHA256     string     `json:"sha256,omitempty"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`

	guid    string
	profile string
	browser *browser.Browser
	path    string // where the finished file is kept
}

func newID(prefix string) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return prefix + hex.EncodeToString(b)
}

// BrowserEvent follows downloads, which Chromium announces on the browser, not on the page.
func (p *Model) BrowserEvent(b *browser.Browser, e cdp.Event) {
	switch e.Method {
	case "Browser.downloadWillBegin":
		var d struct {
			FrameID           string `json:"frameId"`
			GUID              string `json:"guid"`
			URL               string `json:"url"`
			SuggestedFilename string `json:"suggestedFilename"`
		}
		if json.Unmarshal(e.Params, &d) != nil {
			return
		}
		t := p.m.TabByTarget(d.FrameID)
		var g *browser.Group
		if t != nil {
			g = t.Group
		} else {
			// A download a subframe started: the browser's most recently active group owns it.
			for _, x := range p.m.Groups(b.ID) {
				if g == nil || x.View()["last_activity_at"].(time.Time).After(g.View()["last_activity_at"].(time.Time)) {
					g = x
				}
			}
		}
		if g == nil {
			return
		}
		dl := &Download{ID: newID("d"), GroupID: g.ID, Name: safeName(d.SuggestedFilename), URL: d.URL,
			State: "in_progress", StartedAt: time.Now().UTC(), guid: d.GUID, profile: g.Profile, browser: b}
		if t != nil {
			dl.TabID = t.ID
		}
		p.mu.Lock()
		p.downloads[dl.ID] = dl
		p.byGUID[d.GUID] = dl
		p.mu.Unlock()
		p.m.Publish("download.started", map[string]any{"group_id": g.ID, "download": dl.snapshot()})
	case "Browser.downloadProgress":
		var d struct {
			GUID          string  `json:"guid"`
			TotalBytes    float64 `json:"totalBytes"`
			ReceivedBytes float64 `json:"receivedBytes"`
			State         string  `json:"state"`
		}
		if json.Unmarshal(e.Params, &d) != nil {
			return
		}
		p.mu.Lock()
		dl := p.byGUID[d.GUID]
		if dl == nil || dl.State != "in_progress" {
			p.mu.Unlock()
			return
		}
		dl.Size = int64(d.ReceivedBytes)
		tooLarge := p.lim.MaxDownloadBytes > 0 && (int64(d.ReceivedBytes) > p.lim.MaxDownloadBytes || int64(d.TotalBytes) > p.lim.MaxDownloadBytes)
		p.mu.Unlock()
		switch {
		case tooLarge:
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = b.CancelDownload(ctx, d.GUID)
			}()
			p.finish(dl, "too_large")
		case d.State == "completed" && b.Attached():
			// Where the operator's Chrome put the file is a question to the extension, which the
			// events behind this one need not wait for.
			go p.finish(dl, "completed")
		case d.State == "completed":
			p.finish(dl, "completed")
		case d.State == "canceled":
			p.finish(dl, "canceled")
		}
	}
}

// finish records a download's end, keeps its file under the group, and makes room in the profile's
// share.
func (p *Model) finish(dl *Download, state string) {
	now := time.Now().UTC()
	p.mu.Lock()
	if dl.State != "in_progress" {
		p.mu.Unlock()
		return
	}
	dl.State = state
	dl.FinishedAt = &now
	p.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// The file is the daemon's to move only from its own download directory. A download in the
	// operator's Chrome stays in their downloads, where they expect it, and the daemon keeps a copy.
	var src string
	var own bool
	srcErr := errors.New("not asked")
	if state == "completed" || !dl.browser.Attached() {
		src, own, srcErr = dl.browser.DownloadPath(ctx, dl.guid)
	}
	if state == "completed" {
		dir := filepath.Join(p.stateDir, "downloads", "groups", dl.GroupID)
		path := filepath.Join(dir, dl.ID)
		err := srcErr
		if err == nil {
			err = os.MkdirAll(dir, 0o700)
		}
		if err == nil && own {
			err = os.Rename(src, path)
		} else if err == nil {
			err = copyFile(src, path)
		}
		if err != nil {
			p.log.Warn("keeping a download", "error", err.Error())
			state = "failed"
		} else {
			sum, size := hashFile(path)
			p.mu.Lock()
			dl.path, dl.SHA256, dl.Size = path, sum, size
			p.mu.Unlock()
		}
	} else if srcErr == nil && own {
		_ = os.Remove(src)
	}
	p.mu.Lock()
	dl.State = state
	p.mu.Unlock()
	p.evict(dl.profile)
	p.m.Publish("download.done", map[string]any{"group_id": dl.GroupID, "download": dl.snapshot()})
}

// copyFile copies a finished download the daemon does not own into its keeping.
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func hashFile(path string) (string, int64) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0
	}
	defer f.Close()
	h := sha256.New()
	n, _ := io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)), n
}

// evict removes a profile's oldest finished downloads past its share.
func (p *Model) evict(profile string) {
	p.mu.Lock()
	var mine []*Download
	var total int64
	for _, d := range p.downloads {
		if d.profile == profile && d.path != "" {
			mine = append(mine, d)
			total += d.Size
		}
	}
	sort.Slice(mine, func(i, j int) bool { return mine[i].StartedAt.Before(mine[j].StartedAt) })
	var gone []*Download
	for _, d := range mine {
		if total <= p.lim.MaxProfileDownloads || p.lim.MaxProfileDownloads <= 0 {
			break
		}
		total -= d.Size
		delete(p.downloads, d.ID)
		delete(p.byGUID, d.guid)
		gone = append(gone, d)
	}
	p.mu.Unlock()
	for _, d := range gone {
		_ = os.Remove(d.path)
	}
}

func (d *Download) snapshot() Download {
	c := *d
	return c
}

func (p *Model) downloadCount(group string) int {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := 0
	for _, d := range p.downloads {
		if d.GroupID == group {
			n++
		}
	}
	return n
}

// latestDownload is the newest download of a group, when it has more than before.
func (p *Model) latestDownload(group string, before int) *Download {
	p.mu.Lock()
	defer p.mu.Unlock()
	var latest *Download
	n := 0
	for _, d := range p.downloads {
		if d.GroupID != group {
			continue
		}
		n++
		if latest == nil || d.StartedAt.After(latest.StartedAt) {
			latest = d
		}
	}
	if n <= before {
		return nil
	}
	c := latest.snapshot()
	return &c
}

// Downloads lists a group's downloads, oldest first.
func (p *Model) Downloads(group string) []Download {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := []Download{}
	for _, d := range p.downloads {
		if d.GroupID == group {
			out = append(out, d.snapshot())
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt.Before(out[j].StartedAt) })
	return out
}

// ReadDownload reads a finished download in chunks.
func (p *Model) ReadDownload(id string, offset int64, max int) (map[string]any, error) {
	p.mu.Lock()
	d := p.downloads[id]
	var path string
	if d != nil {
		path = d.path
	}
	p.mu.Unlock()
	if d == nil {
		return nil, wire.Errorf(wire.CodeNotFound, "no download %q", id)
	}
	if path == "" {
		return nil, wire.Errorf(wire.CodeForbidden, "download %q is %s, not completed", id, d.State)
	}
	if max <= 0 || max > ChunkBytes {
		max = ChunkBytes
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if offset < 0 || offset > st.Size() {
		return nil, wire.Errorf(wire.CodeInvalidParams, "offset %d is outside the file (%d bytes)", offset, st.Size())
	}
	buf := make([]byte, max)
	n, err := f.ReadAt(buf, offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return map[string]any{"data_b64": base64.StdEncoding.EncodeToString(buf[:n]), "offset": offset, "size": st.Size(),
		"eof": offset+int64(n) >= st.Size()}, nil
}

// DeleteDownload removes a download and its file.
func (p *Model) DeleteDownload(id string) error {
	p.mu.Lock()
	d := p.downloads[id]
	if d != nil {
		delete(p.downloads, id)
		delete(p.byGUID, d.guid)
	}
	p.mu.Unlock()
	if d == nil {
		return wire.Errorf(wire.CodeNotFound, "no download %q", id)
	}
	if d.path != "" {
		_ = os.Remove(d.path)
	}
	return nil
}

type upload struct {
	id    string
	group string
	path  string
	size  int64
}

// safeName is a file name that is one plain part: no directories, no dots that climb.
func safeName(name string) string {
	name = strings.TrimSpace(filepath.Base(strings.ReplaceAll(name, "\\", "/")))
	if name == "" || name == "." || name == ".." || name == "/" {
		return "download"
	}
	if len(name) > 200 {
		for len(name) > 200 || !utf8.ValidString(name) {
			name = name[:len(name)-1]
		}
	}
	return name
}

// PutUpload writes one chunk of a file the host hands in for a file input.
func (p *Model) PutUpload(group *browser.Group, uploadID, name string, offset int64, data string) (map[string]any, error) {
	raw, err := base64.StdEncoding.DecodeString(data)
	if err != nil {
		return nil, wire.Errorf(wire.CodeInvalidParams, "data_b64 is not base64")
	}
	if len(raw) > ChunkBytes {
		return nil, wire.Errorf(wire.CodeInvalidParams, "a chunk is at most %d bytes", ChunkBytes)
	}
	if name != safeName(name) || strings.ContainsAny(name, "/\\") {
		return nil, wire.Errorf(wire.CodeInvalidParams, "name must be a plain file name")
	}
	p.mu.Lock()
	var u *upload
	if uploadID == "" {
		if offset != 0 {
			p.mu.Unlock()
			return nil, wire.Errorf(wire.CodeInvalidParams, "a new upload starts at offset 0")
		}
		id := newID("u")
		u = &upload{id: id, group: group.ID, path: filepath.Join(p.stateDir, "uploads", group.ID, id, name)}
		p.uploads[id] = u
	} else {
		u = p.uploads[uploadID]
		if u == nil || u.group != group.ID {
			p.mu.Unlock()
			return nil, wire.Errorf(wire.CodeNotFound, "no upload %q in this group", uploadID)
		}
		if filepath.Base(u.path) != name {
			p.mu.Unlock()
			return nil, wire.Errorf(wire.CodeInvalidParams, "upload %q is named %q", uploadID, filepath.Base(u.path))
		}
	}
	if offset != u.size {
		p.mu.Unlock()
		return nil, wire.Errorf(wire.CodeInvalidParams, "upload %q holds %d bytes; continue at that offset", u.id, u.size)
	}
	if p.lim.MaxUploadBytes > 0 && u.size+int64(len(raw)) > p.lim.MaxUploadBytes {
		p.mu.Unlock()
		return nil, wire.Errorf(wire.CodeLimit, "an upload is at most %d bytes", p.lim.MaxUploadBytes)
	}
	p.mu.Unlock()
	if err := os.MkdirAll(filepath.Dir(u.path), 0o700); err != nil {
		return nil, err
	}
	flags := os.O_WRONLY | os.O_CREATE | os.O_APPEND
	if offset == 0 {
		flags = os.O_WRONLY | os.O_CREATE | os.O_EXCL
	}
	f, err := os.OpenFile(u.path, flags, 0o600)
	if err != nil {
		return nil, err
	}
	_, err = f.Write(raw)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return nil, err
	}
	p.mu.Lock()
	u.size += int64(len(raw))
	size := u.size
	p.mu.Unlock()
	return map[string]any{"upload_id": u.id, "size": size}, nil
}

func (p *Model) uploadPaths(group string, ids []string) ([]string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []string
	for _, id := range ids {
		u := p.uploads[id]
		if u == nil || u.group != group {
			return nil, wire.Errorf(wire.CodeNotFound, "no upload %q in this group", id)
		}
		out = append(out, u.path)
	}
	return out, nil
}

// GroupClosed removes a closed group's uploads and downloads: its files go with it.
func (p *Model) GroupClosed(group string) {
	p.mu.Lock()
	for id, u := range p.uploads {
		if u.group == group {
			delete(p.uploads, id)
		}
	}
	for id, d := range p.downloads {
		if d.GroupID == group {
			delete(p.downloads, id)
			delete(p.byGUID, d.guid)
		}
	}
	p.mu.Unlock()
	_ = os.RemoveAll(filepath.Join(p.stateDir, "uploads", group))
	_ = os.RemoveAll(filepath.Join(p.stateDir, "downloads", "groups", group))
}
