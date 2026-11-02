package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// fileStore keeps tokens in a file its owner alone may read. That protects against another user of
// the machine and nothing else: any process running as the operator can read it, and on Windows the
// mode bits do not express "owner only" at all, so there the profile directory's access list is the
// protection. Backend says so.
type fileStore struct{ path string }

func newFileStore(dir string) (Store, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create %s: %w", dir, err)
	}
	return &fileStore{path: filepath.Join(dir, "credentials.json")}, nil
}

func (s *fileStore) Backend() string {
	if runtime.GOOS == "windows" {
		return "file " + s.path + " (protected by the profile's permissions only)"
	}
	return "file " + s.path + " (mode 0600)"
}

func (s *fileStore) Sealed() bool { return false }

func (s *fileStore) Get(account string) (string, error) {
	items, err := s.load()
	if err != nil {
		return "", err
	}
	if v := items[account]; v != "" {
		return v, nil
	}
	return "", ErrNotFound
}

func (s *fileStore) Set(account, value string) error {
	items, err := s.load()
	if err != nil {
		return err
	}
	items[account] = value
	return s.store(items)
}

func (s *fileStore) Delete(account string) error {
	items, err := s.load()
	if err != nil {
		return err
	}
	if _, ok := items[account]; !ok {
		return ErrNotFound
	}
	delete(items, account)
	return s.store(items)
}

func (s *fileStore) load() (map[string]string, error) {
	raw, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]string{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", s.path, err)
	}
	items := map[string]string{}
	if err := json.Unmarshal(raw, &items); err != nil {
		// Starting over would greet a paired machine with an unexplained login prompt.
		return nil, fmt.Errorf("%s is unreadable: %w", s.path, err)
	}
	return items, nil
}

func (s *fileStore) store(items map[string]string) error {
	body, err := json.Marshal(items)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".credentials-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer func() { _ = os.Remove(name) }()
	// Restricted before anything is written, so the token is never on disk under a wider mode.
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(body); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, s.path)
}
