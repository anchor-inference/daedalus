package desk

import (
	"testing"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
)

func TestProtectedAppsAreNamedByAnyIdentity(t *testing.T) {
	win := backend.App{ID: "pid:7", PID: 7, ExePath: `C:\Program Files\Daedalus\Daedalus.exe`}
	mac := backend.App{ID: "com.example.daedalus", BundleID: "com.example.daedalus", ExePath: "/Applications/Daedalus.app/Contents/MacOS/Daedalus"}
	x := backend.App{ID: "pid:9", PID: 9, WMClass: "daedalus.Daedalus", ExePath: "/opt/Daedalus/daedalus"}
	other := backend.App{ID: "pid:8", PID: 8, ExePath: "/usr/bin/gedit", WMClass: "gedit.Gedit"}
	for _, tc := range []struct {
		m    Matcher
		app  backend.App
		want bool
	}{
		{Matcher{ExeName: "daedalus"}, win, true}, // one entry covers Windows' .exe and case
		{Matcher{ExeName: "Daedalus.exe"}, x, true},
		{Matcher{ExeName: "daedalus"}, other, false},
		{Matcher{BundleID: "com.example.daedalus"}, mac, true},
		{Matcher{BundleID: "com.example.daedalus"}, win, false},
		{Matcher{WMClass: "daedalus"}, x, true},
		{Matcher{WMClass: "Daedalus"}, x, true},
		{Matcher{WMClass: "daedalus"}, other, false},
		{Matcher{PID: 7}, win, true},
		{Matcher{PID: 7, ExeName: "gedit"}, win, false}, // every field set must match
		{Matcher{}, win, false},                         // an empty matcher protects nothing, not everything
	} {
		if got := tc.m.Match(tc.app); got != tc.want {
			t.Errorf("%+v on %s: %v", tc.m, tc.app.ID, got)
		}
	}
}
