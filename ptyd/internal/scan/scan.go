// Package scan reads a terminal's raw output before anything else does. It does two jobs in one
// pass, because both need the same tokenizer:
//
//   - It clamps what no consumer can be trusted with. The same bytes go to the daemon's emulator and
//     to every browser's xterm.js, and several emulators allocate or loop in proportion to a numeric
//     parameter or buffer an unterminated string without limit (a `REP` of a billion took one to
//     2 GB in nine seconds; an endless OSC took another to 435 MB). So every numeric CSI parameter
//     is capped at MaxParam, a CSI carries at most MaxFields parameters, an OSC, DCS, APC, PM or SOS
//     payload longer than MaxString is dropped whole, and a window title is cut to MaxTitle.
//   - It finds the marks the daemon reports: titles, the working directory, shell-integration
//     prompts, notifications, progress, the bell, mode changes, kitty keyboard stack operations, and
//     the queries a terminal must answer. It sees them in the raw stream, before an emulator has
//     swallowed them.
//
// The scanner is stateful: a sequence split across two reads is held until it is complete, so its
// output is identical however the input was chunked.
package scan

import (
	"bytes"
	"strconv"
	"unicode/utf8"
)

// The clamps. They are part of the daemon's contract with every consumer of its output.
const (
	MaxParam  = 10000    // the largest numeric CSI parameter passed on
	MaxFields = 32       // parameters and sub-parameters in one CSI; the rest are dropped
	MaxString = 64 << 10 // the longest OSC/DCS/APC/PM/SOS payload; a longer one is dropped whole
	MaxTitle  = 4 << 10  // the longest title (OSC 0, 1, 2), cut at a character boundary
	maxCSI    = 1024     // a CSI longer than this is garbage by construction and is dropped
)

// Kind is what a mark reports.
type Kind uint8

const (
	KindTitle    Kind = iota + 1 // OSC 0 or 2: Text
	KindCwd                      // OSC 7: Text is the decoded path
	KindPrompt                   // OSC 133: Letter, Exit, Params
	KindVscode                   // OSC 633: Letter, Text (the command line for E), Params
	KindNotify                   // OSC 9 (not a ConEmu subcommand) or OSC 777;notify: Title, Text
	KindProgress                 // OSC 9;4: State, Value
	KindBell                     // BEL outside any sequence
	KindMode                     // a DEC private mode set or reset: Mode, Set
	KindKitty                    // kitty keyboard stack: Op ('>' push, '<' pop, '=' set), Flags, Value
	KindReset                    // RIS (Letter 'c') or DECSTR (Letter 'p')
	KindQuery                    // a query that expects an answer: Query, Mode, Private, Text, Raw
)

// Query names the kind of a KindQuery mark.
type Query uint8

const (
	QueryDA1       Query = iota + 1 // CSI c
	QueryDA2                        // CSI > c
	QueryXTVERSION                  // CSI > q
	QueryDSR                        // CSI n: Mode is the request (5 status, 6 cursor), Private for CSI ? n
	QueryDECRQM                     // CSI ? Ps $ p or CSI Ps $ p: Mode, Private
	QueryKitty                      // CSI ? u
	QueryDECRQSS                    // DCS $ q Pt ST: Text is Pt
	QueryOSCColor                   // OSC 4, 10, 11 or 12 where every slot is "?": Mode, Value slots, Text the payload
	QueryXTWINOPS                   // CSI Ps t for a report (11, 13-16, 18-21): Mode is Ps
)

// windowReports are the XTWINOPS operations that ask for a reply rather than change something.
var windowReports = map[int]bool{11: true, 13: true, 14: true, 15: true, 16: true, 18: true, 19: true, 20: true, 21: true}

// Mark is one thing found in the stream.
type Mark struct {
	// Offset is where in this call's output the mark takes effect: the byte just after the sequence.
	Offset int
	Kind   Kind

	Text    string
	Title   string
	Letter  byte
	HasExit bool
	Exit    int
	Params  map[string]string
	Mode    int
	Set     bool
	Private bool
	State   int
	Value   int
	Op      byte
	Flags   int
	Query   Query
	Raw     []byte
}

type state uint8

const (
	ground   state = iota
	escape         // after ESC
	escInter       // ESC and intermediates, passed straight through
	csi            // inside a CSI, buffered in pend
	csiDrop        // inside a CSI that is being dropped
	str            // inside an OSC/DCS/APC/PM/SOS, buffered in pend
	strEsc         // ESC seen inside a string: ST or the start of something else
)

// Scanner is the stateful clamp and mark finder for one terminal. It is not safe for concurrent use.
type Scanner struct {
	st    state
	strip bool // output only plain text (see Strip)

	// A 0xC2 byte waiting for the next one: C2 80..9F is a C1 control encoded in UTF-8, which
	// xterm.js treats as the control itself (C2 9B is a CSI).
	c2 bool

	pend []byte // the sequence being assembled

	// CSI parameter state.
	fields     int  // parameters and sub-parameters seen
	fieldStart int  // index in pend where the current parameter's digits start
	fieldVal   int  // its value, saturated at MaxParam+1
	fieldLen   int  // its digit count
	dropFields bool // past MaxFields: parameter bytes are dropped until the final byte

	// String state.
	strKind  byte // ']' OSC, 'P' DCS, 'X' SOS, '^' PM, '_' APC
	introLen int  // length of the introducer in pend (2 for ESC ], 2 for C2 9D)
	strLen   int  // payload bytes seen, including dropped ones
	strDrop  bool // over MaxString: the whole string is being dropped

	cr bool // strip mode: a CR waiting to see whether an LF follows

	out   []byte
	marks []Mark
}

// New returns a scanner in the ground state.
func New() *Scanner { return &Scanner{} }

// Scan filters chunk through the clamps and returns the bytes to pass on and the marks found, with
// offsets into that output. Both slices belong to the scanner and are valid until the next call.
// A sequence that is incomplete at the end of chunk is held back and appears in a later call's
// output.
func (s *Scanner) Scan(chunk []byte) ([]byte, []Mark) {
	s.out = s.out[:0]
	s.marks = s.marks[:0]
	i := 0
	for i < len(chunk) {
		if s.st == ground && !s.c2 && !s.cr {
			// The fast path: a run of printable text and UTF-8 goes straight through.
			j := i
			for j < len(chunk) {
				b := chunk[j]
				if b < 0x20 || b == 0x7f || b == 0xc2 {
					break
				}
				j++
			}
			if j > i {
				s.out = append(s.out, chunk[i:j]...)
				i = j
				continue
			}
		}
		s.step(chunk[i])
		i++
	}
	if len(s.pend) == 0 && cap(s.pend) > 4<<10 {
		// A long string came and went; do not keep its buffer for the life of the terminal.
		s.pend = nil
	}
	return s.out, s.marks
}

// Strip returns the plain text of p: sequences removed, CR LF turned into LF, a line redrawn after a
// lone CR kept at its last drawing, tabs and line feeds kept, every other control dropped.
// It is for agents reading output, so it errs on the side of readable rather than exact.
func Strip(p []byte) []byte {
	s := &Scanner{strip: true}
	out, _ := s.Scan(p)
	out = append([]byte(nil), out...)
	if s.cr {
		out = append(out, '\n')
	}
	return out
}

// emit passes bytes on (ground text and complete sequences).
func (s *Scanner) emit(p ...byte) {
	if !s.strip {
		s.out = append(s.out, p...)
	}
}

func (s *Scanner) emitText(b byte) {
	if !s.strip {
		s.out = append(s.out, b)
		return
	}
	if s.cr {
		s.cr = false
		if b == '\n' {
			s.out = append(s.out, '\n')
			return
		}
		// A lone carriage return is a redraw: a progress bar or spinner draws its line again in
		// place. Turning it into a line feed handed the agent every frame as its own line, a few
		// hundred lines of percentages for one download, so the line is dropped back to its start
		// and only the last drawing is kept, as the terminal itself would show it.
		s.out = s.out[:bytes.LastIndexByte(s.out, '\n')+1]
	}
	switch {
	case b == '\r':
		s.cr = true
	case b == '\n' || b == '\t' || b >= 0x20 && b != 0x7f:
		s.out = append(s.out, b)
	}
}

func (s *Scanner) mark(m Mark) {
	if s.strip {
		return
	}
	m.Offset = len(s.out)
	s.marks = append(s.marks, m)
}

// step advances the state machine by one byte.
func (s *Scanner) step(b byte) {
	if s.c2 {
		s.c2 = false
		if b >= 0x80 && b <= 0x9f {
			s.c1(b)
			return
		}
		if b >= 0xa0 && b <= 0xbf {
			// An ordinary two-byte character.
			s.byteIn(0xc2)
			s.byteIn(b)
			return
		}
		// A lone lead byte. Every decoder turns it into U+FFFD, so pass that instead: left as it is,
		// a sequence dropped after it could bring it next to a raw 0x80..0x9F byte, and the two would
		// decode as a C1 control nobody scanned (a CSI whose parameter escaped the clamp).
		for _, r := range []byte("\uFFFD") {
			s.byteIn(r)
		}
		s.step(b)
		return
	}
	if b == 0xc2 {
		s.c2 = true
		return
	}
	s.byteIn(b)
}

// c1 handles a C1 control that arrived as C2 xx. The ones that start or end a sequence act from any
// state, as they do in xterm.js; the rest are passed through as they came.
func (s *Scanner) c1(b byte) {
	switch b {
	case 0x9b, 0x9d, 0x90, 0x98, 0x9e, 0x9f: // CSI, OSC, DCS, SOS, PM, APC
		s.abandon()
		s.pend = append(s.pend[:0], 0xc2, b)
		switch b {
		case 0x9b:
			s.startCSI()
		case 0x9d:
			s.startString(']', 2)
		case 0x90:
			s.startString('P', 2)
		case 0x98:
			s.startString('X', 2)
		case 0x9e:
			s.startString('^', 2)
		case 0x9f:
			s.startString('_', 2)
		}
	case 0x9c: // ST
		if s.st == str || s.st == strEsc {
			s.finishString([]byte{0xc2, 0x9c})
			return
		}
		s.abandon()
		s.emit(0xc2, b)
	default:
		if s.st == str || s.st == strEsc {
			s.strByte(0xc2)
			s.strByte(b)
			return
		}
		if s.st == csi {
			s.csiByte(0xc2) // drops the sequence
			return
		}
		s.abandon()
		s.emit(0xc2, b)
	}
}

// abandon ends whatever sequence is in progress because another one starts. An unfinished ESC or
// CSI is dropped: a parser would abort it anyway, and passing half of one on is how a clamp is
// bypassed — if the sequence that interrupted it is then dropped too, the next digits in the stream
// continue the half a parser is still holding. A string is passed on as complete, with an explicit
// terminator for the same reason (xterm.js dispatches an OSC that another sequence interrupts).
func (s *Scanner) abandon() {
	switch s.st {
	case str, strEsc:
		if !s.strDrop {
			s.finishString(nil)
			return
		}
	}
	s.pend = s.pend[:0]
	s.st = ground
}

func (s *Scanner) byteIn(b byte) {
	switch s.st {
	case ground:
		switch b {
		case 0x1b:
			s.st = escape
		case 0x07:
			s.emitText(b)
			s.mark(Mark{Kind: KindBell})
		default:
			s.emitText(b)
		}

	case escape:
		switch {
		case b == '[':
			s.pend = append(s.pend[:0], 0x1b, b)
			s.startCSI()
		case b == ']' || b == 'P' || b == 'X' || b == '^' || b == '_':
			s.pend = append(s.pend[:0], 0x1b, b)
			s.startString(b, 2)
		case b == 0x1b:
			// ESC ESC: the first one is abandoned, and dropped, the second starts over. A lone ESC
			// passed on would wait in a parser for whatever comes next, and if what comes next is
			// dropped here, that is a '[' the scanner never saw as a CSI.
		case b == 0x18 || b == 0x1a:
			s.emit(b)
			s.st = ground
		case b < 0x20:
			// A C0 control inside an escape is executed and the escape continues.
			s.emitRawControl(b)
		case b >= 0x20 && b <= 0x2f:
			s.emit(0x1b, b)
			s.st = escInter
		default:
			s.emit(0x1b, b)
			s.st = ground
			switch b {
			case 'c':
				s.mark(Mark{Kind: KindReset, Letter: 'c'})
			case '=':
				s.mark(Mark{Kind: KindMode, Mode: 66, Set: true, Private: true})
			case '>':
				s.mark(Mark{Kind: KindMode, Mode: 66, Set: false, Private: true})
			}
		}

	case escInter:
		switch {
		case b == 0x1b:
			s.st = escape
		case b == 0x18 || b == 0x1a:
			s.emit(b)
			s.st = ground
		default:
			s.emit(b)
			if b >= 0x30 && b <= 0x7e {
				s.st = ground
			}
		}

	case csi:
		s.csiByte(b)

	case csiDrop:
		switch {
		case b == 0x1b:
			s.st = escape
		case b == 0x18 || b == 0x1a:
			s.st = ground
		case b >= 0x40 && b <= 0x7e:
			s.st = ground
		}

	case str:
		s.strByte(b)

	case strEsc:
		if b == '\\' {
			s.finishString([]byte{0x1b, '\\'})
			return
		}
		// ESC followed by anything else ends the string and starts a new escape with that byte.
		s.finishString(nil)
		s.st = escape
		s.byteIn(b)
	}
}

// emitRawControl passes a C0 control through from inside an escape.
func (s *Scanner) emitRawControl(b byte) {
	if s.strip {
		s.emitText(b)
		return
	}
	s.out = append(s.out, b)
}

func (s *Scanner) startCSI() {
	s.st = csi
	s.fields = 1
	s.fieldStart = len(s.pend)
	s.fieldVal, s.fieldLen = 0, 0
	s.dropFields = false
}

func (s *Scanner) csiByte(b byte) {
	switch {
	case b >= '0' && b <= '9':
		if s.dropFields {
			return
		}
		s.fieldLen++
		if s.fieldVal <= MaxParam {
			s.fieldVal = s.fieldVal*10 + int(b-'0')
		}
		if s.fieldLen <= 5 && s.fieldVal <= MaxParam {
			s.pend = append(s.pend, b)
			return
		}
		// Too long or too large: rewrite the parameter as its clamped value. Leading zeros are
		// dropped, which changes nothing a parser sees.
		s.pend = strconv.AppendInt(s.pend[:s.fieldStart], int64(min(s.fieldVal, MaxParam)), 10)
	case b == ';' || b == ':':
		if s.dropFields {
			return
		}
		s.fields++
		if s.fields > MaxFields {
			s.dropFields = true
			return
		}
		s.pend = append(s.pend, b)
		s.fieldStart = len(s.pend)
		s.fieldVal, s.fieldLen = 0, 0
	case b >= 0x40 && b <= 0x7e:
		s.pend = append(s.pend, b)
		s.finishCSI()
	case b == 0x1b:
		// Interrupted: dropped, as in abandon.
		s.pend = s.pend[:0]
		s.st = escape
	case b == 0x18 || b == 0x1a:
		// Cancelled: the sequence is dropped and the cancel passed on, which is what a parser that had
		// seen the whole of it would end up with.
		s.pend = s.pend[:0]
		s.emit(b)
		s.st = ground
	case b >= 0x20 && b <= 0x2f || b >= 0x3c && b <= 0x3f:
		// Private markers and intermediates. Digits after them make the sequence invalid, and
		// parsers ignore it, so they start a new field only for the clamp's bookkeeping.
		s.pend = append(s.pend, b)
		s.fieldStart = len(s.pend)
		s.fieldVal, s.fieldLen = 0, 0
		if len(s.pend) > maxCSI {
			s.pend = s.pend[:0]
			s.st = csiDrop
		}
	case b < 0x20:
		// A C0 control inside a CSI is executed on the spot and the sequence continues around it.
		// It is passed on now, ahead of the sequence it interrupted: the effect is the same, and the
		// digits on either side of it stay one parameter for the clamp. Keeping it in place would
		// let `CSI 99 LF 9999 b` through as the 999999 a parser reads.
		s.emitRawControl(b)
	case b == 0x7f:
		// DEL is ignored inside a sequence.
	default:
		// Anything else inside a CSI (a C1 control, a non-ASCII byte) makes it garbage: drop it.
		s.pend = s.pend[:0]
		s.st = csiDrop
	}
}

func (s *Scanner) finishCSI() {
	seq := s.pend
	s.emit(seq...)
	s.st = ground
	if !s.strip {
		s.csiMarks(seq)
	}
	s.pend = s.pend[:0]
}

// parsedCSI is a complete CSI split into its parts, in fixed arrays: this runs for every CSI a
// terminal prints that could be a mark, and allocating per sequence showed in profiles.
type parsedCSI struct {
	prefix byte // '?', '>', '<', '=' or 0
	params [MaxFields]int
	n      int
	inter  byte // the intermediate, when there is exactly one
	ninter int
	final  byte
}

// param returns parameter i, or def when it is missing or empty (-1).
func (p *parsedCSI) param(i, def int) int {
	if i < p.n && p.params[i] >= 0 {
		return p.params[i]
	}
	return def
}

// noInter reports a sequence without intermediates; hasInter one with exactly c.
func (p *parsedCSI) noInter() bool        { return p.ninter == 0 }
func (p *parsedCSI) hasInter(c byte) bool { return p.ninter == 1 && p.inter == c }

func parseCSI(seq []byte) (p parsedCSI, ok bool) {
	body := seq[2:] // after ESC [ or C2 9B
	p.final = body[len(body)-1]
	body = body[:len(body)-1]
	if len(body) > 0 && body[0] >= 0x3c && body[0] <= 0x3f {
		p.prefix = body[0]
		body = body[1:]
	}
	cur, have, sub := 0, false, false
	push := func() {
		if p.n < MaxFields {
			if have {
				p.params[p.n] = cur
			} else {
				p.params[p.n] = -1
			}
			p.n++
		}
	}
	for _, b := range body {
		switch {
		case b >= '0' && b <= '9':
			if !sub && cur <= MaxParam {
				cur, have = cur*10+int(b-'0'), true
			}
		case b == ';':
			push()
			cur, have, sub = 0, false, false
		case b == ':':
			// Sub-parameters matter to nothing the daemon reports; the main value is kept.
			sub = true
		case b >= 0x20 && b <= 0x2f:
			p.inter = b
			p.ninter++
		default:
			return p, false
		}
	}
	if have || p.n > 0 {
		push()
	}
	return p, true
}

// markFinals are the final bytes of every CSI the daemon reports. Anything else — SGR above all,
// most of the CSIs a terminal ever prints — is not parsed at all.
var markFinals = [256]bool{'h': true, 'l': true, 'c': true, 'q': true, 'n': true, 'p': true, 'u': true, 't': true}

func (s *Scanner) csiMarks(seq []byte) {
	if !markFinals[seq[len(seq)-1]] {
		return
	}
	p, ok := parseCSI(seq)
	if !ok {
		return
	}
	raw := func() []byte { return append([]byte(nil), seq...) }
	switch {
	case (p.final == 'h' || p.final == 'l') && p.prefix == '?' && p.noInter():
		for _, m := range p.params[:p.n] {
			if m >= 0 {
				s.mark(Mark{Kind: KindMode, Mode: m, Set: p.final == 'h', Private: true})
			}
		}
	case p.final == 'c' && p.noInter() && p.param(0, 0) == 0:
		switch p.prefix {
		case 0:
			s.mark(Mark{Kind: KindQuery, Query: QueryDA1, Raw: raw()})
		case '>':
			s.mark(Mark{Kind: KindQuery, Query: QueryDA2, Raw: raw()})
		}
	case p.final == 'q' && p.prefix == '>' && p.noInter() && p.param(0, 0) == 0:
		s.mark(Mark{Kind: KindQuery, Query: QueryXTVERSION, Raw: raw()})
	case p.final == 'n' && p.noInter() && (p.prefix == 0 || p.prefix == '?'):
		s.mark(Mark{Kind: KindQuery, Query: QueryDSR, Mode: p.param(0, 0), Private: p.prefix == '?', Raw: raw()})
	case p.final == 'p' && p.hasInter('$') && (p.prefix == 0 || p.prefix == '?'):
		s.mark(Mark{Kind: KindQuery, Query: QueryDECRQM, Mode: p.param(0, 0), Private: p.prefix == '?', Raw: raw()})
	case p.final == 'p' && p.hasInter('!') && p.prefix == 0:
		s.mark(Mark{Kind: KindReset, Letter: 'p'})
	case p.final == 't' && p.noInter() && p.prefix == 0 && windowReports[p.param(0, 0)]:
		s.mark(Mark{Kind: KindQuery, Query: QueryXTWINOPS, Mode: p.param(0, 0), Raw: raw()})
	case p.final == 'u' && p.noInter():
		switch p.prefix {
		case '?':
			s.mark(Mark{Kind: KindQuery, Query: QueryKitty, Raw: raw()})
		case '>':
			s.mark(Mark{Kind: KindKitty, Op: '>', Flags: p.param(0, 0)})
		case '<':
			s.mark(Mark{Kind: KindKitty, Op: '<', Value: p.param(0, 1)})
		case '=':
			s.mark(Mark{Kind: KindKitty, Op: '=', Flags: p.param(0, 0), Value: p.param(1, 1)})
		}
	}
}

func (s *Scanner) startString(kind byte, introLen int) {
	s.st = str
	s.strKind = kind
	s.introLen = introLen
	s.strLen = 0
	s.strDrop = false
}

func (s *Scanner) strByte(b byte) {
	switch {
	case b == 0x1b:
		s.st = strEsc
		return
	case b == 0x07 && s.strKind == ']':
		s.finishString([]byte{0x07})
		return
	case b == 0x18 || b == 0x1a:
		// Cancelled: pass on what the terminal would have seen, so it cancels too.
		if !s.strDrop {
			s.emit(s.pend...)
		}
		s.emit(b)
		s.pend = s.pend[:0]
		s.st = ground
		return
	}
	s.strLen++
	if s.strDrop {
		return
	}
	if s.strLen > MaxString {
		s.strDrop = true
		s.pend = s.pend[:0]
		return
	}
	s.pend = append(s.pend, b)
}

// finishString completes the string in pend with terminator and passes it on, clamped. A nil
// terminator means the string was interrupted by another sequence; it is then closed with ST, so
// that what follows can never be read as more of it (the interrupting sequence may yet be dropped).
func (s *Scanner) finishString(terminator []byte) {
	if terminator == nil {
		terminator = []byte{0x1b, '\\'}
	}
	s.st = ground
	if s.strDrop {
		s.pend = s.pend[:0]
		return
	}
	payload := s.pend[s.introLen:]
	if s.strKind == ']' {
		payload = s.clampTitle(payload)
	}
	s.emit(s.pend[:s.introLen]...)
	s.emit(payload...)
	s.emit(terminator...)
	if !s.strip {
		switch s.strKind {
		case ']':
			s.oscMarks(payload)
		case 'P':
			if len(payload) >= 2 && payload[0] == '$' && payload[1] == 'q' {
				s.mark(Mark{Kind: KindQuery, Query: QueryDECRQSS, Text: string(payload[2:]),
					Raw: append(append(append([]byte(nil), s.pend[:s.introLen]...), payload...), terminator...)})
			}
		}
	}
	s.pend = s.pend[:0]
}

// clampTitle cuts an OSC 0, 1 or 2 payload to MaxTitle bytes of title at a character boundary.
func (s *Scanner) clampTitle(payload []byte) []byte {
	semi := indexByte(payload, ';')
	if semi < 0 {
		return payload
	}
	ps := string(payload[:semi])
	if ps != "0" && ps != "1" && ps != "2" {
		return payload
	}
	title := payload[semi+1:]
	if len(title) <= MaxTitle {
		return payload
	}
	cut := MaxTitle
	for cut > 0 && !utf8.RuneStart(title[cut]) {
		cut--
	}
	return payload[:semi+1+cut]
}

func indexByte(b []byte, c byte) int {
	for i, x := range b {
		if x == c {
			return i
		}
	}
	return -1
}

func (s *Scanner) oscMarks(payload []byte) {
	semi := indexByte(payload, ';')
	var ps, pt string
	if semi < 0 {
		ps = string(payload)
	} else {
		ps, pt = string(payload[:semi]), string(payload[semi+1:])
	}
	switch ps {
	case "0", "2":
		s.mark(Mark{Kind: KindTitle, Text: pt})
	case "7":
		if path, ok := fileURLPath(pt); ok {
			s.mark(Mark{Kind: KindCwd, Text: path})
		}
	case "133":
		if m, ok := shellMark(KindPrompt, pt); ok {
			s.mark(m)
		}
	case "633":
		if m, ok := shellMark(KindVscode, pt); ok {
			s.mark(m)
		}
	case "9":
		switch {
		case len(pt) >= 2 && pt[0] == '4' && pt[1] == ';':
			fields := splitFields(pt[2:])
			m := Mark{Kind: KindProgress}
			if len(fields) > 0 {
				m.State = atoi(fields[0])
			}
			if len(fields) > 1 {
				m.Value = min(100, atoi(fields[1]))
			}
			s.mark(m)
		case conEmuSubcommand(pt):
			// ConEmu's numbered OSC 9 commands (9;1 sleep, 9;9 cwd, 9;12 prompt…) are not
			// notifications.
		default:
			s.mark(Mark{Kind: KindNotify, Text: pt})
		}
	case "777":
		fields := splitN(pt, 3)
		if len(fields) >= 2 && fields[0] == "notify" {
			m := Mark{Kind: KindNotify, Title: fields[1]}
			if len(fields) == 3 {
				m.Text = fields[2]
			}
			s.mark(m)
		}
	case "10", "11", "12":
		// OSC 10;?;? asks for 10 and 11 at once. Only a pure query is one: a sequence that sets one
		// colour and asks for another is applied by the browser, which does not swallow it.
		slots := splitFields(pt)
		if allQuery(slots, 1) {
			s.mark(Mark{Kind: KindQuery, Query: QueryOSCColor, Mode: atoi(ps), Value: len(slots), Text: pt,
				Raw: append([]byte(nil), payload...)})
		}
	case "4":
		// OSC 4;index;?[;index;?…]: palette entries.
		slots := splitFields(pt)
		if len(slots)%2 == 0 && allQuery(slots[1:], 2) {
			s.mark(Mark{Kind: KindQuery, Query: QueryOSCColor, Mode: 4, Value: len(slots) / 2, Text: pt,
				Raw: append([]byte(nil), payload...)})
		}
	}
}

// allQuery reports whether every step-th slot is "?" (and there is at least one).
func allQuery(slots []string, step int) bool {
	if len(slots) == 0 {
		return false
	}
	for i := 0; i < len(slots); i += step {
		if slots[i] != "?" {
			return false
		}
	}
	return true
}

// shellMark parses an OSC 133 or 633 payload: a letter, then ;-separated fields. A bare number
// after D is the exit status; key=value fields become Params. For 633 E, the first field is the
// command line with VS Code's escaping undone.
//
// An empty letter (`133;;0`, found by fuzzing) is not a mark: indexing it took the daemon down.
func shellMark(kind Kind, pt string) (Mark, bool) {
	fields := splitFields(pt)
	if fields[0] == "" {
		return Mark{}, false
	}
	m := Mark{Kind: kind, Letter: fields[0][0]}
	rest := fields[1:]
	if m.Letter == 'D' && len(rest) > 0 && isNumber(rest[0]) {
		m.HasExit, m.Exit = true, atoi(rest[0])
		rest = rest[1:]
	}
	if kind == KindVscode && m.Letter == 'E' && len(rest) > 0 {
		m.Text = unescapeVscode(rest[0])
		rest = rest[1:]
	}
	for _, f := range rest {
		if k, v, ok := cut(f, '='); ok {
			if m.Params == nil {
				m.Params = map[string]string{}
			}
			if kind == KindVscode {
				v = unescapeVscode(v)
			}
			m.Params[k] = v
		}
	}
	return m, true
}

func cut(s string, c byte) (string, string, bool) {
	for i := 0; i < len(s); i++ {
		if s[i] == c {
			return s[:i], s[i+1:], true
		}
	}
	return s, "", false
}

func splitFields(s string) []string { return splitN(s, -1) }

func splitN(s string, n int) []string {
	var out []string
	for n < 0 || len(out) < n-1 {
		a, b, ok := cut(s, ';')
		if !ok {
			break
		}
		out = append(out, a)
		s = b
	}
	return append(out, s)
}

func isNumber(s string) bool {
	if s == "" {
		return false
	}
	if s[0] == '-' {
		s = s[1:]
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return s != ""
}

// atoi parses a small decimal number, saturating rather than overflowing.
func atoi(s string) int {
	neg := false
	if s != "" && s[0] == '-' {
		neg, s = true, s[1:]
	}
	n := 0
	for i := 0; i < len(s) && s[i] >= '0' && s[i] <= '9'; i++ {
		if n < 1<<30 {
			n = n*10 + int(s[i]-'0')
		}
	}
	if neg {
		return -n
	}
	return n
}

func conEmuSubcommand(pt string) bool {
	semi := indexByte([]byte(pt), ';')
	num := pt
	if semi >= 0 {
		num = pt[:semi]
	}
	return num != "" && isNumber(num) && num[0] != '-'
}

// fileURLPath decodes the path of an OSC 7 URL (file://host/path, or kitty's
// kitty-shell-cwd://host/path). The host is not checked: a shell over ssh reports a remote path,
// and the daemon cannot tell whose it is any better than the reader of the event can.
func fileURLPath(u string) (string, bool) {
	var rest string
	switch {
	case len(u) > 7 && u[:7] == "file://":
		rest = u[7:]
	case len(u) > 18 && u[:18] == "kitty-shell-cwd://":
		rest = u[18:]
	default:
		return "", false
	}
	slash := indexByte([]byte(rest), '/')
	if slash < 0 {
		return "", false
	}
	path := rest[slash:]
	if q := indexByte([]byte(path), '?'); q >= 0 {
		path = path[:q]
	}
	return percentDecode(path), true
}

func percentDecode(s string) string {
	if indexByte([]byte(s), '%') < 0 {
		return s
	}
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) && isHex(s[i+1]) && isHex(s[i+2]) {
			out = append(out, unhex(s[i+1])<<4|unhex(s[i+2]))
			i += 2
			continue
		}
		out = append(out, s[i])
	}
	return string(out)
}

// unescapeVscode undoes the escaping of OSC 633 values: \\ for a backslash and \xAB for a byte.
func unescapeVscode(s string) string {
	if indexByte([]byte(s), '\\') < 0 {
		return s
	}
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+1 < len(s) {
			if s[i+1] == '\\' {
				out = append(out, '\\')
				i++
				continue
			}
			if s[i+1] == 'x' && i+3 < len(s) && isHex(s[i+2]) && isHex(s[i+3]) {
				out = append(out, unhex(s[i+2])<<4|unhex(s[i+3]))
				i += 3
				continue
			}
		}
		out = append(out, s[i])
	}
	return string(out)
}

func isHex(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'
}

func unhex(c byte) byte {
	switch {
	case c >= '0' && c <= '9':
		return c - '0'
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10
	default:
		return c - 'A' + 10
	}
}
