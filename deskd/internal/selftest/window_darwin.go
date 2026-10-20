//go:build darwin && cgo

// The macOS test window. AppKit works only on the process's main thread, and only while that thread
// runs the event loop, so this file needs the caller's help: the main goroutine must call mainLoop
// and keep pumping it while the checks run on another goroutine, then close stop when they finish.
// The init below locks the main goroutine to the main thread so that mainLoop, called from it, runs
// there. Every AppKit call from the checks' goroutine is sent to the main queue with dispatch_sync_f,
// which the pumping loop drains; opening a window while nothing pumps fails at once instead of
// waiting for ever.

package selftest

/*
#cgo CFLAGS: -x objective-c -fobjc-arc
#cgo LDFLAGS: -framework AppKit -framework CoreFoundation

// activateIgnoringOtherApps: is deprecated in macOS 14 in favour of cooperative activation, which a
// command-line process launched from a terminal does not get; the old call still activates it.
#pragma clang diagnostic ignored "-Wdeprecated-declarations"

#import <AppKit/AppKit.h>
#include <dispatch/dispatch.h>
#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

// Defines rather than an enum: cgo gives a define an untyped Go constant, which compares with the
// struct's int field without a conversion.
#define DK_PRESS 1
#define DK_RELEASE 2
#define DK_MOTION 3
#define DK_WHEEL 4
#define DK_CHAR 5
#define DK_KEY 6

typedef struct {
	int kind;
	double x, y;
	int button;
	int wheel;
	char text[64];
	char key[64];
} dk_event;

// The ring the window's handlers fill on the main thread and the checks drain from theirs. When it
// is full the oldest event goes: a test reads its events after every action, so only a run that never
// reads loses any.
#define DK_RING 1024
static dk_event dk_ring[DK_RING];
static int dk_ring_head = 0;
static int dk_ring_count = 0;
static pthread_mutex_t dk_ring_mutex = PTHREAD_MUTEX_INITIALIZER;

static void dk_record(dk_event e) {
	pthread_mutex_lock(&dk_ring_mutex);
	int tail = (dk_ring_head + dk_ring_count) % DK_RING;
	dk_ring[tail] = e;
	if (dk_ring_count < DK_RING) {
		dk_ring_count++;
	} else {
		dk_ring_head = (dk_ring_head + 1) % DK_RING;
	}
	pthread_mutex_unlock(&dk_ring_mutex);
}

static int dk_drain(dk_event *out, int max) {
	pthread_mutex_lock(&dk_ring_mutex);
	int n = 0;
	while (n < max && dk_ring_count > 0) {
		out[n++] = dk_ring[dk_ring_head];
		dk_ring_head = (dk_ring_head + 1) % DK_RING;
		dk_ring_count--;
	}
	pthread_mutex_unlock(&dk_ring_mutex);
	return n;
}

static void dk_ring_clear(void) {
	pthread_mutex_lock(&dk_ring_mutex);
	dk_ring_head = 0;
	dk_ring_count = 0;
	pthread_mutex_unlock(&dk_ring_mutex);
}

static void dk_copy(char *dst, size_t size, const char *src) {
	if (src == NULL) {
		dst[0] = 0;
		return;
	}
	strncpy(dst, src, size - 1);
	dst[size - 1] = 0;
}

// The quadrant colours, set from Go before the first window opens.
static double dk_colors[4][3];

static void dk_set_color(int i, double r, double g, double b) {
	dk_colors[i][0] = r;
	dk_colors[i][1] = g;
	dk_colors[i][2] = b;
}

@interface DKView : NSView
@end

@implementation DKView

// Flipped, so the view's y runs down from the top-left corner like the desktop's and the events can
// be reported as they are; AppKit's own views count up from the bottom.
- (BOOL)isFlipped {
	return YES;
}

- (BOOL)acceptsFirstResponder {
	return YES;
}

// Without this the first click on the window while another app is active only activates it and is
// never delivered, and the selftest's first press would be lost.
- (BOOL)acceptsFirstMouse:(NSEvent *)event {
	return YES;
}

- (void)drawRect:(NSRect)dirty {
	NSRect b = self.bounds;
	CGFloat hw = b.size.width / 2, hh = b.size.height / 2;
	NSRect quads[4] = {
		NSMakeRect(0, 0, hw, hh),
		NSMakeRect(hw, 0, b.size.width - hw, hh),
		NSMakeRect(0, hh, hw, b.size.height - hh),
		NSMakeRect(hw, hh, b.size.width - hw, b.size.height - hh),
	};
	for (int i = 0; i < 4; i++) {
		// sRGB, the space the backend's capture converts into, so a colour comes back as painted.
		[[NSColor colorWithSRGBRed:dk_colors[i][0] green:dk_colors[i][1] blue:dk_colors[i][2] alpha:1] setFill];
		NSRectFill(quads[i]);
	}
}

- (void)recordMouse:(NSEvent *)event kind:(int)kind button:(int)button {
	NSPoint p = [self convertPoint:[event locationInWindow] fromView:nil];
	dk_event e;
	memset(&e, 0, sizeof e);
	e.kind = kind;
	e.x = p.x;
	e.y = p.y;
	e.button = button;
	dk_record(e);
}

// AppKit numbers the buttons from 0 (left, right, middle); the selftest's middle is 2, and any
// further button keeps its place after that.
- (int)otherButton:(NSEvent *)event {
	NSInteger n = [event buttonNumber];
	return n == 2 ? 2 : (int)n + 1;
}

- (void)mouseDown:(NSEvent *)event {
	[self recordMouse:event kind:DK_PRESS button:1];
}
- (void)mouseUp:(NSEvent *)event {
	[self recordMouse:event kind:DK_RELEASE button:1];
}
- (void)rightMouseDown:(NSEvent *)event {
	[self recordMouse:event kind:DK_PRESS button:3];
}
- (void)rightMouseUp:(NSEvent *)event {
	[self recordMouse:event kind:DK_RELEASE button:3];
}
- (void)otherMouseDown:(NSEvent *)event {
	[self recordMouse:event kind:DK_PRESS button:[self otherButton:event]];
}
- (void)otherMouseUp:(NSEvent *)event {
	[self recordMouse:event kind:DK_RELEASE button:[self otherButton:event]];
}
- (void)mouseMoved:(NSEvent *)event {
	[self recordMouse:event kind:DK_MOTION button:0];
}
- (void)mouseDragged:(NSEvent *)event {
	[self recordMouse:event kind:DK_MOTION button:0];
}
- (void)rightMouseDragged:(NSEvent *)event {
	[self recordMouse:event kind:DK_MOTION button:0];
}
- (void)otherMouseDragged:(NSEvent *)event {
	[self recordMouse:event kind:DK_MOTION button:0];
}

// AppKit's scrollingDeltaY is positive for a wheel rolled away from the person, which scrolls up, so
// notches down are its negation. A wheel event of several lines arrives as one event; it is rounded to
// notches and never reported as zero when it moved at all.
- (void)scrollWheel:(NSEvent *)event {
	CGFloat dy = [event scrollingDeltaY];
	if (dy == 0) {
		return;
	}
	long notches = lround(-dy);
	if (notches == 0) {
		notches = dy < 0 ? 1 : -1;
	}
	NSPoint p = [self convertPoint:[event locationInWindow] fromView:nil];
	dk_event e;
	memset(&e, 0, sizeof e);
	e.kind = DK_WHEEL;
	e.x = p.x;
	e.y = p.y;
	e.wheel = (int)notches;
	dk_record(e);
}

@end

static const char *dk_named_key(unsigned short code) {
	switch (code) {
	case 36: return "enter";
	case 48: return "tab";
	case 51: return "backspace";
	case 53: return "escape";
	case 117: return "delete";
	case 115: return "home";
	case 119: return "end";
	case 116: return "pageup";
	case 121: return "pagedown";
	case 123: return "left";
	case 124: return "right";
	case 125: return "down";
	case 126: return "up";
	}
	return NULL;
}

// dk_record_key turns a key press into a char or key event the way the selftest expects: a named key
// or a chord with Control, Option or Command as its canonical name ("ctrl+a", "super+a", "enter"),
// anything else as the characters it typed.
static void dk_record_key(NSEvent *event) {
	NSEventModifierFlags f = [event modifierFlags] & NSEventModifierFlagDeviceIndependentFlagsMask;
	BOOL ctrl = (f & NSEventModifierFlagControl) != 0;
	BOOL alt = (f & NSEventModifierFlagOption) != 0;
	BOOL shift = (f & NSEventModifierFlagShift) != 0;
	BOOL cmd = (f & NSEventModifierFlagCommand) != 0;
	const char *named = dk_named_key([event keyCode]);
	dk_event e;
	memset(&e, 0, sizeof e);
	if (named == NULL && !ctrl && !alt && !cmd) {
		e.kind = DK_CHAR;
		dk_copy(e.text, sizeof e.text, [[event characters] UTF8String]);
		if (e.text[0] != 0) {
			dk_record(e);
		}
		return;
	}
	NSString *base = named != NULL ? [NSString stringWithUTF8String:named]
		: [[event charactersIgnoringModifiers] lowercaseString];
	if (base == nil || [base length] == 0) {
		return;
	}
	NSMutableString *chord = [NSMutableString string];
	if (ctrl) [chord appendString:@"ctrl+"];
	if (alt) [chord appendString:@"alt+"];
	if (shift) [chord appendString:@"shift+"];
	if (cmd) [chord appendString:@"super+"];
	[chord appendString:base];
	e.kind = DK_KEY;
	dk_copy(e.key, sizeof e.key, [chord UTF8String]);
	dk_record(e);
}

@interface DKWindow : NSWindow
@end

@implementation DKWindow

- (BOOL)canBecomeKeyWindow {
	return YES;
}

// Key presses are taken here, before the responder chain: a chord with Command goes through
// performKeyEquivalent: and the rest through sendEvent:, and either route could otherwise end in the
// system beep with the view never told. Returning YES stops a chord from also arriving as keyDown,
// so each press is recorded once.
- (BOOL)performKeyEquivalent:(NSEvent *)event {
	if ([event type] == NSEventTypeKeyDown) {
		dk_record_key(event);
		return YES;
	}
	return [super performKeyEquivalent:event];
}

- (void)sendEvent:(NSEvent *)event {
	if ([event type] == NSEventTypeKeyDown) {
		dk_record_key(event);
		return;
	}
	[super sendEvent:event];
}

@end

// The open windows by window number. ARC keeps an object alive only through a strong reference, and
// a C struct handed to Go cannot hold one, so the windows live here and Go holds the number.
static NSMutableDictionary *dk_windows = nil;
static int dk_app_ready = 0;

static void dk_app_start(void) {
	if (dk_app_ready) {
		return;
	}
	[NSApplication sharedApplication];
	// Regular, not accessory: an unbundled command-line process is otherwise not a foreground
	// application at all, and the window server will not give its window the keyboard, so typed keys
	// would go to the terminal that started the test. The Dock icon for the seconds of the run is the
	// price.
	[NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
	[NSApp finishLaunching];
	dk_windows = [NSMutableDictionary dictionary];
	dk_app_ready = 1;
}

// dk_pump runs the event loop on the main thread for up to seconds: it waits for the first event,
// then handles whatever else is queued without waiting. Waiting for events also runs the main run
// loop, which is what drains the main dispatch queue the other goroutines send work to.
static void dk_pump(double seconds) {
	@autoreleasepool {
		dk_app_start();
		NSDate *until = [NSDate dateWithTimeIntervalSinceNow:seconds];
		for (;;) {
			NSEvent *event = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:until
				inMode:NSDefaultRunLoopMode dequeue:YES];
			if (event == nil) {
				break;
			}
			[NSApp sendEvent:event];
			until = [NSDate distantPast];
		}
		[NSApp updateWindows];
	}
}

typedef struct {
	const char *title;
	double w, h;
	long number;
	double x, y, cw, ch;
	double dx, dy;
	int ok;
} dk_call;

static DKWindow *dk_lookup(long number) {
	if (dk_windows == nil) {
		return nil;
	}
	return dk_windows[@(number)];
}

static void dk_open_main(void *context) {
	dk_call *c = (dk_call *)context;
	@autoreleasepool {
		dk_app_start();
		NSArray *screens = [NSScreen screens];
		if ([screens count] == 0) {
			return;
		}
		NSScreen *primary = screens[0];
		NSRect visible = [primary visibleFrame];
		// A fixed spot near the primary screen's top-left corner, below the menu bar: away from the
		// Dock and the notification banners, and the same on every run.
		NSRect content = NSMakeRect(visible.origin.x + 120,
			visible.origin.y + visible.size.height - 120 - c->h, c->w, c->h);
		DKWindow *window = [[DKWindow alloc] initWithContentRect:content
			styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
		if (window == nil) {
			return;
		}
		// ARC owns the window; letting close release it as well would free it twice.
		[window setReleasedWhenClosed:NO];
		NSString *title = c->title != NULL ? [NSString stringWithUTF8String:c->title] : nil;
		[window setTitle:title != nil ? title : @""];
		// The normal level, not a floating one: the backend lists only windows of layer 0 (floating
		// windows are layer 3, with palettes and overlays), so a floating test window never shows up
		// in the daemon's list and the selftest stops at "listed". Activation brings it to the front.
		DKView *view = [[DKView alloc] initWithFrame:NSMakeRect(0, 0, c->w, c->h)];
		[window setContentView:view];
		// Moves arrive only when the window asks for them and only at its first responder.
		[window setAcceptsMouseMovedEvents:YES];
		[window makeFirstResponder:view];
		[NSApp activateIgnoringOtherApps:YES];
		[window makeKeyAndOrderFront:nil];
		[view setNeedsDisplay:YES];
		long number = (long)[window windowNumber];
		dk_windows[@(number)] = window;
		c->number = number;
		c->ok = 1;
	}
}

// dk_content_main reads the content rectangle in desktop units: global points from the top-left of
// the primary screen. AppKit's frames count up from the primary screen's bottom edge, so y is flipped
// with that screen's height.
static void dk_content_main(void *context) {
	dk_call *c = (dk_call *)context;
	@autoreleasepool {
		DKWindow *window = dk_lookup(c->number);
		NSArray *screens = [NSScreen screens];
		if (window == nil || [screens count] == 0) {
			return;
		}
		CGFloat primaryHeight = [(NSScreen *)screens[0] frame].size.height;
		NSRect r = [window contentRectForFrameRect:[window frame]];
		c->x = r.origin.x;
		c->y = primaryHeight - (r.origin.y + r.size.height);
		c->cw = r.size.width;
		c->ch = r.size.height;
		c->ok = 1;
	}
}

static void dk_move_main(void *context) {
	dk_call *c = (dk_call *)context;
	@autoreleasepool {
		DKWindow *window = dk_lookup(c->number);
		if (window == nil) {
			return;
		}
		NSPoint origin = [window frame].origin;
		// Down on the desktop is down the screen, which in AppKit's coordinates is a smaller y.
		[window setFrameOrigin:NSMakePoint(origin.x + c->dx, origin.y - c->dy)];
		c->ok = 1;
	}
}

static void dk_close_main(void *context) {
	dk_call *c = (dk_call *)context;
	@autoreleasepool {
		DKWindow *window = dk_lookup(c->number);
		if (window == nil) {
			return;
		}
		[window orderOut:nil];
		[window close];
		[dk_windows removeObjectForKey:@(c->number)];
		c->ok = 1;
	}
}

static void dk_open(dk_call *c) { dispatch_sync_f(dispatch_get_main_queue(), c, dk_open_main); }
static void dk_content(dk_call *c) { dispatch_sync_f(dispatch_get_main_queue(), c, dk_content_main); }
static void dk_move(dk_call *c) { dispatch_sync_f(dispatch_get_main_queue(), c, dk_move_main); }
static void dk_close(dk_call *c) { dispatch_sync_f(dispatch_get_main_queue(), c, dk_close_main); }
*/
import "C"

import (
	"context"
	"errors"
	"runtime"
	"strconv"
	"sync/atomic"
	"unsafe"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

// The main goroutine starts on the main thread; locking it there in init is the only point where that
// is still true, and mainLoop depends on it.
func init() {
	runtime.LockOSThread()
	opener = openDarwinWindow
	mainLoop = pumpMainLoop
}

// pumping is set while mainLoop runs. dispatch_sync to a main queue nobody drains never returns, so
// the window calls check it first.
var pumping atomic.Bool

// mainLoop runs AppKit's event loop on the calling goroutine, which must be the main goroutine, until
// stop is closed.
var mainLoop func(stop <-chan struct{})

func pumpMainLoop(stop <-chan struct{}) {
	pumping.Store(true)
	defer pumping.Store(false)
	for {
		select {
		case <-stop:
			return
		default:
		}
		C.dk_pump(0.02)
	}
}

var errNoMainLoop = errors.New("the macOS test window needs mainLoop running on the main goroutine")

type darwinWindow struct {
	number C.long
}

func openDarwinWindow(ctx context.Context, title string, w, h int) (Window, error) {
	if !pumping.Load() {
		return nil, errNoMainLoop
	}
	for i, c := range quadrantColors {
		C.dk_set_color(C.int(i), C.double(float64(c[0])/255), C.double(float64(c[1])/255), C.double(float64(c[2])/255))
	}
	C.dk_ring_clear()
	ctitle := C.CString(title)
	defer C.free(unsafe.Pointer(ctitle))
	call := (*C.dk_call)(C.calloc(1, C.size_t(unsafe.Sizeof(C.dk_call{}))))
	defer C.free(unsafe.Pointer(call))
	call.title = ctitle
	call.w, call.h = C.double(w), C.double(h)
	C.dk_open(call)
	if call.ok == 0 {
		return nil, errors.New("opening the test window failed")
	}
	return &darwinWindow{number: call.number}, nil
}

// do runs one window call on the main thread. The call record is C memory because the main thread
// writes into it while Go waits, and a Go pointer may not be kept by C past the call that got it.
func (d *darwinWindow) do(f func(*C.dk_call), fill func(*C.dk_call)) (C.dk_call, error) {
	if !pumping.Load() {
		return C.dk_call{}, errNoMainLoop
	}
	call := (*C.dk_call)(C.calloc(1, C.size_t(unsafe.Sizeof(C.dk_call{}))))
	defer C.free(unsafe.Pointer(call))
	call.number = d.number
	if fill != nil {
		fill(call)
	}
	f(call)
	if call.ok == 0 {
		return C.dk_call{}, errors.New("the test window is gone")
	}
	return *call, nil
}

func (d *darwinWindow) ID() string { return strconv.FormatInt(int64(d.number), 10) }

func (d *darwinWindow) Content() (geom.Rect, error) {
	c, err := d.do(func(c *C.dk_call) { C.dk_content(c) }, nil)
	if err != nil {
		return geom.Rect{}, err
	}
	return geom.Rect{X: float64(c.x), Y: float64(c.y), W: float64(c.cw), H: float64(c.ch)}, nil
}

func (d *darwinWindow) Events() []Event {
	var buf [256]C.dk_event
	var out []Event
	for {
		n := int(C.dk_drain(&buf[0], C.int(len(buf))))
		for _, e := range buf[:n] {
			ev := Event{X: float64(e.x), Y: float64(e.y), Button: int(e.button), Wheel: int(e.wheel)}
			switch e.kind {
			case C.DK_PRESS:
				ev.Kind = "press"
			case C.DK_RELEASE:
				ev.Kind = "release"
			case C.DK_MOTION:
				ev.Kind = "motion"
			case C.DK_WHEEL:
				ev.Kind = "wheel"
			case C.DK_CHAR:
				ev.Kind = "char"
				ev.Text = C.GoString(&e.text[0])
			case C.DK_KEY:
				ev.Kind = "key"
				ev.Key = C.GoString(&e.key[0])
			default:
				continue
			}
			out = append(out, ev)
		}
		if n < len(buf) {
			return out
		}
	}
}

func (d *darwinWindow) MoveBy(dx, dy float64) error {
	_, err := d.do(func(c *C.dk_call) { C.dk_move(c) }, func(c *C.dk_call) {
		c.dx, c.dy = C.double(dx), C.double(dy)
	})
	return err
}

func (d *darwinWindow) Close() error {
	_, err := d.do(func(c *C.dk_call) { C.dk_close(c) }, nil)
	return err
}
