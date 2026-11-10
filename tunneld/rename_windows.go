package main

import (
	"errors"

	"golang.org/x/sys/windows"
)

// heldByReader is the error a rename over a file another handle holds open gives.
func heldByReader(err error) bool {
	return errors.Is(err, windows.ERROR_ACCESS_DENIED) || errors.Is(err, windows.ERROR_SHARING_VIOLATION)
}
