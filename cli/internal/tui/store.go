package tui

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Prefs are the person's settings for this interface, kept on this machine.
//
// They are local on purpose: a theme or an editor mode belongs to the terminal a person sits at, not
// to the server session, and the same session opened from a light terminal on a laptop and a dark one
// on a desktop should look right on both.
type Prefs struct {
	Theme string `json:"theme,omitempty"`
	Vim   bool   `json:"vim,omitempty"`
	// Inline turns fullscreen off: the transcript is printed into the terminal's own scrollback.
	Inline bool `json:"inline,omitempty"`
	// ShowThinking draws the model's reasoning in the transcript instead of one collapsed row.
	ShowThinking bool `json:"show_thinking,omitempty"`
	// Verbose draws tool results whole instead of collapsing them.
	Verbose bool `json:"verbose,omitempty"`
	// Notify rings the terminal when a turn ends or a request waits while the window is unfocused.
	Notify *bool `json:"notify,omitempty"`
	// Guard is the local guard the next session starts in.
	Guard string `json:"guard,omitempty"`
}

// NotifyOn is Notify with its default: on.
func (p Prefs) NotifyOn() bool { return p.Notify == nil || *p.Notify }

// Store keeps the prefs and the prompt history.
type Store interface {
	LoadPrefs() Prefs
	SavePrefs(Prefs) error
	// History is the prompts typed in this folder, oldest first.
	History(cwd string) []string
	AppendHistory(cwd, prompt string) error
}

// maxHistory bounds the history file. A person reaches for the last few hundred prompts; a file that
// grew without bound would be read whole at every start.
const maxHistory = 1000

// FileStore keeps prefs.json and history.jsonl in one directory.
type FileStore struct {
	Dir string
	mu  sync.Mutex
}

// DefaultStoreDir is the per-user configuration folder of the client.
func DefaultStoreDir() string {
	if dir := os.Getenv("DAEDALUS_CLI_HOME"); dir != "" {
		return dir
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(base, "daedalus-cli")
}

func (s *FileStore) LoadPrefs() Prefs {
	var prefs Prefs
	if s == nil || s.Dir == "" {
		return prefs
	}
	data, err := os.ReadFile(filepath.Join(s.Dir, "prefs.json"))
	if err != nil {
		return prefs
	}
	// A damaged file gives the defaults rather than an error: a settings file must never be the
	// reason the program does not start.
	_ = json.Unmarshal(data, &prefs)
	return prefs
}

func (s *FileStore) SavePrefs(prefs Prefs) error {
	if s == nil || s.Dir == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(prefs, "", "  ")
	if err != nil {
		return err
	}
	// Written beside and renamed over, so a crash mid-write leaves the old file rather than half of
	// a new one.
	tmp := filepath.Join(s.Dir, ".prefs.json.tmp")
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(s.Dir, "prefs.json"))
}

type historyEntry struct {
	Cwd  string    `json:"cwd"`
	Text string    `json:"text"`
	At   time.Time `json:"at"`
}

func (s *FileStore) readHistory() []historyEntry {
	f, err := os.Open(filepath.Join(s.Dir, "history.jsonl"))
	if err != nil {
		return nil
	}
	defer f.Close()
	var entries []historyEntry
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var entry historyEntry
		if json.Unmarshal(scanner.Bytes(), &entry) == nil && entry.Text != "" {
			entries = append(entries, entry)
		}
	}
	return entries
}

func (s *FileStore) History(cwd string) []string {
	if s == nil || s.Dir == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for _, entry := range s.readHistory() {
		if entry.Cwd == cwd {
			out = append(out, entry.Text)
		}
	}
	return out
}

func (s *FileStore) AppendHistory(cwd, prompt string) error {
	if s == nil || s.Dir == "" || strings.TrimSpace(prompt) == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return err
	}
	entries := append(s.readHistory(), historyEntry{Cwd: cwd, Text: prompt, At: time.Now().UTC()})
	if len(entries) > maxHistory {
		entries = entries[len(entries)-maxHistory:]
	}
	var b strings.Builder
	for _, entry := range entries {
		line, err := json.Marshal(entry)
		if err != nil {
			continue
		}
		b.Write(line)
		b.WriteByte('\n')
	}
	tmp := filepath.Join(s.Dir, ".history.jsonl.tmp")
	if err := os.WriteFile(tmp, []byte(b.String()), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(s.Dir, "history.jsonl"))
}

// MemoryStore keeps everything in memory, for tests and for a client with nowhere to write.
type MemoryStore struct {
	mu      sync.Mutex
	Prefs   Prefs
	Entries map[string][]string
}

func (s *MemoryStore) LoadPrefs() Prefs {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.Prefs
}

func (s *MemoryStore) SavePrefs(p Prefs) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Prefs = p
	return nil
}

func (s *MemoryStore) History(cwd string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.Entries[cwd]...)
}

func (s *MemoryStore) AppendHistory(cwd, prompt string) error {
	if strings.TrimSpace(prompt) == "" {
		return errors.New("empty prompt")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Entries == nil {
		s.Entries = map[string][]string{}
	}
	s.Entries[cwd] = append(s.Entries[cwd], prompt)
	return nil
}
