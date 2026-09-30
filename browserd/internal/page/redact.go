package page

import (
	"bytes"
	"encoding/json"
	"io"
	"net/url"
	"regexp"
	"strings"
)

// What the page's traffic says that the agent must not read: a session's credentials. They are cut
// here, in the daemon, so they never cross the socket to the host, let alone into a model's context.
// The agent learns that a request carried an Authorization header or a token parameter — which is
// what finding a service's API needs — but never its value.

// Withheld stands in for a value that was cut.
const Withheld = "[withheld]"

// secretWords are the parts of a name (split at -, _, . and case changes) that make its value a
// credential in an address or a header. Broad on purpose: a harmless value cut costs the agent
// little, a token told costs the operator their session.
var secretWords = map[string]bool{
	"token": true, "tokens": true, "key": true, "apikey": true, "secret": true, "password": true, "passwd": true,
	"pwd": true, "pass": true, "passphrase": true, "session": true, "sessionid": true, "sid": true, "sig": true,
	"signature": true, "auth": true, "authorization": true, "authenticity": true, "credential": true, "credentials": true,
	"jwt": true, "otp": true, "code": true, "bearer": true, "cookie": true, "csrf": true, "xsrf": true, "ticket": true,
	"private": true, "nonce": true, "hash": true, "saml": true, "samlresponse": true, "assertion": true,
}

// bodyWords are secretWords for the members of a response body, where "code", "key", "hash" and
// "private" are the ordinary fields of an API (an error's code, an issue's key, a commit's hash) that
// the agent reads the body for. A key is cut there only with a word that makes it one (api_key,
// accessKey, private_key).
var bodyWords = map[string]bool{
	"token": true, "tokens": true, "apikey": true, "secret": true, "password": true, "passwd": true, "pwd": true,
	"passphrase": true, "session": true, "sessionid": true, "signature": true, "authorization": true,
	"credential": true, "credentials": true, "jwt": true, "otp": true, "cookie": true, "csrf": true, "xsrf": true,
	"saml": true, "samlresponse": true,
}

var keyWords = map[string]bool{"api": true, "access": true, "private": true, "secret": true, "signing": true,
	"encryption": true, "license": true, "client": true, "session": true}

var nameParts = regexp.MustCompile(`[A-Z]?[a-z0-9]+|[A-Z]+`)

// SecretName says a parameter or header of this name carries a credential.
func SecretName(name string) bool {
	if secretWords[strings.ToLower(name)] {
		return true
	}
	for _, part := range nameParts.FindAllString(name, -1) {
		if secretWords[strings.ToLower(part)] {
			return true
		}
	}
	return false
}

// SecretMember says a member of a response body of this name carries a credential.
func SecretMember(name string) bool {
	if bodyWords[strings.ToLower(name)] {
		return true
	}
	key, qualified := false, false
	for _, part := range nameParts.FindAllString(name, -1) {
		p := strings.ToLower(part)
		if bodyWords[p] {
			return true
		}
		key = key || p == "key"
		qualified = qualified || keyWords[p]
	}
	return key && qualified
}

// jwtShape is a JSON web token, whatever it is named.
var jwtShape = regexp.MustCompile(`eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*`)

// secretHeaders are cut whatever SecretName says of them.
var secretHeaders = map[string]bool{"authorization": true, "proxy-authorization": true, "cookie": true, "set-cookie": true}

// RedactHeader is a header's value as the agent may read it.
func RedactHeader(name, value string) string {
	n := strings.ToLower(name)
	if secretHeaders[n] || SecretName(n) {
		return Withheld
	}
	return jwtShape.ReplaceAllString(value, Withheld)
}

// RedactURL is an address with its credentials cut: the user and password before the host, and the
// value of every query or fragment parameter a SecretName names or that holds a token. The rest is
// kept as the page wrote it.
func RedactURL(raw string) string {
	if raw == "" || strings.HasPrefix(raw, "data:") {
		return clipURL(raw)
	}
	u, err := url.Parse(raw)
	if err != nil {
		return clipURL(jwtShape.ReplaceAllString(raw, Withheld))
	}
	if u.User != nil {
		u.User = nil
	}
	u.RawQuery = redactPairs(u.RawQuery)
	if strings.Contains(u.Fragment, "=") {
		// An OAuth answer puts its token after the #: access_token=…&token_type=bearer.
		frag := u.EscapedFragment()
		u.Fragment, u.RawFragment = "", ""
		out := u.String() + "#" + redactPairs(frag)
		return clipURL(out)
	}
	return clipURL(jwtShape.ReplaceAllString(u.String(), Withheld))
}

// redactPairs cuts the secret values of an a=1&b=2 list, keeping its order and its escapes.
func redactPairs(q string) string {
	return redactPairsBy(q, SecretName)
}

func redactPairsBy(q string, secret func(string) bool) string {
	if q == "" {
		return q
	}
	pairs := strings.Split(q, "&")
	for i, pair := range pairs {
		name, value, ok := strings.Cut(pair, "=")
		if !ok {
			continue
		}
		plain, err := url.QueryUnescape(name)
		if err != nil {
			plain = name
		}
		if secret(plain) || jwtShape.MatchString(value) {
			pairs[i] = name + "=" + Withheld
		}
	}
	return strings.Join(pairs, "&")
}

// urlMax bounds an address as it is kept: a page can make one of megabytes.
const urlMax = 2000

func clipURL(s string) string {
	if len(s) <= urlMax {
		return s
	}
	return s[:urlMax] + "…"
}

// RedactBody is a response body as the agent may read it. JSON has the value of every member a
// SecretName names cut, at any depth, and its members kept in their order; a form's body its secret
// pairs; HTML the value of hidden and password inputs and of the tags a site keeps its request
// token in. Any text has what looks like a JSON web token cut.
func RedactBody(body, mime string) string {
	m := strings.ToLower(mime)
	switch {
	case strings.Contains(m, "json"):
		if out, ok := redactJSON(body); ok {
			return out
		}
	case strings.Contains(m, "x-www-form-urlencoded"):
		return redactPairsBy(body, SecretMember)
	case strings.Contains(m, "html"):
		body = htmlSecrets.ReplaceAllStringFunc(body, redactTag)
	}
	return jwtShape.ReplaceAllString(textPairs(body), Withheld)
}

// urlsInText are the addresses a line of text quotes: the browser's own messages name the address
// that failed, token and all, and a page logs the ones it calls.
var urlsInText = regexp.MustCompile(`\b(?:https?|wss?)://[^\s'"<>()\[\]{}]+`)

// RedactText is a line of the console as the agent may read it: the addresses in it cut as
// RedactURL cuts them, and its secret pairs and tokens as a body's text. A page that logs its own
// session token is not a reason to hand it to the agent.
func RedactText(s string) string {
	s = urlsInText.ReplaceAllStringFunc(s, RedactURL)
	return jwtShape.ReplaceAllString(textPairs(s), Withheld)
}

// barePairs are name=value and name: value with a value in no quotes (a form's pairs, inside a
// string or not); quotedPairs are "name": "value" and name = 'value', the value taken whole, spaces
// and all ("Bearer …"). The bare ones go first: a quoted value can hold pairs of its own
// ("user=someone&password=…"), which the quoted rule would pass over under the outer name.
var (
	barePairs   = regexp.MustCompile(`(?i)(["']?)([a-z0-9_.-]{2,40})(["']?\s*[:=]\s*)([^"'&\s,;}]{4,})`)
	quotedPairs = regexp.MustCompile(`(?i)(["']?)([a-z0-9_.-]{2,40})(["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*')`)
)

func textPairs(body string) string {
	for _, re := range []*regexp.Regexp{barePairs, quotedPairs} {
		body = re.ReplaceAllStringFunc(body, func(m string) string {
			sub := re.FindStringSubmatch(m)
			if sub == nil || !SecretMember(sub[2]) {
				return m
			}
			value := Withheld
			if q := sub[4][0]; q == '"' || q == '\'' {
				value = string(q) + Withheld + string(q)
			}
			return sub[1] + sub[2] + sub[3] + value
		})
	}
	return body
}

// htmlSecrets are the tags whose values a page holds secrets in: inputs, and the meta tags of a
// request token.
var htmlSecrets = regexp.MustCompile(`(?is)<(input|meta)\b[^>]*>`)

var attrValue = regexp.MustCompile(`(?is)\b(value|content)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)`)

func redactTag(tag string) string {
	low := strings.ToLower(tag)
	secret := strings.Contains(low, `type="password"`) || strings.Contains(low, `type=password`) || strings.Contains(low, `type='password'`) ||
		strings.Contains(low, `type="hidden"`) || strings.Contains(low, `type=hidden`) || strings.Contains(low, `type='hidden'`)
	if !secret {
		for _, name := range attrName.FindAllStringSubmatch(tag, -1) {
			if SecretName(strings.Trim(name[2], `"'`)) {
				secret = true
			}
		}
	}
	if !secret {
		return tag
	}
	return attrValue.ReplaceAllString(tag, `$1="`+Withheld+`"`)
}

var attrName = regexp.MustCompile(`(?is)\b(name|id|autocomplete)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)`)

// redactJSON rewrites a JSON text with the secret members' values cut, token by token so the
// members keep their order. ok is false for a text that is not JSON.
func redactJSON(body string) (string, bool) {
	dec := json.NewDecoder(strings.NewReader(body))
	dec.UseNumber()
	var out bytes.Buffer
	if err := redactValue(dec, &out, false); err != nil {
		return "", false
	}
	if _, err := dec.Token(); err != io.EOF {
		return "", false
	}
	return out.String(), true
}

// redactValue copies one JSON value from dec to out; cut says it is a secret member's, written as
// Withheld whatever it is.
func redactValue(dec *json.Decoder, out *bytes.Buffer, cut bool) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	switch v := tok.(type) {
	case json.Delim:
		if cut {
			// The whole object or list under a secret name goes: skip to its end.
			if err := skipValue(dec); err != nil {
				return err
			}
			out.WriteString(`"` + Withheld + `"`)
			return nil
		}
		switch v {
		case '{':
			out.WriteByte('{')
			for i := 0; dec.More(); i++ {
				key, err := dec.Token()
				if err != nil {
					return err
				}
				name, _ := key.(string)
				if i > 0 {
					out.WriteByte(',')
				}
				b, _ := json.Marshal(name)
				out.Write(b)
				out.WriteByte(':')
				if err := redactValue(dec, out, SecretMember(name)); err != nil {
					return err
				}
			}
			if _, err := dec.Token(); err != nil {
				return err
			}
			out.WriteByte('}')
		case '[':
			out.WriteByte('[')
			for i := 0; dec.More(); i++ {
				if i > 0 {
					out.WriteByte(',')
				}
				if err := redactValue(dec, out, false); err != nil {
					return err
				}
			}
			if _, err := dec.Token(); err != nil {
				return err
			}
			out.WriteByte(']')
		}
		return nil
	case string:
		if cut || jwtShape.MatchString(v) {
			v = jwtShape.ReplaceAllString(v, Withheld)
			if cut {
				v = Withheld
			}
		}
		b, _ := json.Marshal(v)
		out.Write(b)
	case nil:
		out.WriteString("null")
	default:
		if cut {
			out.WriteString(`"` + Withheld + `"`)
			return nil
		}
		b, _ := json.Marshal(v)
		out.Write(b)
	}
	return nil
}

// skipValue reads to the end of the object or list whose opening dec just gave.
func skipValue(dec *json.Decoder) error {
	for depth := 1; depth > 0; {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		if d, ok := tok.(json.Delim); ok {
			if d == '{' || d == '[' {
				depth++
			} else {
				depth--
			}
		}
	}
	return nil
}
