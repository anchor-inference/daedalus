package ptytest

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// Stats is what a recording says about how a program draws.
//
// The numbers answer the flicker question from the byte stream rather than from a screenshot. A
// full clear (CSI 2J) blanks the screen for as long as the terminal takes to receive the redraw, so
// one in the middle of streaming is a visible flash on any terminal that paints between reads. An
// erase sequence (ED, EL) is the same risk in a smaller area. Bytes inside a synchronized update
// (CSI ?2026h … CSI ?2026l) are painted at once by terminals that support it, so a high share of
// them means even the erases inside are invisible there.
type Stats struct {
	Bytes int
	// Chunks is the number of reads the output arrived in.
	Chunks int
	// SyncFrames is the number of synchronized updates; UnsyncedChunks the chunks that carried
	// output outside any synchronized update.
	SyncFrames     int
	UnsyncedChunks int
	// SyncShare is the share of output bytes that arrived inside synchronized updates.
	SyncShare float64
	// SteadySyncShare is the same share counted from the first synchronized update to the last:
	// the drawing itself, without the start-up queries and the teardown, which dominate a short run.
	SteadySyncShare float64
	// MaxFrameBytes is the largest synchronized update.
	MaxFrameBytes int
	// Erases counts ED (CSI J, 0J, 1J, 2J, 3J) and EL (CSI K, 0K, 1K, 2K) sequences; FullClears
	// counts CSI 2J alone, and FullClearsAfterFirst those after the first frame was drawn.
	Erases               int
	FullClears           int
	FullClearsAfterFirst int
	ScrollbackClears     int
	// FullClearTimes are when each full clear arrived, to tell a clear on resize (expected: the
	// whole frame changes) from one in the middle of streaming (a flash).
	FullClearTimes []time.Duration
	// Duration is the time from the first chunk to the last; ErasesPerSecond is over that window.
	Duration        time.Duration
	ErasesPerSecond float64
	// AltScreenEnters and AltScreenLeaves count switches to and from the alternate screen.
	AltScreenEnters int
	AltScreenLeaves int
}

func (s Stats) String() string {
	return fmt.Sprintf(
		"bytes=%d chunks=%d sync_frames=%d unsynced_chunks=%d sync_share=%.3f steady_sync_share=%.3f max_frame_bytes=%d "+
			"erases=%d erases_per_s=%.1f full_clears=%d full_clears_after_first=%d scrollback_clears=%d "+
			"alt_enter=%d alt_leave=%d duration=%s full_clear_times=%v",
		s.Bytes, s.Chunks, s.SyncFrames, s.UnsyncedChunks, s.SyncShare, s.SteadySyncShare, s.MaxFrameBytes,
		s.Erases, s.ErasesPerSecond, s.FullClears, s.FullClearsAfterFirst, s.ScrollbackClears,
		s.AltScreenEnters, s.AltScreenLeaves, s.Duration.Round(time.Millisecond), roundAll(s.FullClearTimes))
}

// Analyze reads a recording's escape sequences. The chunks are scanned as one stream, so a
// sequence split across two reads counts once.
func Analyze(r Recording) Stats {
	var stats Stats
	stats.Chunks = len(r.Chunks)
	if len(r.Chunks) > 0 {
		stats.Duration = r.Chunks[len(r.Chunks)-1].At - r.Chunks[0].At
	}
	var stream []byte
	starts := make([]int, len(r.Chunks))
	for i, chunk := range r.Chunks {
		starts[i] = len(stream)
		stream = append(stream, chunk.Data...)
	}
	stats.Bytes = len(stream)
	// chunkAt is the index of the chunk holding byte offset, advanced as the scan moves forward.
	chunk := 0
	chunkAt := func(offset int) int {
		for chunk+1 < len(starts) && starts[chunk+1] <= offset {
			chunk++
		}
		return chunk
	}

	inSync, drawnFirst := false, false
	frameStart, syncBytes := 0, 0
	firstSync, lastSyncEnd := -1, -1
	unsyncedChunk := map[int]bool{}
	for i := 0; i < len(stream); i++ {
		b := stream[i]
		if b != 0x1b || i+1 >= len(stream) || stream[i+1] != '[' {
			if !inSync && b >= 0x20 && b != 0x1b {
				unsyncedChunk[chunkAt(i)] = true
			}
			continue
		}
		end := i + 2
		for end < len(stream) && (stream[end] < 0x40 || stream[end] > 0x7e) {
			end++
		}
		if end >= len(stream) {
			break
		}
		params := string(stream[i+2 : end])
		final := stream[end]
		at := chunkAt(i)
		switch {
		case final == 'h' && params == "?2026":
			inSync = true
			frameStart = i
			stats.SyncFrames++
			if firstSync < 0 {
				firstSync = i
			}
		case final == 'l' && params == "?2026":
			if inSync {
				size := end + 1 - frameStart
				syncBytes += size
				stats.MaxFrameBytes = max(stats.MaxFrameBytes, size)
			}
			inSync = false
			drawnFirst = true
			lastSyncEnd = end + 1
		}
		switch final {
		case 'J':
			if params == "" || params == "0" || params == "1" || params == "2" || params == "3" {
				stats.Erases++
			}
			if params == "2" {
				stats.FullClears++
				stats.FullClearTimes = append(stats.FullClearTimes, r.Chunks[at].At)
				if drawnFirst {
					stats.FullClearsAfterFirst++
				}
			}
			if params == "3" {
				stats.ScrollbackClears++
			}
		case 'K':
			if params == "" || params == "0" || params == "1" || params == "2" {
				stats.Erases++
			}
		case 'h', 'l':
			if isAltScreenMode(params) {
				if final == 'h' {
					stats.AltScreenEnters++
				} else {
					stats.AltScreenLeaves++
				}
			}
		}
		if !inSync && (final == 'J' || final == 'K' || final == 'm' || final == 'H') {
			// Drawing outside a synchronized update; mode switches and queries are not drawing.
			unsyncedChunk[at] = true
			if stats.SyncFrames == 0 {
				drawnFirst = true
			}
		}
		i = end
	}
	stats.UnsyncedChunks = len(unsyncedChunk)
	if stats.Bytes > 0 {
		stats.SyncShare = float64(syncBytes) / float64(stats.Bytes)
	}
	if firstSync >= 0 && lastSyncEnd > firstSync {
		stats.SteadySyncShare = float64(syncBytes) / float64(lastSyncEnd-firstSync)
	}
	if seconds := stats.Duration.Seconds(); seconds > 0 {
		stats.ErasesPerSecond = float64(stats.Erases) / seconds
	}
	return stats
}

func isAltScreenMode(params string) bool {
	switch strings.TrimPrefix(params, "?") {
	case "1049", "1047", "47":
		return strings.HasPrefix(params, "?")
	}
	return false
}

// WriteCast writes the recording as an asciicast v2 file, which asciinema and its web player
// replay. A resize becomes an "r" event, as asciinema itself records one.
func (r Recording) WriteCast(path string) error {
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()
	header, _ := json.Marshal(map[string]any{
		"version": 2,
		"width":   r.Cols,
		"height":  r.Rows,
		"env":     map[string]string{"TERM": "xterm-256color"},
	})
	if _, err := fmt.Fprintf(file, "%s\n", header); err != nil {
		return err
	}
	resizes := r.Resizes
	// A read can end inside a multi-byte character; the cut bytes wait for the next chunk rather
	// than being replaced, so the cast replays the characters the program actually wrote.
	var carry []byte
	for _, chunk := range r.Chunks {
		data := append(carry, chunk.Data...)
		cut := incompleteTail(data)
		carry = append([]byte(nil), data[len(data)-cut:]...)
		data = data[:len(data)-cut]
		for len(resizes) > 0 && resizes[0].At <= chunk.At {
			line, _ := json.Marshal([]any{resizes[0].At.Seconds(), "r", fmt.Sprintf("%dx%d", resizes[0].Cols, resizes[0].Rows)})
			if _, err := fmt.Fprintf(file, "%s\n", line); err != nil {
				return err
			}
			resizes = resizes[1:]
		}
		line, _ := json.Marshal([]any{chunk.At.Seconds(), "o", strings.ToValidUTF8(string(data), "\uFFFD")})
		if _, err := fmt.Fprintf(file, "%s\n", line); err != nil {
			return err
		}
	}
	return file.Close()
}

// WriteCast writes what the term has recorded so far as an asciicast v2 file.
func (t *Term) WriteCast(path string) error { return t.Recording().WriteCast(path) }

// incompleteTail is how many bytes at the end of data begin a UTF-8 character that is not complete.
func incompleteTail(data []byte) int {
	for back := 1; back <= 3 && back <= len(data); back++ {
		b := data[len(data)-back]
		if b&0xC0 == 0x80 {
			continue
		}
		var need int
		switch {
		case b&0xE0 == 0xC0:
			need = 2
		case b&0xF0 == 0xE0:
			need = 3
		case b&0xF8 == 0xF0:
			need = 4
		default:
			return 0
		}
		if back < need {
			return back
		}
		return 0
	}
	return 0
}

func roundAll(times []time.Duration) []time.Duration {
	out := make([]time.Duration, len(times))
	for i, t := range times {
		out[i] = t.Round(time.Millisecond)
	}
	return out
}
