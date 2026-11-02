package textedit

import (
	"unicode"

	tea "charm.land/bubbletea/v2"
)

// HandleKey applies one key press and reports what it meant.
func (e *Editor) HandleKey(k tea.KeyPressMsg) Outcome {
	stroke := k.Keystroke()
	vertical := stroke == "up" || stroke == "down"
	if e.vimOn && e.vim.mode == vimNormal {
		text := printable(k)
		vertical = vertical || text == "j" || text == "k"
	}
	if !vertical {
		e.goal = -1
	}
	var out Outcome
	if e.vimOn && e.vim.mode == vimNormal {
		out = e.normalKey(k)
	} else {
		out = e.insertKey(k)
	}
	e.lastKey = stroke
	return out
}

// printable is the text a key types, or "" for a key that types nothing. A key held with ctrl or alt
// types nothing even when the terminal reports text for it: those are commands, and inserting the
// letter would turn an unbound shortcut into a stray character in the prompt.
func printable(k tea.KeyPressMsg) string {
	if k.Text == "" || k.Mod.Contains(tea.ModCtrl) || k.Mod.Contains(tea.ModAlt) || k.Mod.Contains(tea.ModSuper) {
		return ""
	}
	return k.Text
}

// insertKey is the ordinary editing mode, which is also vim's insert mode.
func (e *Editor) insertKey(k tea.KeyPressMsg) Outcome {
	stroke := k.Keystroke()
	previousAction := e.lastAction
	e.lastAction = actOther
	switch stroke {
	case "enter":
		if e.cur > 0 && e.buf[e.cur-1] == '\\' {
			// A trailing backslash continues the line, as in a shell: the one way to break a line that
			// works in every terminal, including those that report shift+enter as plain enter.
			e.save(editOther)
			e.replace(e.cur-1, e.cur, "\n")
			e.lastEdit = editOther
			return Handled
		}
		return Submit
	case "shift+enter", "alt+enter", "ctrl+j":
		e.save(editOther)
		e.insert("\n")
		e.lastEdit = editOther
		return Handled
	case "esc":
		if e.vimOn {
			e.enterNormal()
			return Handled
		}
		return Unhandled
	case "backspace", "ctrl+h":
		e.backspace()
		return Handled
	case "delete":
		e.deleteForward()
		return Handled
	case "ctrl+d":
		if len(e.buf) == 0 {
			return Unhandled
		}
		e.deleteForward()
		return Handled
	case "left", "ctrl+b":
		e.moveLeft()
		return Handled
	case "right", "ctrl+f":
		e.moveRight()
		return Handled
	case "home":
		e.breakCoalescing()
		e.cur = e.lineStart(e.cur)
		return Handled
	case "end":
		e.breakCoalescing()
		e.cur = e.lineEnd(e.cur)
		return Handled
	case "ctrl+a":
		e.breakCoalescing()
		start := e.lineStart(e.cur)
		// A second ctrl+a at the start of a line goes to the start of the prompt: the line start is
		// where the first press already is, so the repeat has nothing else useful to mean.
		if e.cur == start && e.lastKey == "ctrl+a" {
			start = 0
		}
		e.cur = start
		return Handled
	case "ctrl+e":
		e.breakCoalescing()
		end := e.lineEnd(e.cur)
		if e.cur == end && e.lastKey == "ctrl+e" {
			end = len(e.buf)
		}
		e.cur = end
		return Handled
	case "alt+b", "ctrl+left", "alt+left":
		e.breakCoalescing()
		e.cur = e.snapOut(e.wordBack(e.cur), false)
		return Handled
	case "alt+f", "ctrl+right", "alt+right":
		e.breakCoalescing()
		e.cur = e.snapOut(e.wordForward(e.cur), true)
		return Handled
	case "up", "down":
		e.breakCoalescing()
		delta := -1
		if stroke == "down" {
			delta = 1
		}
		if !e.vertical(delta) {
			if delta < 0 {
				return HistoryPrev
			}
			return HistoryNext
		}
		return Handled
	case "ctrl+k":
		e.lastAction = previousAction
		end := e.lineEnd(e.cur)
		if end == e.cur && end < len(e.buf) {
			end++ // at the end of a line ctrl+k takes the newline, joining the next line, as readline does
		}
		e.kill(e.cur, end, false)
		e.lastAction = actKill
		return Handled
	case "ctrl+u":
		e.lastAction = previousAction
		start := e.lineStart(e.cur)
		if start == e.cur && start > 0 {
			start--
		}
		e.kill(start, e.cur, true)
		e.lastAction = actKill
		return Handled
	case "ctrl+w":
		e.lastAction = previousAction
		start := e.cur
		for start > 0 && isSpace(e.buf[start-1]) {
			start--
		}
		for start > 0 && !isSpace(e.buf[start-1]) {
			start--
		}
		e.kill(e.snapOut(start, false), e.cur, true)
		e.lastAction = actKill
		return Handled
	case "alt+backspace", "ctrl+backspace":
		e.lastAction = previousAction
		e.kill(e.snapOut(e.wordBack(e.cur), false), e.cur, true)
		e.lastAction = actKill
		return Handled
	case "alt+d":
		e.lastAction = previousAction
		e.kill(e.cur, e.snapOut(e.wordForward(e.cur), true), false)
		e.lastAction = actKill
		return Handled
	case "ctrl+y":
		e.yank()
		return Handled
	case "alt+y":
		e.lastAction = previousAction
		e.yankPop()
		return Handled
	case "ctrl+_", "ctrl+/":
		e.Undo()
		return Handled
	}
	if text := printable(k); text != "" {
		e.save(editTyping)
		e.insert(text)
		return Handled
	}
	return Unhandled
}

func (e *Editor) backspace() {
	if e.cur == 0 {
		return
	}
	if t, ok := e.tokenEndingAt(e.cur); ok {
		e.save(editOther)
		e.delete(t.start, t.end)
		e.lastEdit = editOther
		return
	}
	// Deleting typed characters is part of typing: a word typed, half rubbed out and retyped
	// undoes as one step.
	e.save(editTyping)
	e.delete(e.cur-1, e.cur)
}

func (e *Editor) deleteForward() {
	if e.cur >= len(e.buf) {
		return
	}
	if t, ok := e.tokenStartingAt(e.cur); ok {
		e.save(editOther)
		e.delete(t.start, t.end)
		e.lastEdit = editOther
		return
	}
	e.save(editTyping)
	e.delete(e.cur, e.cur+1)
}

func (e *Editor) moveLeft() {
	e.breakCoalescing()
	if e.cur == 0 {
		return
	}
	if t, ok := e.tokenEndingAt(e.cur); ok {
		e.cur = t.start
		return
	}
	e.cur--
}

func (e *Editor) moveRight() {
	e.breakCoalescing()
	if e.cur >= len(e.buf) {
		return
	}
	if t, ok := e.tokenStartingAt(e.cur); ok {
		e.cur = t.end
		return
	}
	e.cur++
}

func isWordRune(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) }

// wordForward is where emacs' forward-word lands: past any separators, then to the end of the word.
func (e *Editor) wordForward(offset int) int {
	for offset < len(e.buf) && !isWordRune(e.buf[offset]) {
		offset++
	}
	for offset < len(e.buf) && isWordRune(e.buf[offset]) {
		offset++
	}
	return offset
}

// wordBack is where emacs' backward-word lands: back past separators, then to the start of the word.
func (e *Editor) wordBack(offset int) int {
	for offset > 0 && !isWordRune(e.buf[offset-1]) {
		offset--
	}
	for offset > 0 && isWordRune(e.buf[offset-1]) {
		offset--
	}
	return offset
}
