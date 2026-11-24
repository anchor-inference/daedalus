package page

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The pictures page.screenshot keeps for shot.read. A reply is one frame of at most 1 MiB, and a
// crisp picture is not: a viewport at twice its CSS pixels as PNG is two or three megabytes, so it
// stays here and the host reads it in chunks, as it reads a download. Few are kept and not for long:
// each is read once, straight after it was taken, and is dropped at its end.
const (
	keptShots   = 4
	keptShotFor = 2 * time.Minute
)

type keptShot struct {
	data  []byte
	taken time.Time
}

// keepShot holds data for shot.read and names it.
func (p *Model) keepShot(data []byte) string {
	raw := make([]byte, 8)
	_, _ = rand.Read(raw)
	id := "s" + hex.EncodeToString(raw)
	now := time.Now()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.shots == nil {
		p.shots = map[string]*keptShot{}
	}
	for k, s := range p.shots {
		if now.Sub(s.taken) > keptShotFor {
			delete(p.shots, k)
		}
	}
	for len(p.shots) >= keptShots {
		var oldest string
		for k, s := range p.shots {
			if oldest == "" || s.taken.Before(p.shots[oldest].taken) {
				oldest = k
			}
		}
		delete(p.shots, oldest)
	}
	p.shots[id] = &keptShot{data: data, taken: now}
	return id
}

// ReadShot is shot.read: a chunk of a kept picture from offset. The picture is forgotten once its
// last chunk has been read.
func (p *Model) ReadShot(id string, offset int64, max int) (map[string]any, error) {
	if max <= 0 || max > ChunkBytes {
		max = ChunkBytes
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	s := p.shots[id]
	if s == nil {
		return nil, wire.Errorf(wire.CodeNotFound, "no picture %q is kept (each is read once, within %s)", id, keptShotFor)
	}
	size := int64(len(s.data))
	if offset < 0 || offset > size {
		return nil, wire.Errorf(wire.CodeInvalidParams, "offset %d is outside the picture (%d bytes)", offset, size)
	}
	end := min(size, offset+int64(max))
	eof := end == size
	if eof {
		delete(p.shots, id)
	}
	return map[string]any{"data_b64": base64.StdEncoding.EncodeToString(s.data[offset:end]), "eof": eof, "size": size}, nil
}
