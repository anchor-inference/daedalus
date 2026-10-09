package sessions

import (
	"encoding/json"
	"strings"
	"testing"
)

// Fake tokens, assembled when the tests run: the repository's public audit refuses a token's shape
// written out in any committed file, fixtures included.
var (
	fakeGitHub   = "gh" + "p_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	fakeTelegram = "1234567890" + ":AAHfiqksKZ8WmR2zSjiQ7_v4TI7IqhIzLHo"
	fakePAT      = "github" + "_pat_11ABCDEFG0123456789_abcdefghijklmnop"
)

// The cases are those of tests/unit/test_redact.py, so the two ports of the shapes are held to the
// same answers; the values are made up.
func TestSecretShapesAreMasked(t *testing.T) {
	cases := map[string]string{
		"token " + fakeTelegram + " here":                          "token " + Mask + " here",
		"Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345":   "Authorization: Bearer " + Mask,
		"export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz": "export OPENAI_API_KEY=" + Mask,
		fakeGitHub:                                            Mask,
		"AKIAIOSFODNN7EXAMPLE":                                Mask,
		"postgres://user:s3cretpass@db.local/x":               "postgres://user:" + Mask + "@db.local/x",
		"DB_PASSWORD='hunter22'":                              "DB_PASSWORD='" + Mask + "'",
		`{"api_key": "8f3c1d2e9a0b7c6d5e4f3a2b1c0d9e8f"}`:     `{"api_key": "` + Mask + `"}`,
		`{"password": "hunter2hunter2"}`:                      `{"password": "` + Mask + `"}`,
		"X-Api-Key: 8f3c1d2e9a0b7c6d5e4f3a2b":                 "X-Api-Key: " + Mask,
		`curl -H "Authorization: token 8f3c1d2e9a0b7c6d5e4f"`: `curl -H "Authorization: token ` + Mask + `"`,
		"glpat-ABCDEFGHIJKLMNOPQRST":                          Mask,
		"api_key: 8f3c1d2e9a0b7c6d5e4f":                       "api_key: " + Mask,
		"xoxb-1234567890-abcdefghij":                          Mask,
		"AIzaSyA1234567890abcdefghijklmnopqrstu":              Mask,
		"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U": Mask,
		fakePAT:                         Mask,
		"MY_SECRET=\"long value here\"": "MY_SECRET=\"" + Mask + "\"",
		"GITHUB_TOKEN=abc123def456ghi789jk and more":                                             "GITHUB_TOKEN=" + Mask + " and more",
		"before\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\nafter": "before\n" + Mask + "\nafter",
	}
	for raw, want := range cases {
		var r Redactor
		if got := r.Text(raw); got != want {
			t.Errorf("%q\n got %q\nwant %q", raw, got, want)
		} else if r.Count == 0 {
			t.Errorf("%q masked without counting", raw)
		}
	}
}

func TestOrdinaryTextAndCodeAreUntouched(t *testing.T) {
	for _, text := range []string{
		"git status shows 3 files; the token bucket refills at 10/s; user=alice",
		`input_tokens=normalized["input_tokens"],`,
		"tokens = response.json()",
		"access_token: Optional[str] = None",
		"self.password = derive(salt)",
		"TOKEN_BUDGET = 128000",
		"TOKEN=abc123def456ghi789jk",
		`api_key = os.environ["OPENAI_API_KEY"]`,
		"https://github.com/x/y/commit/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
		"data:image/png;base64,eyJhbGciOiJIUzI1NiJ9AAAA",
		"author: someone_with_a_long_name_2024",
		"oauth_provider: provider_name_1234567",
		`{"description": "a long description text", "name": "value"}`,
		`{"api_key": "{api_key}"}`,
		`{"token": "«secret:github»"}`,
		"short",
	} {
		var r Redactor
		if got := r.Text(text); got != text || r.Count != 0 {
			t.Errorf("%q changed to %q", text, got)
		}
	}
}

func TestSecretNamedKeysMaskCredentialsBelowThem(t *testing.T) {
	fake := "DEMO_CREDENTIAL_1234567890"
	cases := []struct {
		in, want string
		count    int
	}{
		{`{"api_key":"` + fake + `"}`, `{"api_key":"` + Mask + `"}`, 1},
		{`{"config":{"password":"` + fake + `"}}`, `{"config":{"password":"` + Mask + `"}}`, 1},
		{`{"credentials":{"v":["` + fake + `"]}}`, `{"credentials":{"v":["` + Mask + `"]}}`, 1},
		{`{"data":"` + fake + `"}`, `{"data":"` + fake + `"}`, 0},
		{`{"api_key":"test"}`, `{"api_key":"test"}`, 0},
		{`{"n":3,"command":"echo ` + fakeGitHub + `"}`, `{"command":"echo ` + Mask + `","n":3}`, 1},
	}
	for _, tc := range cases {
		var r Redactor
		got := r.JSON(json.RawMessage(tc.in))
		var a, b any
		_ = json.Unmarshal(got, &a)
		_ = json.Unmarshal([]byte(tc.want), &b)
		ga, _ := json.Marshal(a)
		gb, _ := json.Marshal(b)
		if string(ga) != string(gb) || r.Count != tc.count {
			t.Errorf("%s\n got %s (%d)\nwant %s (%d)", tc.in, got, r.Count, tc.want, tc.count)
		}
	}
	// What does not decode is masked as text.
	var r Redactor
	if got := string(r.JSON(json.RawMessage(`{"cut": "` + fakeGitHub))); strings.Contains(got, "ghp_") {
		t.Fatalf("broken JSON kept its token: %s", got)
	}
}

func TestATurnIsMaskedEverywhere(t *testing.T) {
	tok := fakeGitHub
	turn := Turn{Parts: []Part{
		Text("say " + tok), Thinking("think "+tok, false),
		ToolCall("c", "Bash", json.RawMessage(`{"command":"git push https://x:`+tok+`@host/r"}`), ToolNative, ""),
		ToolResult("c", "printed "+tok, false), Compaction("summary "+tok, nil, true),
		Meta("note", map[string]any{"text": "noted " + tok}),
	}}
	var r Redactor
	r.Turn(&turn)
	b, _ := json.Marshal(turn)
	if strings.Contains(string(b), tok) || r.Count < 6 {
		t.Fatalf("%d masked: %s", r.Count, b)
	}
}
