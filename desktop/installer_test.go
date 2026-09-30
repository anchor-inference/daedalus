package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// install.sh picks the same release the launcher's check would: the highest plain desktop-vX.Y.Z
// that is neither a draft nor a prerelease — whatever order the listing and its keys are in.
func TestTheInstallerPicksTheReleaseTheLauncherWould(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("install.sh is for macOS and Linux")
	}
	if _, err := exec.LookPath("curl"); err != nil {
		t.Skip("no curl")
	}
	githubOrder := `[
  {"url": "x", "author": {"login": "a", "id": 1}, "node_id": "n", "tag_name": "desktop-v0.12.1", "draft": false, "prerelease": false,
   "body": "notes mention \"tag_name\": \"desktop-v9.9.9\", in text", "assets": [{"name": "a.zip", "state": "uploaded"}]},
  {"url": "y", "author": {"login": "a"}, "tag_name": "desktop-v0.13.0", "draft": false, "prerelease": false, "assets": []},
  {"url": "z", "author": {"login": "a"}, "tag_name": "desktop-v0.14.0", "draft": true, "prerelease": false, "assets": []},
  {"url": "w", "author": {"login": "a"}, "tag_name": "desktop-v0.15.0", "draft": false, "prerelease": true, "assets": []},
  {"url": "v", "author": {"login": "a"}, "tag_name": "desktop-v0.16.0-rc1", "draft": false, "prerelease": false, "assets": []},
  {"url": "u", "author": {"login": "a"}, "tag_name": "desktop-v0.9.10", "draft": false, "prerelease": false, "assets": []}
]`
	sortedKeys, _ := json.Marshal([]map[string]any{
		{"prerelease": true, "tag_name": "desktop-v0.14.0"},
		{"prerelease": false, "tag_name": "desktop-v0.13.0"},
		{"prerelease": false, "tag_name": "desktop-v0.13.10"},
		{"prerelease": false, "tag_name": "desktop-v0.13.9"},
	})
	for name, c := range map[string]struct{ listing, want string }{
		"GitHub's own order, with text that looks like a tag": {githubOrder, "desktop-v0.13.0"},
		"keys sorted, versions compared as numbers":           {string(sortedKeys), "desktop-v0.13.10"},
	} {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/releases" {
					w.Write([]byte(c.listing))
					return
				}
				http.NotFound(w, r)
			}))
			defer server.Close()
			cmd := exec.Command("sh", "install.sh")
			cmd.Env = append(os.Environ(), "DAEDALUS_RELEASES_API="+server.URL, "DAEDALUS_DOWNLOAD_BASE="+server.URL+"/download",
				"DAEDALUS_DIR="+filepath.Join(t.TempDir(), "Daedalus"))
			out, _ := cmd.CombinedOutput()
			chosen := ""
			for _, line := range strings.Split(string(out), "\n") {
				if i := strings.Index(line, " from "); strings.HasPrefix(line, "Downloading ") && i > 0 {
					chosen = strings.TrimSuffix(strings.TrimSpace(line[i+len(" from "):]), "...")
				}
			}
			if chosen != c.want {
				t.Fatalf("install.sh chose %q, want %q\n%s", chosen, c.want, out)
			}
		})
	}
}
