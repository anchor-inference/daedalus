//go:build !linux

package rpc

func diskPreflight(_ string) diskObservation {
	return diskObservation{Reason: "selected workspace mount observation is not implemented on this platform"}
}
