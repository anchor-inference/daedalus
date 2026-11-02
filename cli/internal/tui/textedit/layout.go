package textedit

import (
	"strings"

	"github.com/charmbracelet/x/ansi"
)

// row is one drawn row: a slice of the buffer, end exclusive, never containing a newline.
type row struct{ start, end int }

// runeWidth is a character's width in cells, measured the way lipgloss measures the lines the prompt
// box is drawn from, so the cursor the editor reports sits where the box draws the character.
func runeWidth(r rune) int {
	if r == '\t' {
		// A tab is drawn as one space by the box; anything wider would need tab stops the box does
		// not keep, and a cursor reported at the wrong column is worse than a narrow tab.
		return 1
	}
	return ansi.StringWidth(string(r))
}

func (e *Editor) widthOf(start, end int) int {
	w := 0
	for _, r := range e.buf[start:end] {
		w += runeWidth(r)
	}
	return w
}

// layout wraps the buffer into rows of at most width cells. A row breaks after its last space when
// it has one, so words stay whole; a word longer than the row is broken where it overflows.
func (e *Editor) layout(width int) []row {
	if width < 1 {
		width = 1
	}
	var rows []row
	lineStart := 0
	for lineStart <= len(e.buf) {
		lineEnd := e.lineEnd(lineStart)
		start, used, lastSpace := lineStart, 0, -1
		for i := lineStart; i < lineEnd; i++ {
			w := runeWidth(e.buf[i])
			// A loop rather than an if: after breaking at a space the carried-over part plus a wide
			// character can still overflow, and then the row breaks again at the character.
			for used+w > width && i > start {
				end := i
				if lastSpace >= start && lastSpace+1 < i {
					end = lastSpace + 1
				}
				rows = append(rows, row{start, end})
				start = end
				used = e.widthOf(start, i)
				lastSpace = -1
			}
			used += w
			if e.buf[i] == ' ' {
				lastSpace = i
			}
		}
		rows = append(rows, row{start, lineEnd})
		lineStart = lineEnd + 1
	}
	return rows
}

// locate is the cursor's row and column in a layout. A cursor at the end of a full row is placed at
// the start of the next one, which may be a row past the last: the box then draws an empty row for
// it, as every editor does when the text exactly fills the line.
func (e *Editor) locate(rows []row, offset, width int) (int, int) {
	for i, r := range rows {
		if offset >= r.start && offset < r.end {
			return i, e.widthOf(r.start, offset)
		}
		if offset == r.end {
			if i+1 < len(rows) && rows[i+1].start == offset {
				continue
			}
			col := e.widthOf(r.start, r.end)
			if col >= width {
				return i + 1, 0
			}
			return i, col
		}
	}
	last := len(rows) - 1
	return last, e.widthOf(rows[last].start, rows[last].end)
}

// offsetAt is the offset in a row nearest to a column, never past it. A wrapped row's last offset is
// its end minus one, since its end is the next row's first character.
func (e *Editor) offsetAt(rows []row, index, col int) int {
	r := rows[index]
	limit := r.end
	if index+1 < len(rows) && rows[index+1].start == r.end && r.end > r.start {
		limit = r.end - 1
	}
	offset, used := r.start, 0
	for offset < limit {
		w := runeWidth(e.buf[offset])
		if used+w > col {
			break
		}
		used += w
		offset++
	}
	return offset
}

// Render is the buffer wrapped to width, as plain rows, and the cursor's cell. It never returns no
// rows: an empty prompt is one empty row, so the box keeps its height.
func (e *Editor) Render(width int) ([]string, int, int) {
	if width < 1 {
		width = 1
	}
	e.width = width
	rows := e.layout(width)
	lines := make([]string, len(rows), len(rows)+1)
	for i, r := range rows {
		lines[i] = strings.ReplaceAll(string(e.buf[r.start:r.end]), "\t", " ")
	}
	cursorRow, cursorCol := e.locate(rows, e.cur, width)
	if cursorRow >= len(lines) {
		lines = append(lines, "")
	}
	return lines, cursorRow, cursorCol
}

// Lines is how many rows the buffer takes at the width of the last Render.
func (e *Editor) Lines() int { return e.LinesFor(e.width) }

// LinesFor is how many rows the buffer takes at a width, without changing the remembered one.
func (e *Editor) LinesFor(width int) int {
	saved := e.width
	lines, _, _ := e.Render(width)
	e.width = saved
	return len(lines)
}

// vertical moves the cursor one row up or down at the last rendered width. It reports false when
// the cursor is already on the first (or last) row, which is where the caller walks the history.
func (e *Editor) vertical(delta int) bool {
	width := e.width
	if width < 1 {
		width = 80
	}
	rows := e.layout(width)
	current, col := e.locate(rows, e.cur, width)
	if current >= len(rows) {
		// The cursor is on the virtual row after a full last row: up goes to that full row.
		if delta > 0 {
			return false
		}
		current = len(rows)
	}
	target := current + delta
	if target < 0 || target >= len(rows) {
		return false
	}
	if e.goal < 0 {
		e.goal = col
	}
	e.cur = e.snapOut(e.offsetAt(rows, target, e.goal), true)
	return true
}
