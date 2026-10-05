//go:build linux

package sandbox

import (
	"bufio"
	"errors"
	"io"
	"os"
	"strings"

	"golang.org/x/sys/unix"
)

const maxProviderRequest = 4096

// ServeProviderConnect grants one tunnel to an exact provider authority. The dial function must
// pin the resolved endpoint; trusting a client-supplied CONNECT target would restore host access.
func ServeProviderConnect(client io.ReadWriteCloser, authority string, dial func() (io.ReadWriteCloser, error)) error {
	defer client.Close()
	if authority == "" || !strings.HasSuffix(authority, ":443") ||
		strings.ContainsAny(authority, " \t\r\n/@") || dial == nil {
		return errors.New("sandbox: invalid provider authority or dialer")
	}
	reader := bufio.NewReader(io.LimitReader(client, maxProviderRequest+1))
	line, err := reader.ReadString('\n')
	if err != nil || line != "CONNECT "+authority+" HTTP/1.1\r\n" {
		_, _ = io.WriteString(client, "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
		return errors.New("sandbox: provider target refused")
	}
	read := len(line)
	for {
		line, err = reader.ReadString('\n')
		read += len(line)
		if err != nil || read > maxProviderRequest {
			return errors.New("sandbox: provider request is incomplete or too large")
		}
		if line == "\r\n" {
			break
		}
		if !strings.HasSuffix(line, "\r\n") || strings.ContainsRune(line, 0) {
			return errors.New("sandbox: provider request header is invalid")
		}
	}
	upstream, err := dial()
	if err != nil {
		_, _ = io.WriteString(client, "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
		return err
	}
	defer upstream.Close()
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return err
	}
	// The CONNECT preface may share a read with the first TLS bytes, so forward the buffered
	// reader rather than reading the socket directly after parsing headers.
	up := make(chan struct{}, 1)
	go func() {
		_, _ = io.Copy(upstream, io.MultiReader(reader, client))
		if half, ok := upstream.(interface{ CloseWrite() error }); ok {
			_ = half.CloseWrite()
		}
		up <- struct{}{}
	}()
	_, downErr := io.Copy(client, upstream)
	if file, ok := client.(*os.File); ok {
		_ = unix.Shutdown(int(file.Fd()), unix.SHUT_RDWR)
	}
	_ = client.Close()
	_ = upstream.Close()
	<-up
	return downErr
}
