package page

import (
	"context"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The console as the agent reads it: what the page wrote to its console, what it threw and did not
// catch, and what the browser said about it (a resource that failed to load, a script it blocked).
// Each tab keeps the last logsKept entries, each at most logTextMax characters; the agent reads the
// ones after a sequence number it keeps, so every call says only what is new.
const (
	logsKept   = 200
	logTextMax = 500
	// logFrames is how many lines of an uncaught error's stack are kept under its message: where it
	// was thrown and what called it, which is what finding the fault needs.
	logFrames = 3
	// LogsMax is the most entries one page.logs returns.
	LogsMax = 200
)

// Log levels, most severe first. debug is Chromium's verbose: framework chatter and the browser's
// own advice, left out unless asked for.
var logLevels = map[string]int{"error": 3, "warning": 2, "info": 1, "debug": 0}

// LogEntry is one line of a tab's console.
type LogEntry struct {
	Seq    int64  `json:"seq"`
	At     int64  `json:"at"`
	Level  string `json:"level"`
	Source string `json:"source"` // console, exception, network, browser, dialog
	Text   string `json:"text"`
	URL    string `json:"url,omitempty"`
	Line   int    `json:"line,omitempty"`
	Count  int    `json:"count,omitempty"` // the same entry repeated in a row, told once with the count
}

type logsKey struct{}

// consoleLog is one tab's console.
type consoleLog struct {
	mu      sync.Mutex
	entries []LogEntry
	seq     int64
	lost    int64 // the newest seq that has been dropped for room
	release bool  // console objects Chromium holds for us wait to be let go (releaseConsole)
	// errors and warnings count every one ever kept, repeats included, so what an action made the
	// page say is a difference of two counts, exact however the lines were folded.
	errors, warnings int
}

func (p *Model) consoleOf(t *browser.Tab, create bool) *consoleLog {
	if c, ok := t.Value(logsKey{}).(*consoleLog); ok {
		return c
	}
	if !create {
		return nil
	}
	c := &consoleLog{}
	t.SetValue(logsKey{}, c)
	return c
}

// add keeps an entry, its credentials cut (RedactText). The same entry again in a row is folded into the last, which takes the new
// sequence number: a page logging in a loop fills one line, not the ring, and a reader who has seen
// the line before is still told it came again.
func (c *consoleLog) add(e LogEntry) {
	e.Text = RedactText(e.Text)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.seq++
	switch e.Level {
	case "error":
		c.errors++
	case "warning":
		c.warnings++
	}
	if n := len(c.entries); n > 0 {
		last := &c.entries[n-1]
		if last.Level == e.Level && last.Source == e.Source && last.Text == e.Text && last.URL == e.URL && last.Line == e.Line {
			last.Count = max(last.Count, 1) + 1
			last.Seq, last.At = c.seq, e.At
			return
		}
	}
	e.Seq = c.seq
	c.entries = append(c.entries, e)
	if len(c.entries) > logsKept {
		c.lost = c.entries[len(c.entries)-logsKept-1].Seq
		c.entries = append([]LogEntry(nil), c.entries[len(c.entries)-logsKept:]...)
	}
}

// LogsResult is page.logs's answer.
type LogsResult struct {
	URL     string     `json:"url"`
	Entries []LogEntry `json:"entries"`
	// Last is the sequence number to read after next time: the last entry returned, or, when none
	// was, where the log stands.
	Last int64 `json:"last"`
	// More counts the entries that matched past limit; the next call with after=last gets them.
	More int `json:"more"`
	// Dropped says entries after `after` were dropped for room before they were read.
	Dropped bool `json:"dropped"`
}

// Logs are the tab's console entries after `after` at level or above, oldest first, at most limit.
// An `after` past the log's end (a daemon restarted under a host that kept its place) reads from
// the start.
func (p *Model) Logs(t *browser.Tab, after int64, level string, limit int) (*LogsResult, error) {
	if level == "" {
		level = "info"
	}
	floor, ok := logLevels[level]
	if !ok {
		return nil, wire.Errorf(wire.CodeInvalidParams, "level is error, warning, info or debug")
	}
	if limit == 0 {
		limit = 100
	}
	if limit < 1 || limit > LogsMax {
		return nil, wire.Errorf(wire.CodeInvalidParams, "limit is 1-%d", LogsMax)
	}
	if after < 0 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "after is a sequence number, 0 or more")
	}
	out := &LogsResult{URL: t.URL(), Entries: []LogEntry{}}
	c := p.consoleOf(t, false)
	if c == nil {
		return out, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if after > c.seq {
		after = 0
	}
	out.Last = c.seq
	out.Dropped = c.lost > after
	for _, e := range c.entries {
		if e.Seq <= after || logLevels[e.Level] < floor {
			continue
		}
		if len(out.Entries) == limit {
			out.More++
			continue
		}
		out.Entries = append(out.Entries, e)
	}
	if out.More > 0 {
		out.Last = out.Entries[len(out.Entries)-1].Seq
	}
	return out, nil
}

// LogMark is where the tab's console stands, for LogCounts.
type LogMark struct{ errors, warnings int }

// Mark is the tab's console now.
func (p *Model) Mark(t *browser.Tab) LogMark {
	c := p.consoleOf(t, false)
	if c == nil {
		return LogMark{}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return LogMark{c.errors, c.warnings}
}

// LogCounts are the errors and warnings the tab's console took since mark, repeats included: what
// an action or a navigation made the page say, told in a word with its result.
func (p *Model) LogCounts(t *browser.Tab, mark LogMark) (errors, warnings int) {
	now := p.Mark(t)
	return max(0, now.errors-mark.errors), max(0, now.warnings-mark.warnings)
}

// logEvent keeps what the console, the page's uncaught errors and the browser's own log say.
func (p *Model) logEvent(t *browser.Tab, e cdp.Event) {
	switch e.Method {
	case "Runtime.consoleAPICalled":
		var r struct {
			Type       string         `json:"type"`
			Args       []remoteObject `json:"args"`
			Timestamp  float64        `json:"timestamp"`
			StackTrace *stackTrace    `json:"stackTrace"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		if r.Type == "clear" || r.Type == "endGroup" || r.Type == "startGroupCollapsed" && len(r.Args) == 0 {
			return
		}
		entry := LogEntry{At: stamp(r.Timestamp), Level: consoleLevel(r.Type), Source: "console", Text: clipText(consoleText(r.Args), logTextMax)}
		if f := r.StackTrace.first(); f != nil {
			entry.URL, entry.Line = f.URL, f.LineNumber+1
		}
		c := p.consoleOf(t, true)
		c.add(entry)
		p.releaseConsole(t, c)
	case "Runtime.exceptionThrown":
		var r struct {
			Timestamp float64 `json:"timestamp"`
			Details   struct {
				Text       string        `json:"text"`
				URL        string        `json:"url"`
				LineNumber int           `json:"lineNumber"`
				Exception  *remoteObject `json:"exception"`
				StackTrace *stackTrace   `json:"stackTrace"`
			} `json:"exceptionDetails"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		d := r.Details
		text := d.Text
		if d.Exception != nil {
			desc := d.Exception.Description
			if desc == "" {
				desc = d.Exception.render(0)
			}
			// "Uncaught" and "Uncaught (in promise)" before what was thrown, as the console says it.
			text = strings.TrimSpace(d.Text + " " + stackHead(desc, logFrames))
		}
		entry := LogEntry{At: stamp(r.Timestamp), Level: "error", Source: "exception", Text: clipText(text, logTextMax), URL: d.URL}
		if d.URL != "" || d.LineNumber > 0 {
			entry.Line = d.LineNumber + 1
		}
		if f := d.StackTrace.first(); f != nil && entry.URL == "" {
			entry.URL, entry.Line = f.URL, f.LineNumber+1
		}
		c := p.consoleOf(t, true)
		c.add(entry)
		p.releaseConsole(t, c)
	case "Log.entryAdded":
		var r struct {
			Entry struct {
				Source     string  `json:"source"`
				Level      string  `json:"level"`
				Text       string  `json:"text"`
				URL        string  `json:"url"`
				LineNumber int     `json:"lineNumber"`
				Timestamp  float64 `json:"timestamp"`
			} `json:"entry"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		l := r.Entry
		if l.Source == "network" && strings.HasSuffix(strings.SplitN(l.URL, "?", 2)[0], "/favicon.ico") {
			// The browser asks every site for its icon on its own; a site without one is not at fault.
			return
		}
		source := "browser"
		if l.Source == "network" {
			source = "network"
		}
		level := l.Level
		if _, known := logLevels[level]; !known {
			level = "debug" // verbose: the browser's advice about the page, seldom its fault
		}
		entry := LogEntry{At: stamp(l.Timestamp), Level: level, Source: source, Text: clipText(l.Text, logTextMax), URL: RedactURL(l.URL)}
		if l.LineNumber > 0 {
			entry.Line = l.LineNumber + 1
		}
		p.consoleOf(t, true).add(entry)
	case "Page.javascriptDialogOpening":
		// A dialog is told in the console too, whoever answers it: it is part of what the page did.
		var r struct {
			Type    string `json:"type"`
			Message string `json:"message"`
		}
		if json.Unmarshal(e.Params, &r) != nil {
			return
		}
		how := "waiting for an answer"
		if t.Dialog() == nil {
			how = "accepted by the browser"
		}
		p.consoleOf(t, true).add(LogEntry{At: time.Now().UnixMilli(), Level: "info", Source: "dialog",
			Text: clipText(r.Type+" ("+how+"): "+r.Message, logTextMax)})
	}
}

// releaseConsole lets Chromium drop the objects it keeps for the console messages it sent: every
// argument of a console call reaches the daemon as a handle in the "console" group, and a handle
// keeps its object alive, so a page logging large objects would otherwise grow for as long as it
// runs. At most once a second a tab: the daemon has taken what it keeps (the words) by then.
func (p *Model) releaseConsole(t *browser.Tab, c *consoleLog) {
	c.mu.Lock()
	pending := c.release
	c.release = true
	c.mu.Unlock()
	if pending {
		return
	}
	time.AfterFunc(time.Second, func() {
		c.mu.Lock()
		c.release = false
		c.mu.Unlock()
		if t.Closed() {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = t.Call(ctx, "Runtime.releaseObjectGroup", map[string]any{"objectGroup": "console"}, nil)
	})
}

func consoleLevel(kind string) string {
	switch kind {
	case "error", "assert":
		return "error"
	case "warning":
		return "warning"
	case "debug":
		return "debug"
	}
	return "info"
}

// stamp turns a CDP timestamp (milliseconds since the epoch, as Runtime and Log give it) into the
// entry's, or now when there is none.
func stamp(ms float64) int64 {
	if ms <= 0 || math.IsNaN(ms) {
		return time.Now().UnixMilli()
	}
	return int64(ms)
}

type stackTrace struct {
	CallFrames []struct {
		FunctionName string `json:"functionName"`
		URL          string `json:"url"`
		LineNumber   int    `json:"lineNumber"`
	} `json:"callFrames"`
}

type frameAt struct {
	URL        string
	LineNumber int
}

func (s *stackTrace) first() *frameAt {
	if s == nil {
		return nil
	}
	for _, f := range s.CallFrames {
		if f.URL != "" {
			return &frameAt{URL: RedactURL(f.URL), LineNumber: f.LineNumber}
		}
	}
	return nil
}

// stackHead is an error's message and the first frames of its stack.
func stackHead(desc string, frames int) string {
	lines := strings.Split(desc, "\n")
	var out []string
	kept := 0
	for i, l := range lines {
		l = strings.TrimSpace(l)
		if l == "" {
			continue
		}
		if i > 0 && strings.HasPrefix(l, "at ") {
			if kept == frames {
				continue
			}
			kept++
		}
		out = append(out, l)
	}
	return strings.Join(out, "\n  ")
}

// remoteObject is what CDP gives for a value the page logged: a primitive's value, or an object's
// description and, one level deep, a preview of its properties. Nothing here runs the page's code:
// the preview is Chromium's, made without calling a getter.
type remoteObject struct {
	Type        string          `json:"type"`
	Subtype     string          `json:"subtype"`
	ClassName   string          `json:"className"`
	Value       json.RawMessage `json:"value"`
	Unserial    string          `json:"unserializableValue"`
	Description string          `json:"description"`
	Preview     *struct {
		Overflow   bool `json:"overflow"`
		Properties []struct {
			Name    string `json:"name"`
			Type    string `json:"type"`
			Subtype string `json:"subtype"`
			Value   string `json:"value"`
		} `json:"properties"`
		Entries []json.RawMessage `json:"entries"`
	} `json:"preview"`
}

// render is a logged value in words, as a console would print it, its properties one level deep.
func (o *remoteObject) render(depth int) string {
	switch o.Type {
	case "undefined":
		return "undefined"
	case "string":
		var s string
		_ = json.Unmarshal(o.Value, &s)
		if depth > 0 {
			return strconv.Quote(s)
		}
		return s
	case "number", "boolean", "bigint":
		if o.Unserial != "" {
			return o.Unserial
		}
		if len(o.Value) > 0 {
			return string(o.Value)
		}
		return o.Description
	case "symbol", "function":
		return o.Description
	}
	if o.Subtype == "null" {
		return "null"
	}
	if o.Subtype == "error" {
		return stackHead(o.Description, logFrames)
	}
	if o.Preview == nil || depth > 0 {
		return o.Description
	}
	parts := make([]string, 0, len(o.Preview.Properties))
	for _, pr := range o.Preview.Properties {
		v := pr.Value
		switch {
		case pr.Type == "string":
			v = strconv.Quote(v)
		case pr.Type == "accessor":
			v = "(getter)"
		}
		if o.Subtype == "array" {
			parts = append(parts, v)
		} else {
			parts = append(parts, pr.Name+": "+v)
		}
	}
	if o.Preview.Overflow {
		parts = append(parts, "…")
	}
	if o.Subtype == "array" {
		return o.Description + " [" + strings.Join(parts, ", ") + "]"
	}
	prefix := ""
	if o.Description != "" && o.Description != "Object" {
		prefix = o.Description + " "
	}
	return prefix + "{" + strings.Join(parts, ", ") + "}"
}

// consoleText is a console call's arguments as one line: the first one's %s, %d, %i, %f, %o, %O and
// %c filled from the rest, as the console does, and the others after it.
func consoleText(args []remoteObject) string {
	if len(args) == 0 {
		return ""
	}
	rest := args[1:]
	first := args[0].render(0)
	if args[0].Type == "string" && strings.Contains(first, "%") {
		var b strings.Builder
		for i := 0; i < len(first); i++ {
			ch := first[i]
			if ch != '%' || i+1 == len(first) {
				b.WriteByte(ch)
				continue
			}
			spec := first[i+1]
			switch spec {
			case 's', 'd', 'i', 'f', 'o', 'O':
				if len(rest) > 0 {
					b.WriteString(rest[0].render(0))
					rest = rest[1:]
				}
				i++
			case 'c':
				if len(rest) > 0 {
					rest = rest[1:] // a style for the console's text; nothing to read
				}
				i++
			case '%':
				b.WriteByte('%')
				i++
			default:
				b.WriteByte(ch)
			}
		}
		first = b.String()
	}
	parts := []string{first}
	for i := range rest {
		parts = append(parts, rest[i].render(0))
	}
	return strings.Join(parts, " ")
}

// clipText cuts s to at most n characters, saying it was cut.
func clipText(s string, n int) string {
	s = strings.TrimSpace(s)
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n-1]) + "…"
}
