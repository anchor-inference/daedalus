package sensitive

import (
	"slices"
	"strings"
	"testing"
)

func TestClassificationTable(t *testing.T) {
	origin := "https://shop.example.com"
	for _, c := range []struct {
		name   string
		action string
		ev     Evidence
		submit bool
		want   string
	}{
		{"buy button", "click", Evidence{Name: "Buy now", Role: "button", PageOrigin: origin}, false, "purchase"},
		{"place order", "click", Evidence{Name: "Place order", PageOrigin: origin}, false, "purchase"},
		{"checkout", "click", Evidence{Name: "Proceed to checkout", PageOrigin: origin}, false, "purchase"},
		{"russian buy", "click", Evidence{Name: "Купить сейчас", PageOrigin: origin}, false, "purchase"},
		{"russian order with ending", "click", Evidence{Name: "Оформить заказ", PageOrigin: origin}, false, "purchase"},
		{"russian pay imperative", "click", Evidence{Name: "Оплатите", PageOrigin: origin}, false, "purchase"},
		{"payment form continue", "click", Evidence{Name: "Continue", Submits: true, Form: true, Payment: true, PageOrigin: origin}, false, "purchase"},
		{"payment heading", "click", Evidence{Name: "Continue", Heading: "Payment", Submits: true, Form: true, PageOrigin: origin}, false, "purchase"},
		{"login submit", "click", Evidence{Name: "Sign in", Submits: true, Form: true, Password: true, PageOrigin: origin}, false, "credentials"},
		{"login by enter", "type", Evidence{Name: "Password", Submits: true, Form: true, Password: true, PageOrigin: origin}, true, "credentials"},
		{"otp", "press", Evidence{Name: "Code", Submits: true, Form: true, OTP: true, PageOrigin: origin}, false, "credentials"},
		{"send", "click", Evidence{Name: "Send", PageOrigin: origin}, false, "send"},
		{"publish", "click", Evidence{Name: "Publish post", PageOrigin: origin}, false, "send"},
		{"russian send", "click", Evidence{Name: "Отправить", PageOrigin: origin}, false, "send"},
		{"delete", "click", Evidence{Name: "Delete repository", PageOrigin: origin}, false, "destroy"},
		{"russian delete", "click", Evidence{Name: "Удалить", PageOrigin: origin}, false, "destroy"},
		{"unsubscribe", "click", Evidence{Name: "Cancel subscription", PageOrigin: origin}, false, "destroy"},
		{"cookies accept", "click", Evidence{Name: "Accept all cookies", PageOrigin: origin}, false, "accept"},
		{"cookies agree ru", "click", Evidence{Name: "Принять все", PageOrigin: origin}, false, "accept"},
		{"cookies reject", "click", Evidence{Name: "Reject all", PageOrigin: origin}, false, ""},
		{"only necessary", "click", Evidence{Name: "Accept only necessary", PageOrigin: origin}, false, ""},
		{"only essential", "click", Evidence{Name: "Accept only essential cookies", PageOrigin: origin}, false, ""},
		{"necessary cookies", "click", Evidence{Name: "Allow necessary cookies", PageOrigin: origin}, false, ""},
		{"continue without", "click", Evidence{Name: "Continue without accepting", PageOrigin: origin}, false, ""},
		{"reject all ru", "click", Evidence{Name: "Отклонить все", PageOrigin: origin}, false, ""},
		{"refuse ru", "click", Evidence{Name: "Отказаться от всех", PageOrigin: origin}, false, ""},
		{"only necessary ru", "click", Evidence{Name: "Принять только необходимые", PageOrigin: origin}, false, ""},
		{"only required ru", "click", Evidence{Name: "Разрешить только обязательные", PageOrigin: origin}, false, ""},
		{"without accepting ru", "click", Evidence{Name: "Продолжить без принятия", PageOrigin: origin}, false, ""},
		{"accept all ru still asks", "click", Evidence{Name: "Принять все cookie", PageOrigin: origin}, false, "accept"},
		{"newsletter subscribe", "click", Evidence{Name: "Subscribe", Submits: true, Form: true, PageOrigin: origin}, false, ""},
		{"newsletter subscribe ru", "click", Evidence{Name: "Подписаться на рассылку", PageOrigin: origin}, false, ""},
		{"paid subscribe", "click", Evidence{Name: "Subscribe — $9.99/month", PageOrigin: origin}, false, "purchase"},
		{"paid subscribe ru", "click", Evidence{Name: "Подписаться за 299 ₽ в месяц", PageOrigin: origin}, false, "purchase"},
		{"subscribe on a payment form", "click", Evidence{Name: "Subscribe", Submits: true, Form: true, Payment: true, PageOrigin: origin}, false, "purchase"},
		{"a refusal word does not hide a purchase", "click", Evidence{Name: "Reject returns and buy now", PageOrigin: origin}, false, "purchase"},
		{"file input", "click", Evidence{Name: "Choose file", File: true, PageOrigin: origin}, false, "upload"},
		{"upload action", "upload", Evidence{Name: "Attachment", PageOrigin: origin}, false, "upload"},
		{"cross-site post", "click", Evidence{Name: "Go", Submits: true, Form: true, FormMethod: "post", FormAction: "https://collector.example.net/x", PageOrigin: origin}, false, "cross_origin_post"},
		{"same-site post", "click", Evidence{Name: "Go", Submits: true, Form: true, FormMethod: "post", FormAction: "https://api.example.com/x", PageOrigin: origin}, false, ""},
		{"cross-site get", "click", Evidence{Name: "Search", Submits: true, Form: true, FormMethod: "get", FormAction: "https://search.example.org/", PageOrigin: origin}, false, ""},
		{"plain link", "click", Evidence{Name: "Running shoes", Role: "link", PageOrigin: origin}, false, ""},
		{"word inside a word", "click", Evidence{Name: "Paypal help and Spaying", PageOrigin: origin}, false, ""},
		{"hover is never sensitive", "hover", Evidence{Name: "Buy now", PageOrigin: origin}, false, ""},
		{"typing without submit", "type", Evidence{Name: "Password", Submits: true, Form: true, Password: true, PageOrigin: origin}, false, ""},
	} {
		got := Classify(c.action, c.ev, c.submit)
		if c.want == "" {
			if len(got.Kinds) != 0 {
				t.Errorf("%s: %v, want none", c.name, got.Kinds)
			}
			continue
		}
		if !slices.Contains(got.Kinds, c.want) {
			t.Errorf("%s: %v, want %s", c.name, got.Kinds, c.want)
		}
	}
}

func TestEvidenceNamesTheWords(t *testing.T) {
	r := Classify("click", Evidence{Name: "Buy now", PageOrigin: "https://a.example", Form: true, FormAction: "https://a.example/cart"}, false)
	words, _ := r.Evidence["words"].([]string)
	if !slices.Contains(words, "buy now") && !slices.Contains(words, "buy") && !slices.ContainsFunc(words, func(w string) bool { return strings.EqualFold(w, "Buy now") }) {
		t.Fatalf("words: %v", r.Evidence)
	}
	if r.Evidence["form_origin"] != "https://a.example" {
		t.Fatalf("form origin: %v", r.Evidence)
	}
}

// The host holds a POST form's submission for the operator by these two, so they are there for every
// action on a form's element, the method lower-cased and "get" when the page gave none.
func TestEvidenceSaysHowAFormIsSent(t *testing.T) {
	for _, c := range []struct {
		name       string
		ev         Evidence
		submits    bool
		method     any
		withMethod bool
	}{
		{"a post form's submit", Evidence{Name: "Save", Form: true, FormMethod: "POST", Submits: true}, true, "post", true},
		{"a form without a method", Evidence{Name: "Search", Form: true, Submits: true}, true, "get", true},
		{"a field of a form, not submitting", Evidence{Name: "Email", Form: true, FormMethod: "post"}, false, "post", true},
		{"a button outside any form", Evidence{Name: "Open"}, false, nil, false},
	} {
		r := Classify("click", c.ev, false)
		if r.Evidence["submits"] != c.submits {
			t.Errorf("%s: submits %v", c.name, r.Evidence["submits"])
		}
		method, ok := r.Evidence["form_method"]
		if ok != c.withMethod || method != c.method {
			t.Errorf("%s: form_method %v (%v)", c.name, method, ok)
		}
	}
}

func TestSite(t *testing.T) {
	for host, want := range map[string]string{
		"shop.example.com":   "example.com",
		"example.com":        "example.com",
		"a.b.example.co.uk":  "example.co.uk",
		"www.example.de":     "example.de",
		"127.0.0.1":          "127.0.0.1",
		"sub.example.com.":   "example.com",
		"localhost":          "localhost",
		"shop.example.co.jp": "example.co.jp",
	} {
		if got := Site(host); got != want {
			t.Errorf("%s: %s, want %s", host, got, want)
		}
	}
}
