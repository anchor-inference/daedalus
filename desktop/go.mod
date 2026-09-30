module github.com/ascorblack/daedalus/desktop

go 1.25.0

// The launcher's first dependency outside the standard library: the operating system's own web
// view, wrapped. There is no release to pin — the project tags nothing — so the commit is what is
// pinned, and the vendored C++ header that comes with it is the whole of the implementation.
require github.com/webview/webview_go v0.0.0-20240831120633-6173450d4dd6

require golang.org/x/sys v0.44.0 // indirect
