package main

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// privateDirProblem says what lets someone other than this user, the system or the administrators
// into the folder, or "" when nobody else gets in: Windows' form of a 0o700 folder.
func privateDirProblem(dir string) string {
	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return err.Error()
	}
	control, _, err := sd.Control()
	if err != nil {
		return err.Error()
	}
	if control&windows.SE_DACL_PROTECTED == 0 {
		return "the folder inherits its parent's access list: " + sd.String()
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
