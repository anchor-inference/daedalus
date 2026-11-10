package main

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// privateProblem says what lets someone other than this user, the system or the administrators
// into a folder or file, or "" when nobody else gets in: Windows' form of 0o700 and 0o600.
func privateProblem(path string) string {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return err.Error()
	}
	control, _, err := sd.Control()
	if err != nil {
		return err.Error()
	}
	if control&windows.SE_DACL_PROTECTED == 0 {
		return "it inherits its parent's access list: " + sd.String()
	}
	dacl, _, err := sd.DACL()
	if err != nil || dacl == nil {
		return fmt.Sprintf("no access list, which lets everyone in: %v", err)
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err.Error()
	}
	system, _ := windows.CreateWellKnownSid(windows.WinLocalSystemSid)
	admins, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	for i := uint32(0); i < uint32(dacl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, i, &ace); err != nil {
			return err.Error()
		}
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE {
			continue
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if !sid.Equals(user.User.Sid) && !sid.Equals(system) && !sid.Equals(admins) {
			return "it lets " + sid.String() + " in: " + sd.String()
		}
	}
	return ""
}
