// Package textedit is the editing engine behind the prompt box: a multi-line buffer with a cursor,
// readline's keys, a kill ring, undo, paste placeholders and an optional vim mode.
//
// It knows nothing about the screen around it. The prompt box asks it to handle a key and gets back
// what the key meant — handled, submit, or "walk the history" — and asks it for the wrapped rows to
// draw with the cursor's cell. Keeping it apart from the interface's model is what lets every key be
// tested as a table of keys in and text out, with no program running.
//
// Offsets are in runes throughout, never bytes: the cursor of a prompt holding Cyrillic or CJK text
// would otherwise land inside a character. Widths on screen are display cells, so a wide character
// takes two.
package textedit

import (
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Outcome is what a key meant to the editor.
type Outcome int

const (
	// Unhandled is a key the editor does not own; the caller decides what it does.
	Unhandled Outcome = iota
	// Handled is a key that edited the buffer or moved the cursor.
	Handled
	// Submit is Enter on a prompt that should be sent.
	Submit
	// HistoryPrev is Up on the first row: the caller steps back through the prompt history.
	HistoryPrev
	// HistoryNext is Down on the last row: the caller steps forward through the prompt history.
	HistoryNext
)

// The thresholds above which a paste collapses into a placeholder. They are the ones Claude Code uses:
// a paste that would push the prompt box past a few rows is shown as one token, so the person can
// still see and edit what they typed around it, and the content goes out in full on submit.
const (
	pasteMaxChars    = 800
	pasteMaxNewlines = 2
)

// undoDepth bounds the undo stack. A prompt is short-lived; a few hundred steps is more than anyone
// walks back through, and an unbounded stack would grow for the whole of a long editing session.
const undoDepth = 200

// killRingDepth is how many kills the ring keeps for yank-pop to cycle through, as readline does.
const killRingDepth = 20

// tokenPattern matches the two placeholders the editor treats as single characters. "lines?" accepts
// the singular so a hand-typed "+1 line" still reads as the token it looks like.
var tokenPattern = regexp.MustCompile(`\[Pasted text #(\d+) \+(\d+) lines?\]|\[Image #(\d+)\]`)

type editKind int

const (
	editNone editKind = iota
	editTyping
	editOther
)

type action int

const (
	actOther action = iota
	actKill
	actYank
)

type snapshot struct {
	buf []rune
	cur int
}

// Editor is the prompt's buffer and cursor. The zero value is not ready; use New.
type Editor struct {
	buf []rune
	cur int

	// width is the width of the last Render. Moving up and down needs to know where rows wrap, and
	// the rows are only defined for a width; the last one drawn is the one the person is looking at.
	width int
	// goal is the column vertical moves aim for, kept across consecutive moves so walking through a
	// short row does not drag the cursor to the left for good. -1 when no vertical move is running.
	goal int

	pasted   map[int]string
	pasteSeq int

	undo     []snapshot
	lastEdit editKind

	ring       []string
	lastAction action
	yankStart  int
	yankEnd    int
	yankIndex  int
	lastKey    string

	vimOn bool
	vim   vimState
}

// New returns an empty editor.
func New() *Editor {
	return &Editor{width: 80, goal: -1, pasted: map[int]string{}}
}

// Value is the buffer as shown, placeholders included.
func (e *Editor) Value() string { return string(e.buf) }

// Expanded is the buffer as it should be sent: each pasted-text placeholder is replaced by what was
// pasted. Image placeholders stay, because the image itself travels beside the text and the token is
// how the text refers to it.
func (e *Editor) Expanded() string {
	return tokenPattern.ReplaceAllStringFunc(string(e.buf), func(token string) string {
		groups := tokenPattern.FindStringSubmatch(token)
		if groups[1] == "" {
			return token
		}
		n, _ := strconv.Atoi(groups[1])
		if content, ok := e.pasted[n]; ok {
			return content
		}
		return token
	})
}

// SetValue replaces the buffer, puts the cursor at the end and forgets the undo history: a value set
// from outside (a history entry, a withdrawn queued message) is a new starting point, and undoing
// into the previous prompt would be a surprise.
func (e *Editor) SetValue(s string) {
	e.buf = []rune(s)
	e.cur = len(e.buf)
	e.undo = nil
	e.lastEdit = editNone
	e.lastAction = actOther
	e.goal = -1
}

// Reset empties the buffer and forgets pasted content, as after a prompt is sent.
func (e *Editor) Reset() {
	e.SetValue("")
	e.pasted = map[int]string{}
	e.pasteSeq = 0
	if e.vimOn {
		e.vim = vimState{mode: vimInsert}
	}
}

// Empty reports whether the buffer holds nothing at all.
func (e *Editor) Empty() bool { return len(e.buf) == 0 }

// Cursor is the cursor's rune offset.
func (e *Editor) Cursor() int { return e.cur }

// SetCursor moves the cursor, clamped to the buffer and kept off the inside of a placeholder.
func (e *Editor) SetCursor(offset int) {
	e.cur = clamp(offset, 0, len(e.buf))
	e.cur = e.snapOut(e.cur, true)
	e.goal = -1
}

// InsertText types text at the cursor. Consecutive insertions are one undo step, the way an editor
// undoes a typed word rather than a typed letter.
func (e *Editor) InsertText(s string) {
	if s == "" {
		return
	}
	e.save(editTyping)
	e.insert(s)
}

// Paste inserts pasted text. Line endings are normalised first: a paste from a Windows clipboard
// carries CRLF, and a bare CR would draw as a line that overwrites itself. A paste over the
// thresholds becomes a placeholder, and the paste is always its own undo step.
func (e *Editor) Paste(s string) {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.ReplaceAll(s, "\r", "\n")
	if s == "" {
		return
	}
	e.save(editOther)
	newlines := strings.Count(s, "\n")
	if utf8.RuneCountInString(s) > pasteMaxChars || newlines > pasteMaxNewlines {
		e.pasteSeq++
		e.pasted[e.pasteSeq] = s
		e.insert("[Pasted text #" + strconv.Itoa(e.pasteSeq) + " +" + strconv.Itoa(newlines) + " lines]")
	} else {
		e.insert(s)
	}
	e.lastEdit = editOther
}

// InsertImage inserts the token that refers to the n-th pasted image.
func (e *Editor) InsertImage(n int) {
	e.save(editOther)
	e.insert("[Image #" + strconv.Itoa(n) + "]")
	e.lastEdit = editOther
}

// Images lists the image numbers the buffer still refers to, in order of first appearance. An image
// whose token the person deleted is not sent.
func (e *Editor) Images() []int {
	var out []int
	seen := map[int]bool{}
	for _, groups := range tokenPattern.FindAllStringSubmatch(string(e.buf), -1) {
		if groups[3] == "" {
			continue
		}
		n, _ := strconv.Atoi(groups[3])
		if !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	return out
}

// Undo restores the buffer as it was before the last edit step.
func (e *Editor) Undo() {
	if len(e.undo) == 0 {
		return
	}
	last := e.undo[len(e.undo)-1]
	e.undo = e.undo[:len(e.undo)-1]
	e.buf = last.buf
	e.cur = clamp(last.cur, 0, len(e.buf))
	e.lastEdit = editNone
	e.lastAction = actOther
	e.goal = -1
}

// WordBeforeCursor is the run of non-space characters ending at the cursor, and where it starts. The
// caller uses it to complete `@file` mentions and `/command` names.
func (e *Editor) WordBeforeCursor() (string, int) {
	start := e.cur
	for start > 0 && !isSpace(e.buf[start-1]) {
		start--
	}
	return string(e.buf[start:e.cur]), start
}

// ReplaceRange replaces the runes in [start, end) with s and leaves the cursor after it, as one undo
// step. Used to accept a completion.
func (e *Editor) ReplaceRange(start, end int, s string) {
	start = clamp(start, 0, len(e.buf))
	end = clamp(end, start, len(e.buf))
	e.save(editOther)
	e.replace(start, end, s)
	e.cur = start + utf8.RuneCountInString(s)
	e.lastEdit = editOther
	e.goal = -1
}

// save records the buffer before an edit. Typing coalesces with the typing before it; anything else
// starts a step of its own.
func (e *Editor) save(kind editKind) {
	if kind == editTyping && e.lastEdit == editTyping {
		return
	}
	copied := make([]rune, len(e.buf))
	copy(copied, e.buf)
	e.undo = append(e.undo, snapshot{buf: copied, cur: e.cur})
	if len(e.undo) > undoDepth {
		e.undo = e.undo[len(e.undo)-undoDepth:]
	}
	e.lastEdit = kind
}

// breakCoalescing ends a run of typing: the next typed character starts a new undo step. Any cursor
// move does this, so typing, moving and typing again undoes in two steps, as editors do.
func (e *Editor) breakCoalescing() {
	if e.lastEdit == editTyping {
		e.lastEdit = editNone
	}
}

func (e *Editor) insert(s string) {
	runes := []rune(s)
	e.replace(e.cur, e.cur, s)
	e.cur += len(runes)
	e.goal = -1
}

func (e *Editor) replace(start, end int, s string) {
	runes := []rune(s)
	out := make([]rune, 0, len(e.buf)-(end-start)+len(runes))
	out = append(out, e.buf[:start]...)
	out = append(out, runes...)
	out = append(out, e.buf[end:]...)
	e.buf = out
}

func (e *Editor) delete(start, end int) string {
	start = clamp(start, 0, len(e.buf))
	end = clamp(end, start, len(e.buf))
	removed := string(e.buf[start:end])
	e.replace(start, end, "")
	if e.cur > end {
		e.cur -= end - start
	} else if e.cur > start {
		e.cur = start
	}
	return removed
}

// span is a placeholder's place in the buffer, in runes, end exclusive.
type span struct{ start, end int }

func (e *Editor) tokens() []span {
	s := string(e.buf)
	if !strings.Contains(s, "[") {
		return nil
	}
	var out []span
	for _, loc := range tokenPattern.FindAllStringIndex(s, -1) {
		start := utf8.RuneCountInString(s[:loc[0]])
		out = append(out, span{start, start + utf8.RuneCountInString(s[loc[0]:loc[1]])})
	}
	return out
}

// tokenEndingAt is the placeholder that ends exactly at offset, if any.
func (e *Editor) tokenEndingAt(offset int) (span, bool) {
	for _, t := range e.tokens() {
		if t.end == offset {
			return t, true
		}
	}
	return span{}, false
}

// tokenStartingAt is the placeholder that starts exactly at offset, if any.
func (e *Editor) tokenStartingAt(offset int) (span, bool) {
	for _, t := range e.tokens() {
		if t.start == offset {
			return t, true
		}
	}
	return span{}, false
}

// snapOut moves an offset that fell inside a placeholder to one of its edges: the end when moving
// forward, the start when moving back. A cursor inside a token would let a keystroke split it into
// text that no longer refers to anything.
func (e *Editor) snapOut(offset int, forward bool) int {
	for _, t := range e.tokens() {
		if offset > t.start && offset < t.end {
			if forward {
				return t.end
			}
			return t.start
		}
	}
	return offset
}

// kill removes [start, end) into the kill ring. Kills in a row join into one ring entry, the way
// readline lets ctrl+w pressed three times be yanked back as the three words.
func (e *Editor) kill(start, end int, prepend bool) {
	start = clamp(start, 0, len(e.buf))
	end = clamp(end, start, len(e.buf))
	if start == end {
		return
	}
	e.save(editOther)
	text := e.delete(start, end)
	if e.lastAction == actKill && len(e.ring) > 0 {
		if prepend {
			e.ring[len(e.ring)-1] = text + e.ring[len(e.ring)-1]
		} else {
			e.ring[len(e.ring)-1] += text
		}
	} else {
		e.ring = append(e.ring, text)
		if len(e.ring) > killRingDepth {
			e.ring = e.ring[len(e.ring)-killRingDepth:]
		}
	}
	e.cur = start
	e.lastEdit = editOther
	e.lastAction = actKill
}

func (e *Editor) yank() {
	if len(e.ring) == 0 {
		return
	}
	e.save(editOther)
	e.yankIndex = len(e.ring) - 1
	e.yankStart = e.cur
	e.insert(e.ring[e.yankIndex])
	e.yankEnd = e.cur
	e.lastEdit = editOther
	e.lastAction = actYank
}

// yankPop replaces the text just yanked with the previous ring entry. It belongs to the yank's undo
// step, so one undo takes the whole yank and its pops back.
func (e *Editor) yankPop() {
	if e.lastAction != actYank || len(e.ring) == 0 {
		return
	}
	e.yankIndex = (e.yankIndex - 1 + len(e.ring)) % len(e.ring)
	text := e.ring[e.yankIndex]
	e.replace(e.yankStart, e.yankEnd, text)
	e.yankEnd = e.yankStart + utf8.RuneCountInString(text)
	e.cur = e.yankEnd
	e.lastAction = actYank
}

func (e *Editor) lineStart(offset int) int {
	for offset > 0 && e.buf[offset-1] != '\n' {
		offset--
	}
	return offset
}

func (e *Editor) lineEnd(offset int) int {
	for offset < len(e.buf) && e.buf[offset] != '\n' {
		offset++
	}
	return offset
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func isSpace(r rune) bool { return r == ' ' || r == '\t' || r == '\n' }
