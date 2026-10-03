package rpc

// diskObservation is a point-in-time read from the terminal's own mount namespace. It is neither
// a reservation nor a filesystem quota; launch admission repeats it before provider entry.
type diskObservation struct {
	Available       bool   `json:"available"`
	Reason          string `json:"reason,omitempty"`
	FreeBytes       int64  `json:"free_bytes,omitempty"`
	DeviceID        string `json:"device_id,omitempty"`
	MountID         string `json:"mount_id,omitempty"`
	ObservedAt      string `json:"observed_at,omitempty"`
	PathDigest      string `json:"path_digest,omitempty"`
	WorkspaceExists bool   `json:"workspace_exists"`
}
