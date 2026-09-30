package main

// What went wrong, in the operator's language.
//
// Everything the launcher runs is a program that reports in English and in its own vocabulary: a Go
// error about a DNS lookup, a compose command that could not reach a daemon, a port that is taken.
// On the progress page that text is not decoration beside an explanation — it *is* the explanation,
// on the one screen where somebody is stuck and has to decide what to do next. So the handful of
// failures that actually happen on a first run are recognised here and answered with a sentence
// that says what to do, and the original text is kept on the page behind the disclosure for whoever
// wants it.
//
// A failure nobody recognised keeps the behaviour it had: the raw text, which is a visible gap
// rather than a silent one.

import "strings"

// failureKeys are the sentences, in the order they are tried. Docker comes before the network
// because "cannot connect to the Docker daemon" is a connection failure that has nothing to do with
// the internet, and a disk that is full before anything else because it makes every other symptom.
var failureKeys = []struct {
	key   string
	marks []string
}{
	{"trouble.disk", []string{"no space left on device", "disk quota exceeded", "not enough free space"}},
	{"trouble.docker", []string{
		"cannot connect to the docker daemon",
		"is the docker daemon running",
		"docker daemon is not running",
		"error during connect",
		"docker: command not found",
		"executable file not found in $path: docker",
	}},
	{"trouble.port", []string{"address already in use", "port is already allocated", "bind: permission denied"}},
	{"trouble.network", []string{
		"no such host",
		"temporary failure in name resolution",
		"network is unreachable",
		"i/o timeout",
		"tls handshake timeout",
		"connection reset by peer",
		"could not be downloaded",
		"proxyconnect tcp",
	}},
}

// FailureKey names the sentence that explains a failure, or the empty string when nothing here
// recognises it. The comparison is on a lowered copy because the same condition reaches the
// launcher capitalised by one program and not by another.
func FailureKey(failure string) string {
	text := strings.ToLower(failure)
	if strings.TrimSpace(text) == "" {
		return ""
	}
	if key := fenceFailureKey(text); key != "" {
		return key
	}
	for _, one := range failureKeys {
		for _, mark := range one.marks {
			if strings.Contains(text, mark) {
				return one.key
			}
		}
	}
	return ""
}
