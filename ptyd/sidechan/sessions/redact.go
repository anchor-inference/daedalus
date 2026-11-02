package sessions

import (
	"encoding/json"
	"regexp"
	"strings"
)

// Mask is what a secret is replaced with, the same mark the host's redactor uses, so a text masked
// here and masked again there reads the same.
const Mask = "•••"

// The secret shapes, ported from daedalus/security/redact.py (`_SHAPES`). They are applied before
// any text leaves the daemon, so a key printed into another program's transcript never crosses the
// socket in the clear; the host masks again with the values it knows. The Python patterns use
// look-arounds and a back-reference, which Go's regexp does not have: those parts are checks in
// code (credValue, secretName) around a looser pattern, and the tests hold the two ports to the
// same cases.
var (
	pemShape = regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`)
	// Each whole-match shape carries the literals one of its matches must contain. Go's regexp has
	// no literal prefilter for patterns that open with \b, and running fifteen of them over every
	// byte made masking most of the time of reading a session of hundreds of megabytes; a text
	// without the literal cannot match, so the pattern is skipped.
	wholeShapes = []shape{
		{regexp.MustCompile(`\b\d{8,12}:[A-Za-z0-9_-]{30,}\b`), nil}, // telegram: digitsColon decides
		{regexp.MustCompile(`\bsk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_-]{16,}\b`), []string{"sk-"}},
		{regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{20,}\b`), []string{"ghp_", "gho_", "ghu_", "ghs_", "ghr_"}},
		{regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}\b`), []string{"github_pat_"}},
		{regexp.MustCompile(`\bgl(?:pat|dt|rt|ptt|oas)-[A-Za-z0-9_-]{16,}\b`), []string{"glpat-", "gldt-", "glrt-", "glptt-", "gloas-"}},
		{regexp.MustCompile(`\b(?:AKIA|ASIA)[A-Z0-9]{16}\b`), []string{"AKIA", "ASIA"}},
		{regexp.MustCompile(`\bxox[abprs]-[A-Za-z0-9-]{10,}\b`), []string{"xox"}},
		{regexp.MustCompile(`\bAIza[A-Za-z0-9_-]{30,}\b`), []string{"AIza"}},
		{regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b`), []string{".eyJ"}},
	}
	authHeader = regexp.MustCompile(`(?i)\b((?:authorization\s*:\s*)?(?:bearer|basic)\s+|authorization\s*:\s*token\s+)([A-Za-z0-9._~+/=-]{12,})`)
	apiHeader  = regexp.MustCompile(`(?i)\b((?:x-)?(?:api[-_]?key|auth[-_]?token|access[-_]?token|private[-_]?token)\s*:\s*)([A-Za-z0-9._~+/=-]{12,})`)
	// API_KEY=… and export DB_PASSWORD='…': the name and the opening quote; the value is read in code.
	envAssignment = regexp.MustCompile(`\b([A-Z][A-Z0-9_]*)(\s*=\s*)(["']?)`)
	// "api_key": "…": any quoted key; secretName decides.
	quotedMember = regexp.MustCompile(`(["'])([A-Za-z0-9_.-]*)(["']\s*[:=]\s*["'])([^"'\n]{8,})(["'])`)
	// api_key: value, unquoted.
	bareMember  = regexp.MustCompile(`\b([A-Za-z0-9_.-]+)(\s*:\s*)([A-Za-z0-9_\-./+=~:]{16,})`)
	urlUserinfo = regexp.MustCompile(`(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@/\s]{3,})(@)`)

	refPlaceholder    = regexp.MustCompile(`^«ref:[0-9a-f]{10}»$`)
	secretPlaceholder = regexp.MustCompile(`^«secret:[a-z][a-z0-9_]{0,47}»$`)
	templateSlot      = regexp.MustCompile(`^\{[A-Za-z_][A-Za-z0-9_]*\}$`)
)

// shape is a pattern and the literals that a text must hold for it to match.
type shape struct {
	re    *regexp.Regexp
	hints []string
}

func containsAny(s string, subs ...string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// digitsColon reports eight digits followed by a colon somewhere in s, which a Telegram token
// starts with.
func digitsColon(s string) bool {
	for i := 0; ; {
		j := strings.IndexByte(s[i:], ':')
		if j < 0 {
			return false
		}
		i += j
		n := 0
		for k := i - 1; k >= 0 && s[k] >= '0' && s[k] <= '9' && n < 8; k-- {
			n++
		}
		if n == 8 {
			return true
		}
		i++
	}
}

// lowerSecretWords are the secret names in lower case, for the prefilter of the key-named shapes.
var lowerSecretWords = []string{"key", "secret", "token", "passw", "credential", "auth"}

// secretWords are the names that mark a secret (`_SECRET_NAME`), upper case.
var secretWords = []string{"API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD",
	"CREDENTIALS", "CREDENTIAL", "PRIVATE_KEY", "PRIVATEKEY", "ACCESS_KEY", "ACCESSKEY"}

func isLetter(c byte) bool { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') }

// secretName reports where a secret word occurs in a name, at or after `from`, and whether one
// ends the name (`atEnd`). The name is compared in upper case. AUTH counts only standing alone
// (not inside `author` or `oauth_provider`, not before `_user_id`), as in the Python pattern.
func secretName(name string, from int) (found, atEnd bool) {
	up := strings.ToUpper(name)
	for _, w := range secretWords {
		for at := from; at+len(w) <= len(up); at++ {
			if up[at:at+len(w)] == w {
				found = true
				atEnd = atEnd || at+len(w) == len(up)
			}
		}
	}
	for start := from; start < len(up); {
		i := strings.Index(up[start:], "AUTH")
		if i < 0 {
			break
		}
		at := start + i
		before := at == 0 || !isLetter(up[at-1])
		end := at + 4
		after := end == len(up) || !(isLetter(up[end]) || up[end] == '_')
		if before && after {
			found = true
			if end == len(up) {
				atEnd = true
			}
		}
		start = at + 1
	}
	return found, atEnd
}

// credValue is `_CRED_VALUE`: a run of at least 16 credential characters that holds both a letter
// and a digit within its first unbroken word. It returns the length of the run, or 0.
func credValue(s string) int {
	n := 0
	for n < len(s) && isCredChar(s[n]) {
		n++
	}
	if n < 16 {
		return 0
	}
	word := 0
	for word < len(s) && !strings.ContainsRune(" \t\n\r\f\v\"'`,;", rune(s[word])) {
		word++
	}
	digit, letter := false, false
	for i := 0; i < word; i++ {
		c := s[i]
		digit = digit || (c >= '0' && c <= '9')
		letter = letter || isLetter(c)
	}
	if !digit || !letter {
		return 0
	}
	return n
}

func isCredChar(c byte) bool {
	return isLetter(c) || (c >= '0' && c <= '9') || strings.IndexByte("_-./+=~:", c) >= 0
}

// placeholder reports a value that is a slot or a reference the host hands out, never a secret.
func placeholder(v string) bool {
	return refPlaceholder.MatchString(v) || secretPlaceholder.MatchString(v) || templateSlot.MatchString(v)
}

// Redactor masks secret shapes and counts what it masked. One is made per reply; it is not shared.
type Redactor struct {
	Count int
}

// Text masks the secret shapes in one text.
func (r *Redactor) Text(s string) string {
	if len(s) < 8 {
		return s
	}
	if strings.Contains(s, "PRIVATE KEY-----") {
		s = pemShape.ReplaceAllStringFunc(s, func(string) string { r.Count++; return Mask })
	}
	for _, sh := range wholeShapes {
		if sh.hints == nil && !digitsColon(s) || sh.hints != nil && !containsAny(s, sh.hints...) {
			continue
		}
		s = sh.re.ReplaceAllStringFunc(s, func(m string) string {
			if placeholder(m) {
				return m
			}
			r.Count++
			return Mask
		})
	}
	lower := strings.ToLower(s)
	if containsAny(lower, "bearer", "basic", "authorization") {
		s = r.groups(authHeader, s, 2)
	}
	if containsAny(lower, lowerSecretWords...) {
		s = r.named(s)
	}
	if strings.Contains(s, "://") && strings.Contains(s, "@") {
		s = r.groups(urlUserinfo, s, 2)
	}
	return s
}

// named masks the shapes that need a secret's name beside the value: headers, assignments and
// members.
func (r *Redactor) named(s string) string {
	s = r.groups(apiHeader, s, 2)
	if strings.Contains(s, "=") {
		s = r.env(s)
	}
	s = replaceSubmatch(quotedMember, s, func(m []string) string {
		if found, _ := secretName(m[2], 0); !found || placeholder(m[4]) {
			return m[0]
		}
		r.Count++
		return m[1] + m[2] + m[3] + Mask + m[5]
	})
	s = replaceSubmatch(bareMember, s, func(m []string) string {
		if _, atEnd := secretName(m[1], 0); !atEnd {
			return m[0]
		}
		n := credValue(m[3])
		if n == 0 || placeholder(m[3][:n]) {
			return m[0]
		}
		r.Count++
		return m[1] + m[2] + Mask + m[3][n:]
	})
	return s
}

// groups replaces one group of each match with the mask, keeping the rest.
func (r *Redactor) groups(re *regexp.Regexp, s string, group int) string {
	return replaceSubmatch(re, s, func(m []string) string {
		if placeholder(m[group]) {
			return m[0]
		}
		r.Count++
		var b strings.Builder
		for i := 1; i < len(m); i++ {
			if i == group {
				b.WriteString(Mask)
			} else {
				b.WriteString(m[i])
			}
		}
		return b.String()
	})
}

// env masks upper-snake assignments of a secret-named variable. The Python pattern requires a
// letter before the secret word (`[A-Z][A-Z0-9_]*TOKEN`), so a bare `TOKEN=` is left alone, and
// it closes a quoted value with the same quote it opened.
func (r *Redactor) env(s string) string {
	idx := envAssignment.FindAllStringSubmatchIndex(s, -1)
	if idx == nil {
		return s
	}
	var b strings.Builder
	last := 0
	for _, m := range idx {
		if m[0] < last {
			continue
		}
		name := s[m[2]:m[3]]
		if found, _ := secretName(name, 1); !found {
			continue
		}
		quote := s[m[6]:m[7]]
		rest := s[m[1]:]
		var n int
		if quote != "" {
			end := strings.IndexAny(rest, "\"'\n")
			if end >= 0 && rest[end:end+1] == quote {
				if c := credValue(rest); c > 0 && c == end {
					n = c
				} else if end >= 8 {
					n = end
				}
			}
		} else {
			n = credValue(rest)
		}
		if n == 0 || placeholder(rest[:n]) {
			continue
		}
		r.Count++
		b.WriteString(s[last:m[1]])
		b.WriteString(Mask)
		last = m[1] + n
	}
	if last == 0 {
		return s
	}
	b.WriteString(s[last:])
	return b.String()
}

func replaceSubmatch(re *regexp.Regexp, s string, fn func([]string) string) string {
	idx := re.FindAllStringSubmatchIndex(s, -1)
	if idx == nil {
		return s
	}
	var b strings.Builder
	last := 0
	for _, m := range idx {
		groups := make([]string, len(m)/2)
		for g := range groups {
			if m[2*g] >= 0 {
				groups[g] = s[m[2*g]:m[2*g+1]]
			}
		}
		b.WriteString(s[last:m[0]])
		b.WriteString(fn(groups))
		last = m[1]
	}
	b.WriteString(s[last:])
	return b.String()
}

// Any masks the strings inside a decoded JSON value. Under a secret-named key a credential-looking
// string is masked even when no shape matches (`redact_any(secret_context=True)`), since
// {"api_key": "…"} gives the shapes nothing to see once the key is a map key and not text.
func (r *Redactor) Any(v any, secret bool) any {
	switch x := v.(type) {
	case string:
		out := r.Text(x)
		if out == x && secret && containsCred(x) {
			r.Count++
			return Mask
		}
		return out
	case map[string]any:
		for k, item := range x {
			found, _ := secretName(k, 0)
			x[k] = r.Any(item, secret || found)
		}
		return x
	case []any:
		for i, item := range x {
			x[i] = r.Any(item, secret)
		}
		return x
	}
	return v
}

// containsCred is `_CRED_VALUE_RE.search`: a credential-looking run anywhere in the text.
func containsCred(s string) bool {
	for i := 0; i < len(s); i++ {
		if (i == 0 || !isCredChar(s[i-1])) && credValue(s[i:]) > 0 {
			return true
		}
	}
	return false
}

// JSON masks a raw JSON document's strings and gives it back encoded. A document that does not
// decode is masked as text.
func (r *Redactor) JSON(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return raw
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		out, _ := json.Marshal(r.Text(string(raw)))
		return out
	}
	before := r.Count
	v = r.Any(v, false)
	if r.Count == before {
		return raw
	}
	out, err := json.Marshal(v)
	if err != nil {
		return raw
	}
	return out
}

// Turn masks every text a turn carries: what was said, the arguments of calls, their output, the
// summaries and the notes. Pictures are not text and pass as they are.
func (r *Redactor) Turn(t *Turn) {
	for i := range t.Parts {
		p := &t.Parts[i]
		p.Text = r.Text(p.Text)
		p.Output = r.Text(p.Output)
		p.Summary = r.Text(p.Summary)
		if len(p.Input) > 0 {
			p.Input = r.JSON(p.Input)
		}
		if p.Data != nil {
			p.Data = r.Any(roundTrip(p.Data), false)
		}
	}
}

// roundTrip turns a note's data into plain JSON values, so Any can walk it whatever Go types a
// parser built it from.
func roundTrip(v any) any {
	switch v.(type) {
	case string, map[string]any, []any, nil:
		return v
	}
	b, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	var out any
	if json.Unmarshal(b, &out) != nil {
		return nil
	}
	return out
}
