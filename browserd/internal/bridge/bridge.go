package bridge

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The bridge's own bye reasons, sent to the extension when there is no daemon to carry it to: the
// popup says "Daedalus is not running on this computer" for the first, and shows the message of the
// second.
const (
	ByeNotRunning  = "not_running"
	ByeUnavailable = "unavailable"
)

// Options are what one bridge runs with.
type Options struct {
	Origin string
	RunDir string
	// Allowed are the extension ids the bridge carries.
	Allowed []string
	// Dial connects to the daemon; nil dials the endpoint as it is.
	Dial func(network, address string) (net.Conn, error)
}

// Main is `browserd bridge <origin> [flags]`: its exit status. Chrome reads nothing from stderr but
// keeps it in its own log, where a refusal is worth finding.
func Main(args []string, stdin io.Reader, stdout io.Writer, stderr io.Writer) int {
	o, err := parse(args, os.Getenv)
	if err != nil {
		fmt.Fprintln(stderr, "browserd bridge:", err)
		return 2
	}
	if err := Run(o, stdin, stdout); err != nil {
		fmt.Fprintln(stderr, "browserd bridge:", err)
		return 1
	}
	return 0
}

// parse reads the origin and the bridge's flags. Chrome starts the host with the origin first and,
// on Windows, --parent-window=<handle> after it; a launcher's wrapper puts its own flags (--run-dir)
// before the origin. So the flags before the origin are the bridge's, and whatever follows it is
// Chrome's and ignored.
func parse(args []string, getenv func(string) string) (Options, error) {
	var o Options
	at := -1
	for i, a := range args {
		if IsOrigin(a) {
			at = i
			break
		}
	}
	if at < 0 {
		return o, errors.New("no extension origin, chrome-extension://<id>/, among the arguments")
	}
	o.Origin = args[at]
	fs := flag.NewFlagSet("bridge", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var data string
	var allow multi
	fs.StringVar(&o.RunDir, "run-dir", "", "the user daemon's run directory")
	fs.StringVar(&data, "data", "", "the installation's data folder, when not the standard one")
	fs.Var(&allow, "allow-extension", "an extension id to carry besides the pinned ones (repeatable)")
	fs.String("parent-window", "", "Chrome's window, on Windows; unused")
	if err := fs.Parse(args[:at]); err != nil {
		return o, err
	}
	if fs.NArg() > 0 {
		return o, fmt.Errorf("unexpected argument %q before the origin", fs.Arg(0))
	}
	exe, _ := os.Executable()
	fc, err := readConfig(exe)
	if err != nil {
		return o, err
	}
	o.Allowed = append(append(append([]string(nil), PinnedExtensionIDs...), fc.ExtensionIDs...), allow...)
	for _, id := range strings.Split(getenv(ExtensionIDsEnv), ",") {
		if id = strings.TrimSpace(id); id != "" {
			o.Allowed = append(o.Allowed, id)
		}
	}
	switch {
	case o.RunDir != "":
	case getenv(RunDirEnv) != "":
		o.RunDir = getenv(RunDirEnv)
	case fc.RunDir != "":
		o.RunDir = fc.RunDir
	default:
		if o.RunDir, err = DefaultRunDir(data, getenv); err != nil {
			return o, fmt.Errorf("where the daemon is: %w", err)
		}
	}
	return o, nil
}

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ",") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

// checkOrigin refuses an extension that is not one of the allowed ones. Chrome starts the host only
// for the extensions its manifest lists, so a refusal here means the manifest and the bridge
// disagree, or someone runs the bridge by hand.
func checkOrigin(origin string, allowed []string) error {
	id, err := OriginID(origin)
	if err != nil {
		return err
	}
	for _, a := range allowed {
		if a == id {
			return nil
		}
	}
	return fmt.Errorf("extension %s is not one the bridge carries", id)
}

// Run carries one extension's relay until either side ends it.
func Run(o Options, stdin io.Reader, stdout io.Writer) error {
	if err := checkOrigin(o.Origin, o.Allowed); err != nil {
		return err
	}
	bye := func(reason, message string) {
		b, _ := json.Marshal(relay.Bye{T: relay.TBye, Reason: reason, Message: message})
		_ = WriteMessage(stdout, b)
	}
	nc, channel, err := connect(o)
	if err != nil {
		var ue *unavailable
		if errors.As(err, &ue) {
			bye(ByeUnavailable, ue.Error())
		} else {
			bye(ByeNotRunning, "Daedalus is not running on this computer")
		}
		return err
	}
	defer nc.Close()
	var wmu sync.Mutex
	send := func(payload []byte) error {
		wmu.Lock()
		defer wmu.Unlock()
		return wire.WriteFrame(nc, channel, payload)
	}
	done := make(chan error, 2)
	// Chrome to the daemon: each message as the pieces the relay channel carries. Chrome closing the
	// pipe (the extension disconnected, the worker or Chrome ended) closes the channel.
	go func() {
		for {
			msg, err := ReadMessage(stdin)
			if err != nil {
				wmu.Lock()
				_ = wire.WriteFrame(nc, channel, nil)
				wmu.Unlock()
				if errors.Is(err, io.EOF) {
					err = nil
				}
				done <- err
				return
			}
			for _, p := range relay.Split(msg, wire.MaxPayload) {
				if err := send(p); err != nil {
					done <- err
					return
				}
			}
		}
	}()
	// The daemon to Chrome: the pieces joined. The daemon closing the channel (its bye sent, or it
	// stopped) ends the bridge, and Chrome tells the extension the port disconnected.
	go func() {
		join := relay.Joiner{Max: relay.MaxToExtension}
		for {
			f, err := wire.ReadFrame(nc)
			if err != nil {
				done <- nil
				return
			}
			if f.Channel != channel {
				continue
			}
			if len(f.Payload) == 0 {
				done <- nil
				return
			}
			msg, err := join.Add(f.Payload)
			if err != nil {
				done <- fmt.Errorf("from the daemon: %w", err)
				return
			}
			if msg == nil {
				continue
			}
			if err := WriteMessage(stdout, msg); err != nil {
				done <- err
				return
			}
			// A bye is the relay's last frame: the bridge goes at once, which is how a stopping
			// daemon knows its bye was taken.
			if t, _ := relay.Type(msg); t == relay.TBye {
				done <- nil
				return
			}
		}
	}()
	return <-done
}

// unavailable is a daemon that answered and refused the relay: one of the wrong backend, or a
// relay.open it did not take.
type unavailable struct{ msg string }

func (u *unavailable) Error() string { return u.msg }

// connect dials the user daemon, presents its token and opens the relay channel.
func connect(o Options) (net.Conn, uint32, error) {
	network, address, token, err := server.ReadEndpoint(o.RunDir)
	if err != nil {
		return nil, 0, fmt.Errorf("no daemon in %s: %w", o.RunDir, err)
	}
	dial := o.Dial
	if dial == nil {
		dial = func(network, address string) (net.Conn, error) {
			return net.DialTimeout(network, address, 5*time.Second)
		}
	}
	nc, err := dial(network, address)
	if err != nil {
		return nil, 0, err
	}
	fail := func(err error) (net.Conn, uint32, error) {
		nc.Close()
		return nil, 0, err
	}
	_ = nc.SetDeadline(time.Now().Add(10 * time.Second))
	if err := wire.WriteFrame(nc, wire.ControlChannel, []byte(token)); err != nil {
		return fail(err)
	}
	req, _ := json.Marshal(wire.Request{JSONRPC: "2.0", ID: json.RawMessage("1"), Method: "relay.open",
		Params: json.RawMessage(`{"transport":"native"}`)})
	if err := wire.WriteFrame(nc, wire.ControlChannel, req); err != nil {
		return fail(err)
	}
	// The hello notification comes first, then the answer; anything else on channel 0 is skipped.
	for {
		f, err := wire.ReadFrame(nc)
		if err != nil {
			return fail(fmt.Errorf("the daemon's handshake: %w", err))
		}
		if f.Channel != wire.ControlChannel {
			continue
		}
		var resp wire.Response
		if json.Unmarshal(f.Payload, &resp) != nil || string(resp.ID) != "1" {
			continue
		}
		if resp.Error != nil {
			return fail(&unavailable{msg: resp.Error.Message})
		}
		var r struct {
			Channel uint32 `json:"channel"`
		}
		if err := json.Unmarshal(resp.Result, &r); err != nil || r.Channel == 0 {
			return fail(fmt.Errorf("relay.open answered %s", resp.Result))
		}
		_ = nc.SetDeadline(time.Time{})
		return nc, r.Channel, nil
	}
}
