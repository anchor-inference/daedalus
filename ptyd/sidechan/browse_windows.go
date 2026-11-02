//go:build windows

package sidechan

import "os"

// readableDir reports whether a directory opens for listing: an access list decides it, so it is
// tried rather than computed.
func readableDir(p string) bool {
	d, err := os.Open(p)
	if err != nil {
		return false
	}
	d.Close()
	return true
}

// volumes is every drive letter that answers, "C:" for C:\.
func volumes() []Place {
	out := []Place{}
	for letter := 'A'; letter <= 'Z'; letter++ {
		root := string(letter) + `:\`
		if st, err := os.Stat(root); err == nil && st.IsDir() {
			out = append(out, Place{Name: string(letter) + ":", Path: root, Kind: "volume"})
		}
	}
	return out
}
