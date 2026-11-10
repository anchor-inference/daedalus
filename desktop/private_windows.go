package main

import (
	"os"

	"golang.org/x/sys/windows"
)

// restrictToUser gives a folder or a file a protected access list naming this user, the system and
// the administrators, and nobody it would otherwise inherit from its parent. Windows ignores the
// 0o700 and 0o600 the launcher creates things with, so without this a secret gets whatever its
// parent grants: under %LOCALAPPDATA% that is already the same three, but a data folder or a
// DAEDALUS_LOCAL_ROOT on a second drive inherits that drive's root, which lets every signed-in user
// read it. A folder's entries are inherited by what is made inside, so a file written into it later,
// or renamed into it from a scratch file beside it, is covered too.
func restrictToUser(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	inherit := ""
	if info.IsDir() {
		inherit = "OICI"
	}
	sddl := "D:P"
	for _, sid := range []string{user.User.Sid.String(), "SY", "BA"} {
		sddl += "(A;" + inherit + ";FA;;;" + sid + ")"
	}
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}
