package workflow

import (
	"net/url"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// What a recording keeps of the words it meets is bounded: a step is a line a person reads, and the
// page's words in it are data, never more than a name.
const (
	maxName  = 100
	maxPlace = 80
	maxTitle = 120
	maxText  = 200
	maxURL   = 500
	maxSlot  = 32
	// MaxLiteral is the longest typed value a recording keeps, where the operator lets it keep any:
	// a search, a filter, a name of a report. Longer is a letter or a document, which is a blank.
	MaxLiteral = 200
)

// clip folds white space and cuts s to n characters, never inside one.
func clip(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n-1]) + "…"
}

var (
	dateLike   = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}`)
	longNumber = regexp.MustCompile(`\+?\d[\d\s().-]{7,}\d`)
	hostLike   = regexp.MustCompile(`^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$|^\d+\.\d+\.\d+\.\d+(:\d+)?$|^localhost(:\d+)?$`)
)

// scrub takes out of a page's words what a recording must not carry even there: an address with its
// query (Chromium titles a loading page with its whole address, a sign-in link's token included), an
// e-mail address (a mail site's title names the account), a key, and a number long enough to be a
// telephone, a card or an account.
func scrub(s string) string {
	ws := strings.Fields(s)
	for i, w := range ws {
		switch {
		case strings.Contains(w, "://"):
			ws[i] = CleanURL(w)
			if ws[i] == "" {
				ws[i] = "{address}"
			}
		case emailLike.MatchString(w):
			ws[i] = "{email}"
		case strings.ContainsAny(w, "/?=") && hostLike.MatchString(strings.ToLower(strings.SplitN(strings.SplitN(w, "/", 2)[0], "?", 2)[0])):
			ws[i] = strings.TrimPrefix(CleanURL("http://"+w), "http://")
		case tokenLike(strings.Trim(w, ".,;:!?()[]\"'")):
			ws[i] = "{token}"
		}
	}
	s = strings.Join(ws, " ")
	return longNumber.ReplaceAllStringFunc(s, func(m string) string {
		if dateLike.MatchString(m) {
			return m // a date and a time run to nine digits, and are a page's plain words
		}
		digits := 0
		for _, c := range m {
			if unicode.IsDigit(c) {
				digits++
			}
		}
		if digits >= 9 {
			return "{number}"
		}
		return m
	})
}

// words is a page's words as a recording keeps them: scrubbed and cut to n characters.
func words(s string, n int) string { return clip(scrub(s), n) }

// sensitiveParam names a query parameter whose value is a credential or a person's: a sign-in link's
// token, an OAuth code and state, a signature, an address. Its value is never kept, whatever it is.
// A name is matched word by word ("api_key", "access_token", "X-Amz-Signature"), so "keyword" and
// "category" keep their values.
var sensitiveParam = regexp.MustCompile(`(?i)(^|[^a-z])(\w*token|apikey|key|code|auth\w*|sess\w*|sid|sig|signature|secret|pass\w*|pwd|otp|state|nonce|ticket|jwt|refresh\w*|credential\w*|hash|e?mail|phone|tel|login|user\w*|account\w*|card\w*)([^a-z]|$)`)

var (
	emailLike = regexp.MustCompile(`[^\s@]+@[^\s@]+\.[^\s@]+`)
	b64Like   = regexp.MustCompile(`^[A-Za-z0-9+/=_.~-]+$`)
)

// tokenLike says a word looks like a key, a session id or a code rather than a word: long, without
// spaces, of letters and digits mixed, or long in the alphabet of hex and base64.
func tokenLike(w string) bool {
	if len(w) >= 32 && b64Like.MatchString(w) {
		return true
	}
	if len(w) < 16 {
		return false
	}
	letters, digits := false, false
	for _, c := range w {
		switch {
		case unicode.IsLetter(c):
			letters = true
		case unicode.IsDigit(c):
			digits = true
		}
	}
	return letters && digits
}

// SafeLiteral says a typed value may be kept as it is, when the operator lets values be kept: short,
// and nothing in it that looks like an address, a number that is a phone or a card or an account,
// or a key. Everything else stays a blank the agent fills.
func SafeLiteral(v string) bool {
	if utf8.RuneCountInString(v) > MaxLiteral {
		return false
	}
	if emailLike.MatchString(v) {
		return false
	}
	digits := 0
	for _, c := range v {
		if unicode.IsDigit(c) {
			digits++
		}
	}
	if digits >= 9 {
		return false
	}
	for _, w := range strings.Fields(v) {
		if tokenLike(w) {
			return false
		}
	}
	return true
}

// CleanURL is an address as a recording keeps it: http and https only, without a user name, a
// password or a fragment; a query value that names a credential or a person, or looks like a key,
// becomes the blank {name}; a path segment that looks like a key becomes {token}. What is left says
// where the page is, which is what a procedure needs, and never how to get in.
func CleanURL(raw string) string {
	if raw == "about:blank" {
		return raw
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return ""
	}
	u.User = nil
	u.Fragment, u.RawFragment = "", ""
	segs := strings.Split(u.EscapedPath(), "/")
	for i, s := range segs {
		plain, err := url.PathUnescape(s)
		if err != nil || tokenLike(plain) {
			segs[i] = "{token}"
		}
	}
	path := strings.Join(segs, "/")
	var query []string
	if u.RawQuery != "" {
		for _, pair := range strings.Split(u.RawQuery, "&") {
			if pair == "" {
				continue
			}
			k, v, _ := strings.Cut(pair, "=")
			name, err := url.QueryUnescape(k)
			if err != nil {
				continue
			}
			value, err := url.QueryUnescape(v)
			if err != nil || sensitiveParam.MatchString(name) || !SafeLiteral(value) || utf8.RuneCountInString(value) > 60 {
				query = append(query, url.QueryEscape(name)+"={"+slotBase(name, "value")+"}")
				continue
			}
			query = append(query, url.QueryEscape(name)+"="+url.QueryEscape(value))
		}
	}
	out := u.Scheme + "://" + u.Host + path
	if len(query) > 0 {
		out += "?" + strings.Join(query, "&")
	}
	return clip(out, maxURL)
}

// slotBase is a blank's name made from the words that name a field: lower case, letters and digits
// joined by underscores, in any script ("Report period" is report_period, "Поиск" is поиск).
func slotBase(name, fallback string) string {
	var b strings.Builder
	under := false
	for _, c := range strings.ToLower(name) {
		if unicode.IsLetter(c) || unicode.IsDigit(c) {
			if under && b.Len() > 0 {
				b.WriteByte('_')
			}
			under = false
			b.WriteRune(c)
			continue
		}
		under = true
	}
	s := b.String()
	if r := []rune(s); len(r) > maxSlot {
		s = strings.TrimRight(string(r[:maxSlot]), "_")
	}
	if s == "" {
		return fallback
	}
	return s
}
