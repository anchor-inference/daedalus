package fakeserver

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/coder/websocket"
)

// RefuseRelay makes the next n relay connections fail at the handshake with a 503, which keeps the
// device offline for a while.
func (s *Server) RefuseRelay(n int) {
	s.mu.Lock()
	s.refuse = n
	s.mu.Unlock()
}

// DropRelay closes the current relay connection without a close frame, as a network drop would.
func (s *Server) DropRelay() {
	s.mu.Lock()
	conn := s.relay
	s.mu.Unlock()
	if conn != nil {
		_ = conn.CloseNow()
	}
}

// Hellos are the device.hello messages received, in order.
func (s *Server) Hellos() []Hello {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Hello{}, s.hellos...)
}

// Sent is how many times a request id was sent to the device.
func (s *Server) Sent(id string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sent[id]
}

// RelayUp reports whether a device is connected.
func (s *Server) RelayUp() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.relay != nil
}

// WaitRelay waits for a connected device.
func (s *Server) WaitRelay(ctx context.Context) bool {
	for {
		if s.RelayUp() {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(10 * time.Millisecond):
		}
	}
}

func (s *Server) relayHandler(w http.ResponseWriter, r *http.Request) {
	if s.deviceOf(r) == "" {
		writeJSON(w, 401, map[string]any{"detail": "no device", "code": "device_token_invalid"})
		return
	}
	s.mu.Lock()
	if s.refuse > 0 {
		s.refuse--
		s.mu.Unlock()
		writeJSON(w, 503, map[string]any{"detail": "not now"})
		return
	}
	s.mu.Unlock()
	if r.Header.Get("Origin") != "" {
		writeJSON(w, 403, map[string]any{"detail": "a web page cannot open the relay"})
		return
	}
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	conn.SetReadLimit(2 << 20)
	ctx := r.Context()
	_, data, err := conn.Read(ctx)
	if err != nil {
		return
	}
	var first struct {
		Method string `json:"method"`
		Params Hello  `json:"params"`
	}
	if json.Unmarshal(data, &first) != nil || first.Method != "device.hello" {
		_ = conn.Close(4400, "expected device.hello")
		return
	}
	var bound []map[string]string
	for _, ss := range s.Sessions() {
		bound = append(bound, map[string]string{"session_id": ss.ID, "cwd": ss.Cwd})
	}
	welcome, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "device.welcome",
		"params": map[string]any{"device_id": "dev-1", "contract": 1, "server_version": "test", "bound": bound}})
	if err := conn.Write(ctx, websocket.MessageText, welcome); err != nil {
		return
	}
	s.mu.Lock()
	if old := s.relay; old != nil {
		_ = old.Close(4409, "replaced")
	}
	s.relay = conn
	s.relayGen++
	s.hellos = append(s.hellos, first.Params)
	s.relayCond.Broadcast()
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		if s.relay == conn {
			s.relay = nil
			s.relayGen++
			s.relayCond.Broadcast()
		}
		s.mu.Unlock()
	}()
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			return
		}
		var msg struct {
			ID     *string         `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			rpcResponse
		}
		if json.Unmarshal(data, &msg) != nil {
			continue
		}
		if msg.Method == "exec.output" {
			var p struct {
				ID   string `json:"id"`
				Data string `json:"data"`
			}
			_ = json.Unmarshal(msg.Params, &p)
			s.mu.Lock()
			s.output[p.ID] += p.Data
			s.mu.Unlock()
			continue
		}
		if msg.Method == "device.guard" {
			var p struct {
				Guard string `json:"guard"`
			}
			_ = json.Unmarshal(msg.Params, &p)
			s.mu.Lock()
			s.guards = append(s.guards, p.Guard)
			s.mu.Unlock()
			continue
		}
		if msg.ID == nil {
			continue
		}
		s.mu.Lock()
		ch := s.calls[*msg.ID]
		s.mu.Unlock()
		if ch != nil {
			select {
			case ch <- msg.rpcResponse:
			default:
			}
		}
	}
}

// Call sends one request to the device and waits for its result, keeping the server's side of the
// disconnect rules: a call is held while the relay is down; after a drop, the next hello decides
// (done: collect with device.result; running: keep waiting; not listed: "unknown", never sent again).
func (s *Server) Call(ctx context.Context, id, method string, params map[string]any) (map[string]any, *RPCError, string) {
	ch := make(chan rpcResponse, 4)
	s.mu.Lock()
	s.calls[id] = ch
	// The server's own form of a device.result request's id.
	resultID := id + "/result"
	s.calls[resultID] = ch
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.calls, id)
		delete(s.calls, resultID)
		s.mu.Unlock()
	}()
	request, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	sentOn := -1
	for {
		conn, gen, hello := s.waitConn(ctx, sentOn)
		if conn == nil {
			return nil, nil, "cancelled"
		}
		if sentOn >= 0 && !s.ResendOnReconnect {
			// A reconnect after the call went out: the hello says what became of it.
			state := ""
			for _, k := range hello.Known {
				if k["id"] == id {
					state = k["state"]
				}
			}
			switch state {
			case "":
				return nil, nil, "unknown"
			case "done":
				ask, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": resultID, "method": "device.result", "params": map[string]any{"id": id, "_meta": params["_meta"]}})
				_ = conn.Write(ctx, websocket.MessageText, ask)
			}
		} else {
			if err := conn.Write(ctx, websocket.MessageText, request); err != nil {
				sentOn = gen
				continue
			}
			s.mu.Lock()
			s.sent[id]++
			s.mu.Unlock()
		}
		sentOn = gen
		select {
		case res := <-ch:
			if res.Error != nil {
				return nil, res.Error, "error"
			}
			var out map[string]any
			_ = json.Unmarshal(res.Result, &out)
			return out, nil, "ok"
		case <-s.connLost(gen):
		case <-ctx.Done():
			return nil, nil, "cancelled"
		}
	}
}

// waitConn waits for a relay connection newer than generation after, and returns it with its hello.
func (s *Server) waitConn(ctx context.Context, after int) (*websocket.Conn, int, Hello) {
	done := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			s.mu.Lock()
			s.relayCond.Broadcast()
			s.mu.Unlock()
		case <-done:
		}
	}()
	defer close(done)
	s.mu.Lock()
	defer s.mu.Unlock()
	for s.relay == nil || s.relayGen <= after {
		if ctx.Err() != nil {
			return nil, 0, Hello{}
		}
		s.relayCond.Wait()
	}
	return s.relay, s.relayGen, s.hellos[len(s.hellos)-1]
}

// connLost is closed when the relay connection of generation gen goes away.
func (s *Server) connLost(gen int) <-chan struct{} {
	out := make(chan struct{})
	go func() {
		s.mu.Lock()
		for s.relayGen == gen {
			s.relayCond.Wait()
		}
		s.mu.Unlock()
		close(out)
	}()
	return out
}

// Output is what the device streamed for a request with exec.output.
func (s *Server) Output(id string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.output[id]
}

// Cancel tells the device to end a running request, as the server does when the operator stops the
// turn.
func (s *Server) Cancel(ctx context.Context, id string) error {
	s.mu.Lock()
	conn := s.relay
	s.mu.Unlock()
	if conn == nil {
		return nil
	}
	msg, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "request.cancel", "params": map[string]any{"id": id}})
	return conn.Write(ctx, websocket.MessageText, msg)
}
