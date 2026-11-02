package markdown

import (
	"strings"
)

// StablePrefix splits streaming Markdown at the last block boundary: a blank line outside a fenced
// code block and outside a rich-answer block (":::card", ":::flow"). Everything before the boundary
// is finished and renders the same however the answer continues; everything after it is the block
// still being written.
//
// A fence or a rich block is never split, because half of one renders as something else entirely:
// an unclosed fence swallows the rest of the answer into code, and half a card is flattened with its
// closing marker left to show up raw in the next block.
func StablePrefix(text string) (stable, open string) {
	boundary := 0
	fence := ""
	rich := false
	offset := 0
	lines := strings.SplitAfter(text, "\n")
	for _, raw := range lines {
		line := strings.TrimRight(raw, "\r\n")
		complete := strings.HasSuffix(raw, "\n")
		trimmed := strings.TrimLeft(line, " ")
		switch {
		case fence != "":
			if complete && closesFence(trimmed, fence) {
				fence = ""
			}
		case rich:
			if complete && cardCloseRE.MatchString(line) {
				rich = false
			}
		case isFence(trimmed):
			fence = fenceMarker(trimmed)
		case cardOpenRE.MatchString(line) || flowOpenRE.MatchString(line):
			rich = true
		case complete && strings.TrimSpace(line) == "":
			boundary = offset + len(raw)
		}
		offset += len(raw)
	}
	return text[:boundary], text[boundary:]
}

func isFence(trimmed string) bool {
	return strings.HasPrefix(trimmed, "```") || strings.HasPrefix(trimmed, "~~~")
}

// fenceMarker is the run of backticks or tildes that opened a fence; the fence closes on a run of the
// same character at least as long, as CommonMark has it.
func fenceMarker(trimmed string) string {
	c := trimmed[0]
	n := 0
	for n < len(trimmed) && trimmed[n] == c {
		n++
	}
	return trimmed[:n]
}

func closesFence(trimmed, marker string) bool {
	if !strings.HasPrefix(trimmed, marker) {
		return false
	}
	rest := strings.TrimLeft(trimmed, marker[:1])
	return strings.TrimSpace(rest) == ""
}

// Stream is an answer being written: text appended as it arrives, rendered with every finished block
// drawn once and cached, and only the open block drawn again on each frame. It is not safe for
// concurrent use; the interface owns one per streaming message.
type Stream struct {
	renderer *Renderer
	text     strings.Builder
	// committed is the length of the text already split off into chunks.
	committed int
	chunks    []chunk
}

type chunk struct {
	source string
	width  int
	lines  []string
}

// NewStream starts an empty stream drawn by r.
func NewStream(r *Renderer) *Stream { return &Stream{renderer: r} }

// Append adds a piece of the answer.
func (s *Stream) Append(delta string) {
	if delta == "" {
		return
	}
	s.text.WriteString(delta)
	stable, _ := StablePrefix(s.text.String()[s.committed:])
	if strings.TrimSpace(stable) != "" {
		s.chunks = append(s.chunks, chunk{source: stable, width: -1})
		s.committed += len(stable)
	} else if stable != "" {
		// Blank lines only: nothing to draw, but they are behind the boundary for good.
		s.committed += len(stable)
	}
}

// Text is the whole answer so far.
func (s *Stream) Text() string { return s.text.String() }

// Reset empties the stream.
func (s *Stream) Reset() {
	s.text.Reset()
	s.committed = 0
	s.chunks = nil
}

// StableLines is how many of Render's lines at width belong to finished blocks: the lines that will
// not change however the answer continues. Inline mode prints those into the terminal's scrollback
// while the answer is still streaming and keeps only the rest live.
func (s *Stream) StableLines(width int) int {
	n := 0
	for i := range s.chunks {
		c := &s.chunks[i]
		if c.width != width {
			c.lines = splitRendered(s.renderer.Render(c.source, width))
			c.width = width
		}
		if len(c.lines) == 0 {
			continue
		}
		if n > 0 {
			n++
		}
		n += len(c.lines)
	}
	if n > 0 && strings.TrimSpace(s.text.String()[s.committed:]) != "" {
		// The blank line that joins the last finished block to the open one is finished too.
		n++
	}
	return n
}

// Render draws the answer at width. Finished chunks are rendered again only when the width changes;
// the open tail is rendered on every call. Chunks are joined by one blank line, which is what the
// blank line that ended each of them would have rendered as in one document.
func (s *Stream) Render(width int) []string {
	var out []string
	add := func(lines []string) {
		if len(lines) == 0 {
			return
		}
		if len(out) > 0 {
			out = append(out, "")
		}
		out = append(out, lines...)
	}
	for i := range s.chunks {
		c := &s.chunks[i]
		if c.width != width {
			c.lines = splitRendered(s.renderer.Render(c.source, width))
			c.width = width
		}
		add(c.lines)
	}
	if tail := s.text.String()[s.committed:]; strings.TrimSpace(tail) != "" {
		add(splitRendered(s.renderer.Render(tail, width)))
	}
	return out
}

func splitRendered(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}
