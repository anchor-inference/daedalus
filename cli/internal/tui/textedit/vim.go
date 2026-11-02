package textedit

import (
	"strings"
	"unicode"
	"unicode/utf8"

	tea "charm.land/bubbletea/v2"
)

// The vim mode covers what people reach for in a prompt — motions, the d/c/y operators with counts,
// the insert commands, put, undo, replace — not the whole editor. A prompt is a few lines; marks,
// registers by name, visual mode and ex commands would be weight nobody uses here.

type vimMode int

const (
	vimInsert vimMode = iota
	vimNormal
)

// vimState is the normal mode's half-typed command: a count, an operator waiting for its motion, a
// `g` waiting for its second `g`, an `r` waiting for its character.
type vimState struct {
	mode    vimMode
	count   int
	op      string
	opCount int
	g       bool
	r       bool
	// register is what d, c, y and x put away for p and P. Kept apart from the readline kill ring,
	// as vim's unnamed register is apart from the shell's.
	register string
	linewise bool
}

// SetVim turns vim mode on or off. It starts in insert mode, so turning it on mid-prompt does not
// make the next letter typed a command.
func (e *Editor) SetVim(on bool) {
	e.vimOn = on
	e.vim = vimState{mode: vimInsert}
}

// Vim reports whether vim mode is on.
func (e *Editor) Vim() bool { return e.vimOn }

// VimMode names the vim mode for the status line, or "" when vim mode is off.
func (e *Editor) VimMode() string {
	if !e.vimOn {
		return ""
	}
	if e.vim.mode == vimNormal {
		return "NORMAL"
	}
	return "INSERT"
}

func (e *Editor) enterNormal() {
	e.vim.mode = vimNormal
	e.vim.count, e.vim.op, e.vim.opCount, e.vim.g, e.vim.r = 0, "", 0, false, false
	e.breakCoalescing()
	// Leaving insert mode steps back onto the character just typed, as vim does: normal mode's
	// cursor sits on a character, never after the last one.
	if e.cur > e.lineStart(e.cur) {
		e.cur--
	}
	e.clampNormal()
}

// enterInsert switches to insert mode. joinTyping is true after a command that already changed the
// buffer (cw, o, cc): the text typed next belongs to the same undo step, so one u undoes the change
// and what replaced it together.
func (e *Editor) enterInsert(joinTyping bool) {
	e.vim.mode = vimInsert
	if joinTyping {
		e.lastEdit = editTyping
	} else {
		e.lastEdit = editNone
	}
}

// clampNormal keeps the normal-mode cursor on a character of its line.
func (e *Editor) clampNormal() {
	if e.vim.mode != vimNormal {
		return
	}
	e.cur = clamp(e.cur, 0, len(e.buf))
	start, end := e.lineStart(e.cur), e.lineEnd(e.cur)
	if e.cur >= end && end > start {
		e.cur = end - 1
	}
}

func (e *Editor) resetPending() {
	e.vim.count, e.vim.op, e.vim.opCount, e.vim.g, e.vim.r = 0, "", 0, false, false
}

// normalKey handles a key in vim's normal mode.
func (e *Editor) normalKey(k tea.KeyPressMsg) Outcome {
	text := printable(k)
	if text == "" {
		switch k.Keystroke() {
		case "enter":
			e.resetPending()
			return Submit
		case "left", "backspace":
			text = "h"
		case "right":
			text = "l"
		case "up":
			text = "k"
		case "down":
			text = "j"
		case "esc":
			// A half-typed command is cancelled by Esc; with nothing pending Esc belongs to the caller,
			// which uses it to interrupt or to rewind.
			if e.vim.count != 0 || e.vim.op != "" || e.vim.g || e.vim.r {
				e.resetPending()
				return Handled
			}
			return Unhandled
		case "ctrl+_", "ctrl+/":
			e.Undo()
			e.clampNormal()
			return Handled
		default:
			return Unhandled
		}
	}
	out := e.normalCommand(text)
	e.clampNormal()
	return out
}

func (e *Editor) normalCommand(ch string) Outcome {
	s := &e.vim
	if s.r {
		e.replaceChars(ch, max(s.count, 1))
		e.resetPending()
		return Handled
	}
	if len(ch) == 1 && ch[0] >= '1' && ch[0] <= '9' || ch == "0" && s.count > 0 {
		s.count = s.count*10 + int(ch[0]-'0')
		return Handled
	}
	if s.g {
		s.g = false
		if ch != "g" {
			e.resetPending()
			return Handled
		}
		ch = "gg"
	} else if ch == "g" {
		s.g = true
		return Handled
	}
	n := max(s.count, 1) * max(s.opCount, 1)

	if s.op != "" {
		op := s.op
		if ch == op {
			e.lineOp(op, n)
			e.resetPending()
			return Handled
		}
		from, to, ok := e.operatorRange(op, ch, n)
		e.resetPending()
		if ok {
			e.rangeOp(op, from, to)
		}
		return Handled
	}

	switch ch {
	case "d", "c", "y":
		s.op, s.opCount, s.count = ch, max(s.count, 1), 0
		return Handled
	case "j", "k":
		e.resetPending()
		delta := 1
		if ch == "k" {
			delta = -1
		}
		for i := 0; i < n; i++ {
			if !e.vertical(delta) {
				if i == 0 {
					if delta < 0 {
						return HistoryPrev
					}
					return HistoryNext
				}
				break
			}
		}
		return Handled
	case "x":
		end := min(e.cur+n, e.lineEnd(e.cur))
		e.yankRegister(e.cur, end, false)
		e.cutRange(e.cur, end)
	case "X":
		start := max(e.cur-n, e.lineStart(e.cur))
		e.yankRegister(start, e.cur, false)
		e.cutRange(start, e.cur)
	case "D":
		e.rangeOp("d", e.cur, e.lineEnd(e.cur))
	case "C":
		e.rangeOp("c", e.cur, e.lineEnd(e.cur))
	case "i":
		e.enterInsert(false)
	case "a":
		if e.cur < e.lineEnd(e.cur) {
			e.cur++
		}
		e.enterInsert(false)
	case "I":
		e.cur = e.firstNonBlank(e.cur)
		e.enterInsert(false)
	case "A":
		e.cur = e.lineEnd(e.cur)
		e.enterInsert(false)
	case "o":
		e.save(editOther)
		e.cur = e.lineEnd(e.cur)
		e.insert("\n")
		e.enterInsert(true)
	case "O":
		e.save(editOther)
		e.cur = e.lineStart(e.cur)
		e.insert("\n")
		e.cur--
		e.enterInsert(true)
	case "u":
		for i := 0; i < n; i++ {
			e.Undo()
		}
	case "p", "P":
		e.put(ch == "p", n)
	case "r":
		s.r = true
		return Handled
	case "~":
		e.toggleCase(n)
	default:
		if target, _, ok := e.motion(ch, n); ok {
			e.cur = target
		}
	}
	e.resetPending()
	return Handled
}

// motion is where a motion key moves the cursor, and whether the character it lands on is part of
// an operator's range (e is; w is not).
func (e *Editor) motion(ch string, n int) (int, bool, bool) {
	cur := e.cur
	switch ch {
	case "h":
		return max(cur-n, e.lineStart(cur)), false, true
	case "l":
		end := e.lineEnd(cur)
		return min(cur+n, max(end, e.lineStart(cur))), false, true
	case "w", "W":
		big := ch == "W"
		for i := 0; i < n; i++ {
			cur = e.vimWordForward(cur, big)
		}
		return cur, false, true
	case "b", "B":
		big := ch == "B"
		for i := 0; i < n; i++ {
			cur = e.vimWordBack(cur, big)
		}
		return cur, false, true
	case "e", "E":
		big := ch == "E"
		for i := 0; i < n; i++ {
			cur = e.vimWordEnd(cur, big)
		}
		return cur, true, true
	case "0":
		return e.lineStart(cur), false, true
	case "^":
		return e.firstNonBlank(cur), false, true
	case "$":
		return e.lineEnd(cur), false, true
	case "gg":
		if e.vim.count > 0 {
			return e.lineOffset(e.vim.count - 1), false, true
		}
		return 0, false, true
	case "G":
		if e.vim.count > 0 {
			return e.lineOffset(e.vim.count - 1), false, true
		}
		return e.lineStart(len(e.buf)), false, true
	}
	return cur, false, false
}

// operatorRange is the span an operator with a motion covers.
func (e *Editor) operatorRange(op, ch string, n int) (int, int, bool) {
	cur := e.cur
	if ch == "j" || ch == "k" {
		// dj and dk are line operations over the current line and n lines below or above.
		line := e.lineIndex(cur)
		first, last := line, line+n
		if ch == "k" {
			first, last = line-n, line
		}
		first = max(first, 0)
		last = min(last, e.lineCount()-1)
		e.cur = e.lineOffset(first)
		e.lineOp(op, last-first+1)
		return 0, 0, false
	}
	if op == "c" && (ch == "w" || ch == "W") && cur < len(e.buf) && !isSpace(e.buf[cur]) {
		// cw changes to the end of the word, not to the start of the next one: vim's own exception,
		// and the one every vim user's fingers expect.
		ch = strings.Replace(ch, "w", "e", 1)
		ch = strings.Replace(ch, "W", "E", 1)
	}
	target, inclusive, ok := e.motion(ch, n)
	if !ok {
		return 0, 0, false
	}
	if (ch == "w" || ch == "W") && target > e.lineEnd(cur) && cur < e.lineEnd(cur) {
		// dw on the last word of a line stops at the line's end instead of joining the next line.
		target = e.lineEnd(cur)
	}
	from, to := cur, target
	if to < from {
		from, to = to, from
	} else if inclusive && to < len(e.buf) {
		to++
	}
	return from, to, true
}

func (e *Editor) rangeOp(op string, from, to int) {
	from = clamp(from, 0, len(e.buf))
	to = clamp(to, from, len(e.buf))
	switch op {
	case "y":
		e.yankRegister(from, to, false)
		e.cur = from
	case "d":
		e.yankRegister(from, to, false)
		e.cutRange(from, to)
	case "c":
		e.yankRegister(from, to, false)
		e.save(editOther)
		e.delete(from, to)
		e.cur = from
		e.enterInsert(true)
	}
}

// lineOp is dd, cc or yy over n lines starting at the cursor's line.
func (e *Editor) lineOp(op string, n int) {
	first := e.lineIndex(e.cur)
	last := min(first+n-1, e.lineCount()-1)
	start := e.lineOffset(first)
	end := e.lineEnd(e.lineOffset(last))
	e.vim.register = string(e.buf[start:end])
	e.vim.linewise = true
	switch op {
	case "y":
		return
	case "c":
		e.save(editOther)
		e.delete(start, end)
		e.cur = start
		e.enterInsert(true)
	case "d":
		e.save(editOther)
		cutStart, cutEnd := start, end
		if cutEnd < len(e.buf) {
			cutEnd++
		} else if cutStart > 0 {
			cutStart--
		}
		e.delete(cutStart, cutEnd)
		e.cur = e.firstNonBlank(clamp(cutStart, 0, len(e.buf)))
		if cutStart > 0 && cutStart == len(e.buf) {
			e.cur = e.firstNonBlank(e.lineStart(cutStart))
		}
		e.lastEdit = editOther
	}
}

func (e *Editor) yankRegister(from, to int, linewise bool) {
	if from >= to {
		return
	}
	e.vim.register = string(e.buf[from:to])
	e.vim.linewise = linewise
}

func (e *Editor) cutRange(from, to int) {
	if from >= to {
		return
	}
	e.save(editOther)
	e.delete(from, to)
	e.cur = from
	e.lastEdit = editOther
}

// put is p and P: a line-wise register goes on its own line below or above, anything else after or
// at the cursor, with the cursor left on the last character put.
func (e *Editor) put(after bool, n int) {
	text := e.vim.register
	if text == "" {
		return
	}
	e.save(editOther)
	repeated := strings.Repeat(text, n)
	if e.vim.linewise {
		repeated = strings.TrimSuffix(strings.Repeat(text+"\n", n), "\n")
		if after {
			e.cur = e.lineEnd(e.cur)
			e.insert("\n" + repeated)
			e.cur -= utf8.RuneCountInString(repeated)
		} else {
			e.cur = e.lineStart(e.cur)
			at := e.cur
			e.insert(repeated + "\n")
			e.cur = at
		}
		e.lastEdit = editOther
		return
	}
	if after && e.cur < e.lineEnd(e.cur) {
		e.cur++
	}
	e.insert(repeated)
	e.cur--
	e.lastEdit = editOther
}

func (e *Editor) replaceChars(ch string, n int) {
	end := e.lineEnd(e.cur)
	if e.cur+n > end {
		return // vim refuses an r that would run past the line, rather than doing part of it
	}
	e.save(editOther)
	e.replace(e.cur, e.cur+n, strings.Repeat(ch, n))
	e.cur += n - 1
	e.lastEdit = editOther
}

func (e *Editor) toggleCase(n int) {
	end := min(e.cur+n, e.lineEnd(e.cur))
	if e.cur >= end {
		return
	}
	e.save(editOther)
	for i := e.cur; i < end; i++ {
		r := e.buf[i]
		if unicode.IsUpper(r) {
			e.buf[i] = unicode.ToLower(r)
		} else {
			e.buf[i] = unicode.ToUpper(r)
		}
	}
	e.cur = end
	e.lastEdit = editOther
}

func (e *Editor) firstNonBlank(offset int) int {
	i := e.lineStart(offset)
	end := e.lineEnd(offset)
	for i < end && (e.buf[i] == ' ' || e.buf[i] == '\t') {
		i++
	}
	return i
}

func (e *Editor) lineIndex(offset int) int {
	n := 0
	for _, r := range e.buf[:clamp(offset, 0, len(e.buf))] {
		if r == '\n' {
			n++
		}
	}
	return n
}

func (e *Editor) lineCount() int {
	n := 1
	for _, r := range e.buf {
		if r == '\n' {
			n++
		}
	}
	return n
}

// lineOffset is where the n-th line starts, zero-based, clamped to the last line.
func (e *Editor) lineOffset(n int) int {
	offset := 0
	for line := 0; line < n; line++ {
		end := e.lineEnd(offset)
		if end >= len(e.buf) {
			return e.lineStart(offset)
		}
		offset = end + 1
	}
	return offset
}

// vimClass sorts characters the way vim's word motions do: blanks, word characters, and everything
// else, so `foo.bar` is three words to w and one WORD to W.
func vimClass(r rune, big bool) int {
	if isSpace(r) {
		return 0
	}
	if big || isWordRune(r) {
		return 1
	}
	return 2
}

func (e *Editor) vimWordForward(i int, big bool) int {
	n := len(e.buf)
	if i >= n {
		return n
	}
	if c := vimClass(e.buf[i], big); c != 0 {
		for i < n && vimClass(e.buf[i], big) == c {
			i++
		}
	}
	for i < n && vimClass(e.buf[i], big) == 0 {
		i++
	}
	return i
}

func (e *Editor) vimWordBack(i int, big bool) int {
	i--
	for i > 0 && vimClass(e.buf[i], big) == 0 {
		i--
	}
	if i <= 0 {
		return 0
	}
	c := vimClass(e.buf[i], big)
	for i > 0 && vimClass(e.buf[i-1], big) == c {
		i--
	}
	return i
}

func (e *Editor) vimWordEnd(i int, big bool) int {
	n := len(e.buf)
	i++
	for i < n && vimClass(e.buf[i], big) == 0 {
		i++
	}
	if i >= n {
		return max(n-1, 0)
	}
	c := vimClass(e.buf[i], big)
	for i+1 < n && vimClass(e.buf[i+1], big) == c {
		i++
	}
	return i
}
