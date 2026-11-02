package auth_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/fakeserver"
)

func TestPairingHandsTheTokenOnce(t *testing.T) {
	srv := fakeserver.New()
	defer srv.Close()
	c := client.New(srv.URL, "")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	ch, err := auth.Start(ctx, c, auth.ThisDevice("laptop", "test"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(ch.VerificationURIComplete, ch.UserCode) {
		t.Errorf("challenge: %+v", ch)
	}
	go func() {
		time.Sleep(1200 * time.Millisecond)
		srv.Approve(ch.UserCode)
	}()
	approved, err := auth.Wait(ctx, c, ch)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(approved.Token, "dcl_") || approved.Device.Name != "laptop" {
		t.Fatalf("%+v", approved)
	}
	// The token works, and a second poll of the same request gets nothing.
	if _, err := client.New(srv.URL, approved.Token).Me(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := auth.Wait(ctx, c, ch); !errors.Is(err, auth.ErrExpired) {
		t.Errorf("the token was handed out twice: %v", err)
	}
}

func TestPairingDenied(t *testing.T) {
	srv := fakeserver.New()
	defer srv.Close()
	c := client.New(srv.URL, "")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	ch, err := auth.Start(ctx, c, auth.ThisDevice("", "test"))
	if err != nil {
		t.Fatal(err)
	}
	srv.Deny(ch.UserCode)
	if _, err := auth.Wait(ctx, c, ch); !errors.Is(err, auth.ErrDenied) {
		t.Fatalf("%v", err)
	}
}

func TestFileStoreIsOwnerOnly(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(auth.EnvStore, "file")
	t.Setenv(auth.EnvToken, "")
	s, err := auth.OpenStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if s.Sealed() {
		t.Error("a file claims to be sealed")
	}
	if _, err := s.Get("default"); !errors.Is(err, auth.ErrNotFound) {
		t.Fatalf("an empty store: %v", err)
	}
	if err := s.Set("default", "dcl_x"); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		st, err := os.Stat(filepath.Join(dir, "credentials.json"))
		if err != nil || st.Mode().Perm() != 0o600 {
			t.Fatalf("mode %v, %v", st.Mode(), err)
		}
	}
	token, source, err := auth.Token(s, "default")
	if err != nil || token != "dcl_x" || !strings.Contains(source, "file") {
		t.Fatalf("%q %q %v", token, source, err)
	}
	t.Setenv(auth.EnvToken, "from-env")
	if token, _, _ := auth.Token(s, "default"); token != "from-env" {
		t.Errorf("the environment did not win: %q", token)
	}
	if err := s.Delete("default"); err != nil {
		t.Fatal(err)
	}
	if err := s.Delete("default"); !errors.Is(err, auth.ErrNotFound) {
		t.Errorf("a second delete: %v", err)
	}
}

func TestQRDrawsAScannableSquare(t *testing.T) {
	out, err := auth.QR("http://127.0.0.1:8765/app/?pair-device=ABCD-EFGH", false)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimRight(out, "\n"), "\n")
	width := len([]rune(lines[0]))
	for _, l := range lines {
		if len([]rune(l)) != width {
			t.Fatalf("ragged rows: %d and %d", width, len([]rune(l)))
		}
	}
	// Two modules per row of text: the square is about twice as wide as it is tall.
	if width < 2*len(lines)-4 || width > 2*len(lines)+4 {
		t.Errorf("%d rows of %d columns", len(lines), width)
	}
}
