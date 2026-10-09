//go:build !unix && !windows

package sidechan

import "os"

func readableDir(p string) bool {
	d, err := os.Open(p)
	if err != nil {
		return false
	}
	d.Close()
	return true
}

func volumes() []Place { return []Place{{Name: "/", Path: "/", Kind: "volume"}} }
