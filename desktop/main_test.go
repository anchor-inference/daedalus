package main

import (
	"strings"
	"testing"
)

func TestParseArgsTakesTheCommandFromAnywhere(t *testing.T) {
	opts, err := parseArgs([]string{"logs", "-f"})
	if err != nil || opts.command != "logs" || !opts.follow {
		t.Fatalf("%+v %v", opts, err)
	}
	opts, err = parseArgs([]string{"--data", "/tmp/x", "status"})
	if err != nil || opts.command != "status" || opts.data != "/tmp/x" {
		t.Fatalf("%+v %v", opts, err)
	}
	opts, err = parseArgs([]string{"--port=9000", "--setup"})
	if err != nil || opts.port != 9000 || !opts.setup || opts.command != "" {
		t.Fatalf("%+v %v", opts, err)
	}
	if opts, _ := parseArgs(nil); opts.port != defaultPort {
		t.Fatalf("the default port is %d, got %d", defaultPort, opts.port)
	}
}

func TestParseArgsRefusesWhatItCannotMean(t *testing.T) {
	if _, err := parseArgs([]string{"--nonsense"}); err == nil {
		t.Fatal("an unknown flag is an error")
	}
	if _, err := parseArgs([]string{"start", "stop"}); err == nil {
		t.Fatal("two commands are an error")
	}
	if _, err := parseArgs([]string{"--port"}); err == nil {
		t.Fatal("a flag without its value is an error")
	}
	if _, err := parseArgs([]string{"--port", "abc"}); err == nil {
		t.Fatal("a port must be a number")
	}
	// A mistyped port that quietly became another one would leave the page somewhere unexpected.
	if _, err := parseArgs([]string{"--port", "8770abc"}); err == nil {
		t.Fatal("a port with something after it is an error")
	}
	if _, err := parseArgs([]string{"--port", "70000"}); err == nil {
		t.Fatal("a number that is not a port is an error")
	}
}

func TestUpdateStatusAndResolveAreSubcommandsOfUpdate(t *testing.T) {
	for argv, want := range map[string][2]string{
		"update":                 {"update", ""},
		"update status":          {"update", "status"},
		"update resolve":         {"update", "resolve"},
		"update resolve --apply": {"update", "resolve"},
	} {
		opts, err := parseArgs(strings.Fields(argv))
		if err != nil || opts.command != want[0] || opts.extra != want[1] {
			t.Fatalf("%q: %+v %v", argv, opts, err)
		}
		if opts.apply != strings.Contains(argv, "--apply") {
			t.Fatalf("%q: apply %v", argv, opts.apply)
		}
	}
	if _, err := parseArgs([]string{"update", "everything"}); err == nil {
		t.Fatal("an unknown word after update was taken")
	}
}
