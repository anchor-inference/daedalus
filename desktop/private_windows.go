package main

import "golang.org/x/sys/windows"

// restrictToUser gives a folder a protected access list naming this user, the system and the
// administrators, and nobody it would otherwise inherit from its parent. Windows ignores the 0o700
// that MkdirAll was given, so without this the folder gets whatever its parent grants: under
// %LOCALAPPDATA% that is already the same three, but DAEDALUS_LOCAL_ROOT can put it on a second
// drive, whose root lets every signed-in user read it. The entries are inherited by what is made
// inside, so a file written into the folder later is covered too.
func restrictToUser(dir string) error {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString("D:P(A;OICI;FA;;;" + user.User.Sid.String() + ")(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil)
}
