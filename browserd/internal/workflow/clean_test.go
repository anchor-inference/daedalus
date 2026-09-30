package workflow

import (
	"strings"
	"testing"
)

func TestCleanURLKeepsWhereAPageIsAndNeverHowToGetIn(t *testing.T) {
	cases := map[string]string{
		"https://shop.example/reports?period=last&keyword=shoes":                          "https://shop.example/reports?period=last&keyword=shoes",
		"https://someone:hunter2@shop.example/a#section":                                  "https://shop.example/a",
		"https://shop.example/login?next=/a&token=abcdef0123456789abcdef":                 "https://shop.example/login?next=%2Fa&token={token}",
		"https://shop.example/cb?code=4/0AX4XfWh&state=xyz":                               "https://shop.example/cb?code={code}&state={state}",
		"https://shop.example/s?api_key=k&X-Amz-Signature=deadbeef":                       "https://shop.example/s?api_key={api_key}&X-Amz-Signature={x_amz_signature}",
		"https://shop.example/u?email=someone%40example.com":                              "https://shop.example/u?email={email}",
		"https://shop.example/find?q=someone%40example.com":                               "https://shop.example/find?q={q}",
		"https://shop.example/reset/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d": "https://shop.example/reset/{token}",
		"https://shop.example/order/12345":                                                "https://shop.example/order/12345",
		"https://shop.example/c?phone=5551234567":                                         "https://shop.example/c?phone={phone}",
		"https://shop.example/c?ref=5551234567":                                           "https://shop.example/c?ref={ref}",
		"file:///etc/passwd":                                                              "",
		"javascript:alert(1)":                                                             "",
		"about:blank":                                                                     "about:blank",
	}
	for in, want := range cases {
		if got := CleanURL(in); got != want {
			t.Errorf("CleanURL(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSafeLiteralRefusesWhatLooksPersonal(t *testing.T) {
	for _, v := range []string{"blue shoes", "Monthly report", "", "Отчёт за сентябрь", "2026-09"} {
		if !SafeLiteral(v) {
			t.Errorf("%q should be kept", v)
		}
	}
	for _, v := range []string{
		"someone@example.com", "call me at +1 555 123 4567", "4111 1111 1111 1111", "sk-live-0123456789abcdefXYZ",
		"eyJhbGciOiJIUzI1NiJ9eyJzdWIiOiIxMjM0NTY3ODkwIn0", strings.Repeat("a", MaxLiteral+1),
	} {
		if SafeLiteral(v) {
			t.Errorf("%q should be a blank", v)
		}
	}
}

func TestSlotBaseNamesAFieldInItsOwnWords(t *testing.T) {
	cases := map[string]string{
		"Search":              "search",
		"Report period":       "report_period",
		"  Поиск по товарам ": "поиск_по_товарам",
		"":                    "text",
		"***":                 "text",
		"E-mail (work)":       "e_mail_work",
	}
	for in, want := range cases {
		if got := slotBase(in, "text"); got != want {
			t.Errorf("slotBase(%q) = %q, want %q", in, got, want)
		}
	}
	if got := slotBase(strings.Repeat("word ", 20), "text"); len([]rune(got)) > maxSlot {
		t.Errorf("a long name made a blank of %d characters", len([]rune(got)))
	}
}

func TestClipNeverCutsInsideACharacter(t *testing.T) {
	if got := clip("  Отчёт\n за   сентябрь  ", 100); got != "Отчёт за сентябрь" {
		t.Fatalf("clip folded to %q", got)
	}
	if got := clip("Отчёт за сентябрь", 6); got != "Отчёт…" {
		t.Fatalf("clip cut to %q", got)
	}
}

func TestScrubTakesAddressesAndNumbersOutOfAPagesWords(t *testing.T) {
	cases := map[string]string{
		"127.0.0.1:8080/site/steps.html?token=SECRETTOKEN1234567890abcdef&period=last#done": "127.0.0.1:8080/site/steps.html?token={token}&period=last",
		"Inbox (3) - someone@example.com - Mail":                                            "Inbox (3) - {email} - Mail",
		"Call +1 (555) 123-4567 today":                                                      "Call {number} today",
		"Card 4111 1111 1111 1111":                                                          "Card {number}",
		"Order 2026-09 of 12 items":                                                         "Order 2026-09 of 12 items",
		"Open https://shop.example/r?sig=abc":                                               "Open https://shop.example/r?sig={sig}",
		"Your key: sk-live-0123456789abcdefXYZ.":                                            "Your key: {token}",
		"Report of 2026-09-30 14:05":                                                        "Report of 2026-09-30 14:05",
		"Отчёт за сентябрь":                                                                 "Отчёт за сентябрь",
	}
	for in, want := range cases {
		if got := scrub(in); got != want {
			t.Errorf("scrub(%q) = %q, want %q", in, got, want)
		}
	}
}
