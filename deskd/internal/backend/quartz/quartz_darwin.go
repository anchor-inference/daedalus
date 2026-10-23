//go:build darwin && cgo

package quartz

/*
#cgo CFLAGS: -x objective-c -fobjc-arc -mmacosx-version-min=12.0
#cgo LDFLAGS: -mmacosx-version-min=12.0 -framework CoreGraphics -framework ApplicationServices -framework AppKit -framework CoreFoundation

// CGDisplayCreateImageForRect and NSApplicationActivateIgnoringOtherApps are deprecated in the
// macOS 14 and 15 SDKs and still work there. The warning is silenced rather than the calls replaced
// because the replacements change the program's shape: ScreenCaptureKit's SCScreenshotManager is
// asynchronous, needs a run loop and asks for a newer system, and is the capture to adopt next.
// The deployment target is pinned to macOS 12, the oldest the app supports: left to the compiler it
// follows the build machine, and on a macOS 15 runner the capture call stops being deprecated and
// becomes unavailable, which fails the build outright.
#pragma clang diagnostic ignored "-Wdeprecated-declarations"

#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <CoreFoundation/CoreFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// The one event source every synthetic event comes from. Its private state means the keys and
// buttons the operator physically holds are not merged into our events: with the combined or HID
// state, a Shift the operator happened to hold would capitalise text the agent types. It lives here
// rather than in Go because a CoreGraphics reference held in Go memory is a pointer the garbage
// collector must not see, and there is only ever one backend in the process.
static CGEventSourceRef dk_source = NULL;

static int dk_input_open(void) {
	if (dk_source != NULL) {
		return 1;
	}
	dk_source = CGEventSourceCreate(kCGEventSourceStatePrivate);
	if (dk_source == NULL) {
		return 0;
	}
	// By default the system ignores the operator's own mouse and keyboard for a quarter of a second
	// after each event we post. That would hide the operator taking over, and fight them for the
	// pointer, so the suppression is turned off.
	CGEventSourceSetLocalEventsSuppressionInterval(dk_source, 0.0);
	return 1;
}

static void dk_input_close(void) {
	if (dk_source != NULL) {
		CFRelease(dk_source);
		dk_source = NULL;
	}
}

static int dk_has_session(void) {
	CFDictionaryRef d = CGSessionCopyCurrentDictionary();
	if (d == NULL) {
		return 0;
	}
	CFRelease(d);
	return 1;
}

static int dk_screen_granted(void) { return CGPreflightScreenCaptureAccess() ? 1 : 0; }
static int dk_ax_trusted(void) { return AXIsProcessTrusted() ? 1 : 0; }
static int dk_listen_granted(void) { return CGPreflightListenEventAccess() ? 1 : 0; }
static void dk_request_screen(void) { CGRequestScreenCaptureAccess(); }
static void dk_request_listen(void) { CGRequestListenEventAccess(); }

static void dk_request_ax(void) {
	const void *keys[] = {kAXTrustedCheckOptionPrompt};
	const void *values[] = {kCFBooleanTrue};
	CFDictionaryRef options = CFDictionaryCreate(kCFAllocatorDefault, keys, values, 1,
		&kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
	if (options == NULL) {
		return;
	}
	AXIsProcessTrustedWithOptions(options);
	CFRelease(options);
}

// dk_stay_out_of_dock makes this process a background one before anything else touches AppKit.
// deskd sits in Daedalus.app/Contents/MacOS, so the system takes the application's Info.plist for
// its own: the first AppKit call (NSEvent, NSScreen, NSWorkspace below) checked it in as a second
// copy of Daedalus, an ordinary application with a Dock icon. It never runs an event loop and so
// never finishes launching, and that icon bounced for as long as the daemon ran. Prohibited is what
// LSBackgroundOnly would say if the daemon had a bundle of its own: no Dock icon, no menu bar, never
// activated. The grants are not affected: they follow the responsible process, the app.
static void dk_stay_out_of_dock(void) {
	@autoreleasepool {
		[NSApplication sharedApplication];
		[NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
	}
}

static double dk_double_click_interval(void) {
	double interval = 0;
	@autoreleasepool {
		interval = [NSEvent doubleClickInterval];
	}
	return interval;
}

typedef struct {
	uint32_t id;
	double x, y, w, h;
	double pixels_per_point;
	int main;
	int builtin;
} dk_display;

static int dk_displays(dk_display *out, int max) {
	CGDirectDisplayID ids[32];
	uint32_t n = 0;
	if (CGGetActiveDisplayList(32, ids, &n) != kCGErrorSuccess) {
		return -1;
	}
	CGDirectDisplayID mainID = CGMainDisplayID();
	int count = 0;
	for (uint32_t i = 0; i < n && count < max; i++) {
		CGRect b = CGDisplayBounds(ids[i]);
		double ppp = 1;
		// The display mode's pixel width over its point width is the backing scale; CGDisplayPixelsWide
		// would give the points again on a scaled mode.
		CGDisplayModeRef mode = CGDisplayCopyDisplayMode(ids[i]);
		if (mode != NULL) {
			size_t pw = CGDisplayModeGetPixelWidth(mode);
			size_t w = CGDisplayModeGetWidth(mode);
			if (pw > 0 && w > 0) {
				ppp = (double)pw / (double)w;
			}
			CGDisplayModeRelease(mode);
		}
		dk_display *d = &out[count++];
		d->id = ids[i];
		d->x = b.origin.x;
		d->y = b.origin.y;
		d->w = b.size.width;
		d->h = b.size.height;
		d->pixels_per_point = ppp;
		d->main = ids[i] == mainID;
		d->builtin = CGDisplayIsBuiltin(ids[i]) ? 1 : 0;
	}
	return count;
}

typedef struct {
	uint8_t *data;
	int w, h, stride;
	// status is 0 for a picture, 1 when the window server gave no image, 2 when drawing it failed.
	int status;
} dk_capture;

// dk_capture_rect captures rect, in points relative to the display's own top-left corner, into a
// premultiplied RGBA buffer the caller frees. Drawing into a bitmap of our own format, rather than
// reading the image's bytes, takes whatever layout the window server chose (BGRA, ten-bit, wide gamut)
// to one we know; drawing it into sRGB also converts the display's colour profile, which is what a
// picture shown to a model is assumed to be in.
static dk_capture dk_capture_rect(uint32_t display, double x, double y, double w, double h) {
	dk_capture r = {0};
	CGImageRef img = CGDisplayCreateImageForRect(display, CGRectMake(x, y, w, h));
	if (img == NULL) {
		r.status = 1;
		return r;
	}
	size_t pw = CGImageGetWidth(img);
	size_t ph = CGImageGetHeight(img);
	if (pw == 0 || ph == 0) {
		CGImageRelease(img);
		r.status = 1;
		return r;
	}
	size_t stride = pw * 4;
	uint8_t *buf = calloc(ph, stride);
	if (buf == NULL) {
		CGImageRelease(img);
		r.status = 2;
		return r;
	}
	CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
	if (space == NULL) {
		free(buf);
		CGImageRelease(img);
		r.status = 2;
		return r;
	}
	CGContextRef ctx = CGBitmapContextCreate(buf, pw, ph, 8, stride, space,
		(uint32_t)kCGImageAlphaPremultipliedLast | (uint32_t)kCGBitmapByteOrder32Big);
	CGColorSpaceRelease(space);
	if (ctx == NULL) {
		free(buf);
		CGImageRelease(img);
		r.status = 2;
		return r;
	}
	CGContextSetBlendMode(ctx, kCGBlendModeCopy);
	CGContextDrawImage(ctx, CGRectMake(0, 0, (CGFloat)pw, (CGFloat)ph), img);
	CGContextRelease(ctx);
	CGImageRelease(img);
	r.data = buf;
	r.w = (int)pw;
	r.h = (int)ph;
	r.stride = (int)stride;
	return r;
}

static char *dk_cfstring(CFTypeRef s) {
	if (s == NULL || CFGetTypeID(s) != CFStringGetTypeID()) {
		return NULL;
	}
	CFIndex length = CFStringGetLength((CFStringRef)s);
	CFIndex size = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
	char *buf = malloc((size_t)size);
	if (buf == NULL) {
		return NULL;
	}
	if (!CFStringGetCString((CFStringRef)s, buf, size, kCFStringEncodingUTF8)) {
		free(buf);
		return NULL;
	}
	return buf;
}

static char *dk_nsstring(NSString *s) {
	if (s == nil) {
		return NULL;
	}
	const char *u = [s UTF8String];
	return u != NULL ? strdup(u) : NULL;
}

// dk_display_name is the screen's name as System Settings shows it ("DELL U2720Q", "Built-in Retina
// Display"), or NULL. Core Graphics has no name for a display; AppKit's NSScreen does, keyed by the
// same CGDirectDisplayID under NSScreenNumber. The caller frees the result.
static char *dk_display_name(uint32_t id) {
	char *out = NULL;
	@autoreleasepool {
		if (@available(macOS 10.15, *)) {
			for (NSScreen *screen in [NSScreen screens]) {
				NSNumber *number = screen.deviceDescription[@"NSScreenNumber"];
				if (number != nil && number.unsignedIntValue == id) {
					out = dk_nsstring(screen.localizedName);
					break;
				}
			}
		}
	}
	return out;
}

static int dk_dict_number(CFDictionaryRef d, CFStringRef key, CFNumberType type, void *out) {
	CFTypeRef v = CFDictionaryGetValue(d, key);
	if (v == NULL || CFGetTypeID(v) != CFNumberGetTypeID()) {
		return 0;
	}
	return CFNumberGetValue((CFNumberRef)v, type, out) ? 1 : 0;
}

typedef struct {
	uint32_t number;
	int32_t pid;
	double x, y, w, h;
	char *title;
	char *owner;
} dk_window;

static void dk_windows_free(dk_window *list, int n) {
	if (list == NULL) {
		return;
	}
	for (int i = 0; i < n; i++) {
		free(list[i].title);
		free(list[i].owner);
	}
	free(list);
}

// dk_windows lists the on-screen windows of the normal layer, front to back, into a buffer the caller
// frees with dk_windows_free. Other layers are the menu bar, the Dock, overlays and status items,
// which no task means by "a window".
static int dk_windows(dk_window **out) {
	*out = NULL;
	CFArrayRef list = CGWindowListCopyWindowInfo(
		kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
	if (list == NULL) {
		return -1;
	}
	CFIndex n = CFArrayGetCount(list);
	if (n == 0) {
		CFRelease(list);
		return 0;
	}
	dk_window *items = calloc((size_t)n, sizeof(dk_window));
	if (items == NULL) {
		CFRelease(list);
		return -1;
	}
	int count = 0;
	for (CFIndex i = 0; i < n; i++) {
		CFTypeRef entry = CFArrayGetValueAtIndex(list, i);
		if (entry == NULL || CFGetTypeID(entry) != CFDictionaryGetTypeID()) {
			continue;
		}
		CFDictionaryRef d = (CFDictionaryRef)entry;
		int32_t layer = -1;
		if (!dk_dict_number(d, kCGWindowLayer, kCFNumberSInt32Type, &layer) || layer != 0) {
			continue;
		}
		double alpha = 1;
		if (dk_dict_number(d, kCGWindowAlpha, kCFNumberDoubleType, &alpha) && alpha <= 0) {
			continue;
		}
		int64_t number = 0;
		int32_t pid = 0;
		if (!dk_dict_number(d, kCGWindowNumber, kCFNumberSInt64Type, &number) ||
			!dk_dict_number(d, kCGWindowOwnerPID, kCFNumberSInt32Type, &pid)) {
			continue;
		}
		CFTypeRef bounds = CFDictionaryGetValue(d, kCGWindowBounds);
		CGRect rect;
		if (bounds == NULL || CFGetTypeID(bounds) != CFDictionaryGetTypeID() ||
			!CGRectMakeWithDictionaryRepresentation((CFDictionaryRef)bounds, &rect)) {
			continue;
		}
		if (rect.size.width < 1 || rect.size.height < 1) {
			continue;
		}
		dk_window *w = &items[count++];
		w->number = (uint32_t)number;
		w->pid = pid;
		w->x = rect.origin.x;
		w->y = rect.origin.y;
		w->w = rect.size.width;
		w->h = rect.size.height;
		// The title is withheld from a program without Screen Recording; an empty one is expected.
		w->title = dk_cfstring(CFDictionaryGetValue(d, kCGWindowName));
		w->owner = dk_cfstring(CFDictionaryGetValue(d, kCGWindowOwnerName));
	}
	CFRelease(list);
	*out = items;
	return count;
}

typedef struct {
	char *bundle;
	char *exe;
	char *name;
} dk_app;

static dk_app dk_app_info(int32_t pid) {
	dk_app a = {NULL, NULL, NULL};
	@autoreleasepool {
		NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:(pid_t)pid];
		if (app != nil) {
			a.bundle = dk_nsstring(app.bundleIdentifier);
			a.exe = dk_nsstring(app.executableURL.path);
			a.name = dk_nsstring(app.localizedName);
		}
	}
	return a;
}

static void dk_app_free(dk_app a) {
	free(a.bundle);
	free(a.exe);
	free(a.name);
}

// dk_focused_pid is the process of the application that has the keyboard, or -1. The Accessibility
// answer comes first because NSWorkspace's frontmostApplication is refreshed by notifications on the
// main run loop, which a daemon without one never runs: it would keep naming the app that was in
// front when deskd started.
static int32_t dk_focused_pid(void) {
	pid_t pid = -1;
	if (AXIsProcessTrusted()) {
		AXUIElementRef sys = AXUIElementCreateSystemWide();
		if (sys != NULL) {
			CFTypeRef app = NULL;
			if (AXUIElementCopyAttributeValue(sys, kAXFocusedApplicationAttribute, &app) == kAXErrorSuccess && app != NULL) {
				if (CFGetTypeID(app) == AXUIElementGetTypeID()) {
					AXUIElementGetPid((AXUIElementRef)app, &pid);
				}
				CFRelease(app);
			}
			CFRelease(sys);
		}
	}
	if (pid > 0) {
		return (int32_t)pid;
	}
	@autoreleasepool {
		NSRunningApplication *app = [[NSWorkspace sharedWorkspace] frontmostApplication];
		if (app != nil) {
			pid = app.processIdentifier;
		}
	}
	return (int32_t)pid;
}

static int dk_activate_app(int32_t pid) {
	int ok = 0;
	@autoreleasepool {
		NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:(pid_t)pid];
		if (app != nil) {
			ok = [app activateWithOptions:NSApplicationActivateIgnoringOtherApps] ? 1 : 0;
		}
	}
	return ok;
}

static int dk_ax_point(AXUIElementRef el, CFStringRef attr, AXValueType type, void *out) {
	CFTypeRef v = NULL;
	if (AXUIElementCopyAttributeValue(el, attr, &v) != kAXErrorSuccess || v == NULL) {
		return 0;
	}
	int ok = CFGetTypeID(v) == AXValueGetTypeID() && AXValueGetValue((AXValueRef)v, type, out);
	CFRelease(v);
	return ok;
}

// dk_ax_raise brings the application to the front and raises its window at the given bounds: 1 when
// the window was found and raised, 0 when only the application came forward, -1 on failure. Setting
// AXFrontmost works where activateWithOptions: is refused, as macOS 14 does for an app that is not
// itself active; the window list has no handle to an Accessibility window, so it is matched by its
// position and size.
static int dk_ax_raise(int32_t pid, double x, double y, double w, double h) {
	AXUIElementRef app = AXUIElementCreateApplication((pid_t)pid);
	if (app == NULL) {
		return -1;
	}
	AXUIElementSetAttributeValue(app, kAXFrontmostAttribute, kCFBooleanTrue);
	int found = 0;
	CFTypeRef windows = NULL;
	if (AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, &windows) == kAXErrorSuccess && windows != NULL) {
		if (CFGetTypeID(windows) == CFArrayGetTypeID()) {
			CFIndex n = CFArrayGetCount((CFArrayRef)windows);
			for (CFIndex i = 0; i < n && !found; i++) {
				CFTypeRef item = CFArrayGetValueAtIndex((CFArrayRef)windows, i);
				if (item == NULL || CFGetTypeID(item) != AXUIElementGetTypeID()) {
					continue;
				}
				AXUIElementRef win = (AXUIElementRef)item;
				CGPoint p;
				CGSize s;
				if (!dk_ax_point(win, kAXPositionAttribute, kAXValueTypeCGPoint, &p) ||
					!dk_ax_point(win, kAXSizeAttribute, kAXValueTypeCGSize, &s)) {
					continue;
				}
				if (fabs(p.x - x) <= 1 && fabs(p.y - y) <= 1 && fabs(s.width - w) <= 1 && fabs(s.height - h) <= 1) {
					AXUIElementPerformAction(win, kAXRaiseAction);
					AXUIElementSetAttributeValue(win, kAXMainAttribute, kCFBooleanTrue);
					found = 1;
				}
			}
		}
		CFRelease(windows);
	}
	CFRelease(app);
	return found;
}

static int dk_cursor(double *x, double *y) {
	CGEventRef ev = CGEventCreate(NULL);
	if (ev == NULL) {
		return 0;
	}
	CGPoint p = CGEventGetLocation(ev);
	CFRelease(ev);
	*x = p.x;
	*y = p.y;
	return 1;
}

static void dk_finish(CGEventRef ev, uint64_t flags, int64_t tag) {
	CGEventSetFlags(ev, (CGEventFlags)flags);
	CGEventSetIntegerValueField(ev, kCGEventSourceUserData, tag);
	CGEventPost(kCGHIDEventTap, ev);
	CFRelease(ev);
}

static int dk_post_mouse(int type, double x, double y, int button, int64_t clicks, uint64_t flags, int64_t tag) {
	if (dk_source == NULL) {
		return 0;
	}
	CGEventRef ev = CGEventCreateMouseEvent(dk_source, (CGEventType)type, CGPointMake(x, y), (CGMouseButton)button);
	if (ev == NULL) {
		return 0;
	}
	if (clicks > 0) {
		CGEventSetIntegerValueField(ev, kCGMouseEventClickState, clicks);
	}
	dk_finish(ev, flags, tag);
	return 1;
}

// The scroll wheel function is variadic, which cgo cannot call, hence this wrapper.
static int dk_post_scroll(int32_t wheel1, int32_t wheel2, uint64_t flags, int64_t tag) {
	if (dk_source == NULL) {
		return 0;
	}
	CGEventRef ev = CGEventCreateScrollWheelEvent(dk_source, kCGScrollEventUnitLine, 2, wheel1, wheel2);
	if (ev == NULL) {
		return 0;
	}
	dk_finish(ev, flags, tag);
	return 1;
}

static int dk_post_key(uint16_t code, int down, uint64_t flags, int64_t tag) {
	if (dk_source == NULL) {
		return 0;
	}
	CGEventRef ev = CGEventCreateKeyboardEvent(dk_source, (CGKeyCode)code, down ? true : false);
	if (ev == NULL) {
		return 0;
	}
	dk_finish(ev, flags, tag);
	return 1;
}

// dk_post_text types up to 20 UTF-16 units as one key press and release carrying the string. The
// key code is 0 and the flags are cleared, so the string is all an application sees: a held Command
// would otherwise turn the text into shortcuts.
static int dk_post_text(const uint16_t *units, int n, int64_t tag) {
	if (dk_source == NULL) {
		return 0;
	}
	for (int down = 1; down >= 0; down--) {
		CGEventRef ev = CGEventCreateKeyboardEvent(dk_source, (CGKeyCode)0, down ? true : false);
		if (ev == NULL) {
			return 0;
		}
		CGEventKeyboardSetUnicodeString(ev, (UniCharCount)n, (const UniChar *)units);
		dk_finish(ev, 0, tag);
	}
	return 1;
}

// dk_locked is 1 when the session's screen is locked or the session is not the one on the console
// (another user switched in), 0 when unlocked, -1 when the process has no window server session.
// Secure Event Input (a password field focused) is not a lock and is not reported here: the screen
// is the operator's and visible, only typed text is hidden from event taps.
static int dk_locked(void) {
	CFDictionaryRef d = CGSessionCopyCurrentDictionary();
	if (d == NULL) {
		return -1;
	}
	int locked = 0;
	CFTypeRef onConsole = CFDictionaryGetValue(d, kCGSessionOnConsoleKey);
	if (onConsole != NULL && CFGetTypeID(onConsole) == CFBooleanGetTypeID() && !CFBooleanGetValue((CFBooleanRef)onConsole)) {
		locked = 1;
	}
	// Not a documented key, but the one the system itself sets while the lock screen is up.
	CFTypeRef screenLocked = CFDictionaryGetValue(d, CFSTR("CGSSessionScreenIsLocked"));
	if (screenLocked != NULL) {
		if (CFGetTypeID(screenLocked) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)screenLocked)) {
			locked = 1;
		} else if (CFGetTypeID(screenLocked) == CFNumberGetTypeID()) {
			int v = 0;
			if (CFNumberGetValue((CFNumberRef)screenLocked, kCFNumberIntType, &v) && v != 0) {
				locked = 1;
			}
		}
	}
	CFRelease(d);
	return locked;
}

// The HID system state counts hardware events only, so this mostly sees the operator; our own posted
// events may or may not move it.
static double dk_seconds_since_input(void) {
	return CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType);
}
*/
import "C"

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"image"
	"math"
	"runtime"
	"slices"
	"strconv"
	"sync"
	"time"
	"unicode/utf16"
	"unsafe"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/deskd/internal/keys"
)

// chunkPause is the wait between two typed chunks. Posting them back to back can overtake the
// application's handling of the previous one, and the characters arrive out of order.
const chunkPause = 5 * time.Millisecond

type quartz struct {
	// tag is put in kCGEventSourceUserData of every event we post, so a listen tap can tell ours
	// from the operator's.
	tag         int64
	doubleClick time.Duration

	mu     sync.Mutex
	held   heldButtons
	mods   uint64
	clicks clickCounter
}

// The activation policy is NSApplication's, which belongs to the main thread; init is the last point
// where the main goroutine is certain to be on it, and locking it there keeps it there. It runs for
// every subcommand: only the self-test's window needs to be an ordinary application, and it says so
// itself when it opens one.
func init() {
	runtime.LockOSThread()
	C.dk_stay_out_of_dock()
}

// Open opens the desktop of the logged-in user. It fails without a window server session (a process
// started over SSH or by launchd outside the user's session).
func Open(ctx context.Context) (backend.Backend, error) {
	if C.dk_has_session() == 0 {
		return nil, fmt.Errorf("%w: the process is not in a window server session", backend.ErrNoBackend)
	}
	if C.dk_input_open() == 0 {
		return nil, errors.New("creating an event source failed")
	}
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		C.dk_input_close()
		return nil, err
	}
	interval := time.Duration(float64(C.dk_double_click_interval()) * float64(time.Second))
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	return &quartz{tag: int64(binary.LittleEndian.Uint64(b[:]) >> 1), doubleClick: interval}, nil
}

func (q *quartz) Platform() backend.Platform {
	return backend.Platform{OS: "darwin", SessionType: backend.SessionQuartz, Units: backend.UnitsPoints}
}

func (q *quartz) Displays(ctx context.Context) ([]geom.Display, error) {
	var list [32]C.dk_display
	n := int(C.dk_displays(&list[0], C.int(len(list))))
	if n < 0 {
		return nil, errors.New("listing the displays failed")
	}
	out := make([]geom.Display, 0, n)
	for _, d := range list[:n] {
		name := ""
		if c := C.dk_display_name(d.id); c != nil {
			name = C.GoString(c)
			C.free(unsafe.Pointer(c))
		}
		if name == "" && d.builtin != 0 {
			name = "built-in display"
		}
		ppp := float64(d.pixels_per_point)
		out = append(out, geom.Display{
			ID:            strconv.FormatUint(uint64(d.id), 10),
			Name:          name,
			Bounds:        geom.Rect{X: float64(d.x), Y: float64(d.y), W: float64(d.w), H: float64(d.h)},
			Primary:       d.main != 0,
			PixelsPerUnit: ppp,
			Scale:         ppp,
		})
	}
	slices.SortStableFunc(out, func(a, b geom.Display) int {
		switch {
		case a.Primary == b.Primary:
			return 0
		case a.Primary:
			return -1
		}
		return 1
	})
	return out, nil
}

func (q *quartz) Permissions(ctx context.Context) backend.Permissions {
	return backend.Permissions{
		Screen:          permissionOf(C.dk_screen_granted() != 0),
		Accessibility:   permissionOf(C.dk_ax_trusted() != 0),
		InputMonitoring: permissionOf(C.dk_listen_granted() != 0),
	}
}

// RequestPermission shows the system's prompt for which. macOS shows each prompt once per program;
// after that the calls return at once and the operator has to use System Settings (SettingsURL).
func (q *quartz) RequestPermission(ctx context.Context, which string) error {
	switch which {
	case backend.PermScreen:
		C.dk_request_screen()
	case backend.PermAccessibility:
		C.dk_request_ax()
	case backend.PermInputMonitoring:
		C.dk_request_listen()
	default:
		return fmt.Errorf("no permission %q on macOS", which)
	}
	return nil
}

func (q *quartz) Capture(ctx context.Context, display geom.Display, region geom.Rect) (*image.RGBA, error) {
	if C.dk_screen_granted() == 0 {
		return nil, &backend.PermissionError{Which: backend.PermScreen, Detail: permissionDetail(backend.PermScreen)}
	}
	id, err := strconv.ParseUint(display.ID, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("display id %q is not a CGDirectDisplayID", display.ID)
	}
	r := region.Intersect(display.Bounds)
	if r.Empty() {
		return nil, fmt.Errorf("the region %v is not on display %s", region, display.ID)
	}
	c := C.dk_capture_rect(C.uint32_t(id), C.double(r.X-display.Bounds.X), C.double(r.Y-display.Bounds.Y),
		C.double(r.W), C.double(r.H))
	switch c.status {
	case 1:
		// Between the probe and the capture the grant can go (the monthly re-confirmation); a missing
		// image is then a permission matter, not a broken display.
		if C.dk_screen_granted() == 0 {
			return nil, &backend.PermissionError{Which: backend.PermScreen, Detail: permissionDetail(backend.PermScreen)}
		}
		return nil, fmt.Errorf("display %s gave no image", display.ID)
	case 2:
		return nil, errors.New("converting the captured image failed")
	}
	defer C.free(unsafe.Pointer(c.data))
	stride, h := int(c.stride), int(c.h)
	img, err := rgbaFromRows(int(c.w), h, stride, unsafe.Slice((*byte)(unsafe.Pointer(c.data)), stride*h))
	if err != nil {
		return nil, err
	}
	if allZero(img) {
		return nil, &backend.PermissionError{Which: backend.PermScreen, Detail: permissionDetail(backend.PermScreen)}
	}
	return img, nil
}

func (q *quartz) app(pid int, owner string) backend.App {
	info := C.dk_app_info(C.int32_t(pid))
	defer C.dk_app_free(info)
	a := backend.App{
		PID:      pid,
		BundleID: C.GoString(info.bundle),
		ExePath:  C.GoString(info.exe),
		Name:     C.GoString(info.name),
	}
	if a.Name == "" {
		a.Name = owner
	}
	if a.BundleID != "" {
		a.ID = a.BundleID
	} else {
		a.ID = "pid:" + strconv.Itoa(pid)
	}
	return a
}

func (q *quartz) Windows(ctx context.Context) ([]backend.Window, error) {
	var list *C.dk_window
	n := int(C.dk_windows(&list))
	if n < 0 {
		return nil, errors.New("listing the windows failed")
	}
	defer C.dk_windows_free(list, C.int(n))
	if n == 0 {
		return nil, nil
	}
	focus := int(C.dk_focused_pid())
	apps := map[int]backend.App{}
	out := make([]backend.Window, 0, n)
	focused := false
	for _, it := range unsafe.Slice(list, n) {
		pid := int(it.pid)
		a, ok := apps[pid]
		if !ok {
			a = q.app(pid, C.GoString(it.owner))
			apps[pid] = a
		}
		w := backend.Window{
			ID:     strconv.FormatUint(uint64(it.number), 10),
			Title:  C.GoString(it.title),
			Bounds: geom.Rect{X: float64(it.x), Y: float64(it.y), W: float64(it.w), H: float64(it.h)},
			App:    a,
		}
		// The list is front to back, so the focused application's first window is its key window
		// as far as the window server will say without Accessibility.
		if !focused && pid == focus {
			w.Focused, focused = true, true
		}
		out = append(out, w)
	}
	return out, nil
}

func (q *quartz) WindowAt(ctx context.Context, x, y float64) (backend.Window, bool, error) {
	ws, err := q.Windows(ctx)
	if err != nil {
		return backend.Window{}, false, err
	}
	for _, w := range ws {
		if w.Bounds.Contains(x, y) {
			return w, true, nil
		}
	}
	return backend.Window{}, false, nil
}

func (q *quartz) Foreground(ctx context.Context) (backend.Window, bool, error) {
	ws, err := q.Windows(ctx)
	if err != nil {
		return backend.Window{}, false, err
	}
	for _, w := range ws {
		if w.Focused {
			return w, true, nil
		}
	}
	return backend.Window{}, false, nil
}

// Activate brings the window's application to the front and, with Accessibility, raises that window
// above the application's others. Without Accessibility only the application comes forward, with
// whichever of its windows was last in front: macOS offers no other public way to raise one window
// of another program.
func (q *quartz) Activate(ctx context.Context, windowID string) error {
	ws, err := q.Windows(ctx)
	if err != nil {
		return err
	}
	i := slices.IndexFunc(ws, func(w backend.Window) bool { return w.ID == windowID })
	if i < 0 {
		return fmt.Errorf("no window %s on screen", windowID)
	}
	w := ws[i]
	activated := C.dk_activate_app(C.int32_t(w.App.PID)) != 0
	if C.dk_ax_trusted() != 0 {
		if C.dk_ax_raise(C.int32_t(w.App.PID), C.double(w.Bounds.X), C.double(w.Bounds.Y),
			C.double(w.Bounds.W), C.double(w.Bounds.H)) >= 0 {
			return nil
		}
	}
	if !activated {
		return fmt.Errorf("macOS refused to bring %s to the front", w.App.Name)
	}
	return nil
}

func (q *quartz) Cursor(ctx context.Context) (float64, float64, error) {
	var x, y C.double
	if C.dk_cursor(&x, &y) == 0 {
		return 0, 0, errors.New("reading the pointer failed")
	}
	return float64(x), float64(y), nil
}

// inputAllowed refuses input without Accessibility. Since macOS 10.14 the system drops events posted
// by an untrusted program without a word, so without this check every click would report success.
func inputAllowed() error {
	if C.dk_ax_trusted() == 0 {
		return &backend.PermissionError{Which: backend.PermAccessibility, Detail: permissionDetail(backend.PermAccessibility)}
	}
	return nil
}

func (q *quartz) Move(ctx context.Context, x, y float64) error {
	if err := inputAllowed(); err != nil {
		return err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	typ, btn := moveEvent(q.held)
	if C.dk_post_mouse(C.int(typ), C.double(x), C.double(y), C.int(btn), 0, C.uint64_t(q.mods), C.int64_t(q.tag)) == 0 {
		return errors.New("posting a pointer move failed")
	}
	return nil
}

func (q *quartz) Button(ctx context.Context, b backend.Button, down bool) error {
	if err := inputAllowed(); err != nil {
		return err
	}
	typ, btn, err := buttonEvent(b, down)
	if err != nil {
		return err
	}
	x, y, err := q.Cursor(ctx)
	if err != nil {
		return err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	var clicks int
	if down {
		clicks = q.clicks.press(b, x, y, time.Now(), q.doubleClick)
	} else {
		clicks = q.clicks.release(b)
	}
	if C.dk_post_mouse(C.int(typ), C.double(x), C.double(y), C.int(btn), C.int64_t(clicks), C.uint64_t(q.mods), C.int64_t(q.tag)) == 0 {
		return fmt.Errorf("posting a %s button event failed", b)
	}
	q.held[b] = down
	return nil
}

func (q *quartz) Scroll(ctx context.Context, dx, dy int) error {
	if err := inputAllowed(); err != nil {
		return err
	}
	if dx == 0 && dy == 0 {
		return nil
	}
	w1, w2 := wheelAxes(dx, dy)
	q.mu.Lock()
	defer q.mu.Unlock()
	if C.dk_post_scroll(C.int32_t(w1), C.int32_t(w2), C.uint64_t(q.mods), C.int64_t(q.tag)) == 0 {
		return errors.New("posting a scroll failed")
	}
	return nil
}

func (q *quartz) Key(ctx context.Context, k keys.Key, down bool) error {
	if err := inputAllowed(); err != nil {
		return err
	}
	s, err := lookupKey(k)
	if err != nil {
		return err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	flags := q.mods | s.implied
	if s.mod != "" {
		// A modifier's own event already carries its new state, as the hardware's does: the press of
		// Command has the Command bit, its release does not.
		if down {
			q.mods |= modFlags[s.mod]
		} else {
			q.mods &^= modFlags[s.mod]
		}
		flags = q.mods
	}
	if C.dk_post_key(C.uint16_t(s.code), boolInt(down), C.uint64_t(flags), C.int64_t(q.tag)) == 0 {
		return fmt.Errorf("posting key %s failed", k)
	}
	return nil
}

func (q *quartz) Type(ctx context.Context, text string) error {
	if err := inputAllowed(); err != nil {
		return err
	}
	for i, chunk := range keys.ChunkUTF16(text, 20) {
		if i > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(chunkPause):
			}
		}
		units := utf16.Encode([]rune(chunk))
		if len(units) == 0 {
			continue
		}
		q.mu.Lock()
		ok := C.dk_post_text((*C.uint16_t)(unsafe.Pointer(&units[0])), C.int(len(units)), C.int64_t(q.tag))
		q.mu.Unlock()
		if ok == 0 {
			return errors.New("posting typed text failed")
		}
	}
	return nil
}

func (q *quartz) Secure(ctx context.Context) (backend.Secure, error) {
	// No session at all (-1) counts as locked: it is a fast user switch away from this user, and
	// nothing of theirs is on the screen.
	if C.dk_locked() == 0 {
		return backend.Secure{}, nil
	}
	return backend.Secure{Locked: true, Reason: "lock_screen"}, nil
}

func (q *quartz) LastInput(ctx context.Context) (time.Time, bool) {
	secs := float64(C.dk_seconds_since_input())
	if math.IsNaN(secs) || math.IsInf(secs, 0) || secs < 0 {
		return time.Time{}, false
	}
	return time.Now().Add(-time.Duration(secs * float64(time.Second))), true
}

func (q *quartz) Close() error {
	C.dk_input_close()
	return nil
}

func boolInt(b bool) C.int {
	if b {
		return 1
	}
	return 0
}
