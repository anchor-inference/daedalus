// Package sensitive says which of an action's consequences the operator must approve: a login, a
// purchase, a message sent, something deleted, terms accepted, a file uploaded, a form posted to
// another site. It only classifies; asking is the host's policy.
//
// The word lists are deliberately broad. A false alarm costs the operator a tap; a purchase the
// model did not think to ask about costs money.
package sensitive

import (
	"net/url"
	"regexp"
	"sort"
	"strings"
)

// The kinds, as the contract names them.
const (
	Credentials     = "credentials"
	Purchase        = "purchase"
	Send            = "send"
	Destroy         = "destroy"
	Accept          = "accept"
	Upload          = "upload"
	CrossOriginPost = "cross_origin_post"
)

// Evidence is what the page says about the element an action lands on.
type Evidence struct {
	Name       string  `json:"name"`
	Role       string  `json:"role"`
	Tag        string  `json:"tag"`
	Type       string  `json:"type"`
	Text       string  `json:"text"`
	Value      string  `json:"value"`
	Heading    string  `json:"heading"`
	Form       bool    `json:"form"`
	FormAction string  `json:"form_action"`
	FormMethod string  `json:"form_method"`
	Submits    bool    `json:"submits"`
	Password   bool    `json:"password"`
	Payment    bool    `json:"payment"`
	OTP        bool    `json:"otp"`
	File       bool    `json:"file"`
	PageOrigin string  `json:"page_origin"`
	Fields     []Field `json:"fields"`
}

// Field is one field of the element's form.
type Field struct {
	Type         string `json:"type"`
	Name         string `json:"name"`
	Autocomplete string `json:"autocomplete"`
}

// Result is the classification and what it rests on.
type Result struct {
	Kinds    []string       `json:"kinds"`
	Evidence map[string]any `json:"evidence"`
}

// words are matched as whole words or phrases, case-insensitively, in English and Russian. The last
// Russian word of a phrase matches with any ending (купить, купите, оплатите), since the stem carries
// the meaning: its last two letters are dropped and any letters may follow.
var words = map[string][]string{
	Purchase: {"buy", "buy now", "pay", "pay now", "place order", "checkout", "check out", "purchase",
		"donate", "book now", "complete order", "confirm order", "confirm purchase", "add payment", "payment", "billing",
		"купить", "оплатить", "оплата", "оформить заказ", "заказать", "забронировать", "пожертвовать",
		"подтвердить заказ", "подтвердить оплату"},
	Send: {"send", "post", "publish", "share", "reply", "submit", "tweet", "comment", "retweet", "repost",
		"отправить", "опубликовать", "поделиться", "ответить", "комментировать", "запостить"},
	Destroy: {"delete", "remove", "cancel subscription", "unsubscribe", "close account", "delete account", "revoke",
		"erase", "discard", "deactivate", "terminate",
		"удалить", "отменить подписку", "отписаться", "закрыть счёт", "закрыть счет", "удалить аккаунт", "отозвать",
		"стереть", "деактивировать"},
	Accept: {"accept", "agree", "i agree", "accept all", "allow all", "consent", "i accept",
		"принять", "согласен", "согласна", "соглашаюсь", "принять все", "разрешить все"},
}

// refusals are the answers to a consent banner that are not an agreement: "reject all" contains
// "all" but accepts nothing. The agent is told to prefer them, so each common wording of one must pass
// without a question: "Accept only essential cookies" and "Продолжить без принятия" hold an accepting
// word ("принятия" shares the stem of "принять") and would otherwise be asked about.
var refusals = []string{"reject", "decline", "deny", "refuse", "only necessary", "necessary only", "essential only",
	"only essential", "only required", "required only", "strictly necessary", "necessary cookies", "essential cookies",
	"without accepting", "don't accept", "do not accept",
	"отклонить", "отказаться", "только необходимые", "только обязательные", "только технические", "без принятия",
	"без согласия", "не принимать", "не согласен"}

// subscriptions are a purchase only beside a price: "Subscribe" under a newsletter box costs nothing and
// was asked about as a purchase on every sign-up, while "Subscribe — $9.99/month" is one. A paid plan
// whose button names no price still meets the purchase words or the payment form on the next page.
var subscriptions = []string{"subscribe", "subscribe now", "start subscription", "start trial", "подписаться", "оформить подписку"}

// price is an amount of money, or a period that says it recurs.
var price = regexp.MustCompile(`(?i)([$€£¥₽]\s*\d|\d\s*([$€£¥₽]|usd|eur|gbp|руб|р\.)|per month|per year|/\s*(mo|month|yr|year)\b|в месяц|в год|/\s*мес)`)

var patterns = map[string]*regexp.Regexp{}
var refusal *regexp.Regexp
var subscription *regexp.Regexp

func phrase(w string) string {
	parts := strings.Fields(w)
	for i, p := range parts {
		q := regexp.QuoteMeta(p)
		if r := []rune(p); isCyrillic(p) && i == len(parts)-1 && len(r) >= 5 {
			q = regexp.QuoteMeta(string(r[:len(r)-2])) + `\p{L}*`
		}
		parts[i] = q
	}
	return strings.Join(parts, `\s+`)
}

func isCyrillic(s string) bool {
	for _, r := range s {
		if r >= 'а' && r <= 'я' || r == 'ё' {
			return true
		}
	}
	return false
}

func compile(list []string) *regexp.Regexp {
	alts := make([]string, len(list))
	for i, w := range list {
		alts[i] = phrase(w)
	}
	// \b is ASCII-only in Go's regexp, so word edges are spelt out for Cyrillic too.
	return regexp.MustCompile(`(?i)(^|[^\p{L}\p{N}])(` + strings.Join(alts, "|") + `)($|[^\p{L}\p{N}])`)
}

func init() {
	for k, list := range words {
		patterns[k] = compile(list)
	}
	refusal = compile(refusals)
	subscription = compile(subscriptions)
}

// Classify says which kinds apply to an action on the element evidence describes. action is the
// contract's (click, type, press, …); submit is true for a type that presses Enter after.
func Classify(action string, ev Evidence, submit bool) Result {
	kinds := map[string]bool{}
	matched := []string{}
	acts := action == "click" || action == "double_click" || action == "press" || (action == "type" && submit)
	label := strings.ToLower(strings.Join([]string{ev.Name, ev.Text, ev.Value}, " "))
	if acts {
		if ev.Submits && (ev.Password || ev.OTP) {
			kinds[Credentials] = true
		}
		if ev.Submits && ev.Payment {
			kinds[Purchase] = true
		}
		for _, k := range []string{Purchase, Send, Destroy, Accept} {
			m := patterns[k].FindStringSubmatch(label)
			if m == nil && k == Purchase && ev.Submits {
				// A checkout button named only "Continue" under a heading that says "Payment".
				m = patterns[k].FindStringSubmatch(strings.ToLower(ev.Heading))
			}
			if m == nil && k == Purchase && price.MatchString(label) {
				m = subscription.FindStringSubmatch(label)
			}
			if m == nil {
				continue
			}
			if k == Accept && refusal.MatchString(label) {
				continue
			}
			kinds[k] = true
			matched = append(matched, strings.TrimSpace(m[2]))
		}
		if ev.Submits && ev.Form && crossSite(ev.PageOrigin, ev.FormAction) && strings.EqualFold(ev.FormMethod, "post") {
			kinds[CrossOriginPost] = true
		}
	}
	if ev.File || action == "upload" {
		kinds[Upload] = true
	}
	out := Result{Kinds: []string{}, Evidence: map[string]any{"name": ev.Name, "role": ev.Role, "words": matched,
		"page_origin": ev.PageOrigin, "fields": fieldsOrEmpty(ev.Fields)}}
	if ev.Form {
		out.Evidence["form_action"] = ev.FormAction
		if u, err := url.Parse(ev.FormAction); err == nil && u.Host != "" {
			out.Evidence["form_origin"] = u.Scheme + "://" + u.Host
		}
	}
	for k := range kinds {
		out.Kinds = append(out.Kinds, k)
	}
	sort.Strings(out.Kinds)
	return out
}

func fieldsOrEmpty(f []Field) []Field {
	if f == nil {
		return []Field{}
	}
	return f
}

// crossSite reports whether a form's action is on another site than its page. A site is taken as
// the host's last two labels, or three under a two-letter country domain with a short second label
// (example.co.uk): an approximation of the public suffix list, erring towards calling two hosts
// different, which costs a question and never a missed one.
func crossSite(pageOrigin, action string) bool {
	p, err1 := url.Parse(pageOrigin)
	a, err2 := url.Parse(action)
	if err1 != nil || err2 != nil || a.Host == "" {
		return false
	}
	return Site(p.Hostname()) != Site(a.Hostname())
}

// Site is a host's registrable part, approximately.
func Site(host string) string {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	labels := strings.Split(host, ".")
	if len(labels) <= 2 || isIP(host) {
		return host
	}
	n := 2
	tld, second := labels[len(labels)-1], labels[len(labels)-2]
	if len(tld) == 2 && len(second) <= 3 {
		n = 3
	}
	return strings.Join(labels[len(labels)-n:], ".")
}

func isIP(host string) bool {
	return strings.Count(host, ".") == 3 && strings.Trim(host, "0123456789.") == "" || strings.Contains(host, ":")
}
