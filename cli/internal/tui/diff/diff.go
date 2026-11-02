// Package diff computes and draws the inline diffs of file edits: line-numbered rows tinted red and
// green, with the changed words inside a changed line marked more strongly, as Claude Code draws them.
package diff

import "strings"

// Op says what happened to a line.
type Op int

const (
	Context Op = iota
	Add
	Remove
	// Elision stands for unchanged lines left out between two changes. It carries how many, because
	// "17 unchanged lines" and "2 unchanged lines" are different amounts of reason to go and look.
	Elision
)

// Line is one row of a diff. Old and New are the line's numbers in the file before and after: a
// context line has both, a removed line only Old, an added line only New. Elided counts the lines an
// Elision row stands for.
type Line struct {
	Op     Op
	Old    int
	New    int
	Text   string
	Elided int
}

// contextLines is how many unchanged lines flank a change: the convention every diff reader already
// has in their eye.
const contextLines = 3

// maxCells bounds the longest-common-subsequence table. The table is quadratic, and a generated file
// rewritten wholesale would otherwise allocate hundreds of megabytes to draw a diff nobody reads line
// by line. Past the bound the change is shown as the old block removed and the new block added, which
// is honest and costs nothing.
const maxCells = 4_000_000

// Lines describes the change writing after over before would make, with three lines of context
// around each change and elisions between hunks.
//
// A line-level longest common subsequence is what a person reading a file edit wants: the shape of the
// edit, not the smallest edit script. The common prefix and suffix are taken off first, so an edit in
// the middle of a large file aligns only the lines around it.
func Lines(before, after string) []Line {
	if before == after {
		return nil
	}
	return compress(script(splitLines(before), splitLines(after)))
}

// FromEdit describes an Edit tool call whose file is not known: the replaced text against the new
// text, numbered from the first line of the snippet.
func FromEdit(oldString, newString string) []Line {
	return Lines(oldString, newString)
}

// Count returns how many lines the change adds and removes, from the script rather than from what is
// drawn, so a cut diff still reports the whole change.
func Count(lines []Line) (added, removed int) {
	for _, line := range lines {
		switch line.Op {
		case Add:
			added++
		case Remove:
			removed++
		}
	}
	return added, removed
}

func script(oldLines, newLines []string) []Line {
	prefix := 0
	for prefix < len(oldLines) && prefix < len(newLines) && oldLines[prefix] == newLines[prefix] {
		prefix++
	}
	suffix := 0
	for suffix < len(oldLines)-prefix && suffix < len(newLines)-prefix &&
		oldLines[len(oldLines)-1-suffix] == newLines[len(newLines)-1-suffix] {
		suffix++
	}
	var out []Line
	for i := 0; i < prefix; i++ {
		out = append(out, Line{Op: Context, Old: i + 1, New: i + 1, Text: newLines[i]})
	}
	midOld := oldLines[prefix : len(oldLines)-suffix]
	midNew := newLines[prefix : len(newLines)-suffix]
	if len(midOld)*len(midNew) > maxCells {
		for i, text := range midOld {
			out = append(out, Line{Op: Remove, Old: prefix + i + 1, Text: text})
		}
		for j, text := range midNew {
			out = append(out, Line{Op: Add, New: prefix + j + 1, Text: text})
		}
	} else {
		out = append(out, walk(midOld, midNew, prefix)...)
	}
	for k := 0; k < suffix; k++ {
		i := len(oldLines) - suffix + k
		j := len(newLines) - suffix + k
		out = append(out, Line{Op: Context, Old: i + 1, New: j + 1, Text: newLines[j]})
	}
	return out
}

// walk aligns the two middles with a longest-common-subsequence table and reads it back as an
// ordered script. Removals come before additions inside a changed stretch, which is the order the
// word highlighter pairs them in.
func walk(oldLines, newLines []string, offset int) []Line {
	cols := len(newLines) + 1
	table := make([]int32, (len(oldLines)+1)*cols)
	for i := len(oldLines) - 1; i >= 0; i-- {
		for j := len(newLines) - 1; j >= 0; j-- {
			if oldLines[i] == newLines[j] {
				table[i*cols+j] = table[(i+1)*cols+j+1] + 1
			} else {
				table[i*cols+j] = max(table[(i+1)*cols+j], table[i*cols+j+1])
			}
		}
	}
	var out []Line
	i, j := 0, 0
	for i < len(oldLines) && j < len(newLines) {
		switch {
		case oldLines[i] == newLines[j]:
			out = append(out, Line{Op: Context, Old: offset + i + 1, New: offset + j + 1, Text: newLines[j]})
			i, j = i+1, j+1
		case table[(i+1)*cols+j] >= table[i*cols+j+1]:
			out = append(out, Line{Op: Remove, Old: offset + i + 1, Text: oldLines[i]})
			i++
		default:
			out = append(out, Line{Op: Add, New: offset + j + 1, Text: newLines[j]})
			j++
		}
	}
	for ; i < len(oldLines); i++ {
		out = append(out, Line{Op: Remove, Old: offset + i + 1, Text: oldLines[i]})
	}
	for ; j < len(newLines); j++ {
		out = append(out, Line{Op: Add, New: offset + j + 1, Text: newLines[j]})
	}
	return out
}

// compress drops the unchanged stretches between changes, keeping a few lines of context either side
// and saying how many were left out.
func compress(script []Line) []Line {
	keep := make([]bool, len(script))
	changed := false
	for i, line := range script {
		if line.Op == Context {
			continue
		}
		changed = true
		for k := max(0, i-contextLines); k <= min(len(script)-1, i+contextLines); k++ {
			keep[k] = true
		}
	}
	if !changed {
		// The two differ only in a trailing newline the split already discarded; no card wants that.
		return nil
	}
	var out []Line
	elided := 0
	for i, line := range script {
		if !keep[i] {
			elided++
			continue
		}
		if elided > 0 {
			out = append(out, Line{Op: Elision, Elided: elided})
			elided = 0
		}
		out = append(out, line)
	}
	if elided > 0 {
		out = append(out, Line{Op: Elision, Elided: elided})
	}
	return out
}

// splitLines splits on newlines without inventing a trailing empty line for text that ends in one:
// every text file does, and a phantom last line would show in every diff as a change that is not there.
// Carriage returns go too, so a CRLF file does not draw a stray cell at the end of every row.
func splitLines(s string) []string {
	if s == "" {
		return nil
	}
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.TrimSuffix(s, "\n")
	return strings.Split(s, "\n")
}
