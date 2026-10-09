package config

import "time"

// The limits of the side channels: exec.run, fs.*, net.dial and the hook listener. Each one bounds
// something a caller could otherwise make the daemon hold without end.
const (
	// DefaultExecTimeout and MaxExecTimeout bound one exec.run. Half an hour covers a CLI updating
	// itself over a slow line; the default covers a version check.
	DefaultExecTimeout = time.Minute
	MaxExecTimeout     = 30 * time.Minute

	// DefaultExecOutput and MaxExecOutput bound what is captured of each of stdout and stderr. The
	// rest is read and thrown away, so a chatty program never blocks on a full pipe.
	DefaultExecOutput = 1 << 20
	MaxExecOutput     = 4 << 20

	// MaxExecStdin bounds what may be fed to a program's stdin.
	MaxExecStdin = 512 << 10

	// MaxExecConcurrent bounds the exec.run calls running at once. An update of five CLIs at once is
	// five; more than this is a client that lost count.
	MaxExecConcurrent = 16

	// MaxReplyData is the most file or program output one reply carries. A reply is one frame
	// (1 MiB) and the data travels as base64 or as JSON text, so a bigger read is cut here and
	// continued by offset; the caller learns it from `eof` or `truncated`.
	MaxReplyData = 640 << 10

	// MaxFSRead is the largest `max` a read or tail may ask for. It is what the protocol names; what
	// one reply carries is MaxReplyData.
	MaxFSRead = 4 << 20

	// MaxFSWrite is the most one fs.write carries (base64 in one frame), and MaxFSWriteFile the
	// largest file it builds by offset: a file handed to a staff member, never a disk image.
	MaxFSWrite     = 512 << 10
	MaxFSWriteFile = 50 << 20

	// MaxFSList and DefaultFSList bound the entries of one fs.list.
	MaxFSList     = 5000
	DefaultFSList = 1000

	// MaxBrowse and DefaultBrowse bound the folders of one fs.browse, and BrowseBudget the time its
	// per-folder stats may take: a picker listing a slow network mount answers with what it has.
	MaxBrowse     = 2000
	DefaultBrowse = 500
	BrowseBudget  = 1500 * time.Millisecond

	// MaxTailFollow is the longest a tail waits for new bytes, and TailPoll how often it looks.
	// Polling a stat is cheap and portable; a watcher per tail would be a descriptor per tail.
	MaxTailFollow = time.Minute
	TailPoll      = 100 * time.Millisecond

	// MaxFollowing bounds the tails waiting at once, so long polls can never take every request slot
	// a connection has and starve the terminal methods behind them.
	MaxFollowing = 128

	// MaxRoots bounds the roots the host may set.
	MaxRoots = 1024

	// DialTimeout bounds connecting a net.dial stream; MaxDialsPerLaunch and MaxDials bound the
	// streams open at once.
	DialTimeout       = 5 * time.Second
	MaxDialsPerLaunch = 16
	MaxDials          = 256

	// DialQueueBytes is what a net.dial stream holds from the host before its program has read it.
	// Past it the stream is closed as too slow, rather than the host's frames waiting behind it.
	DialQueueBytes = 1 << 20

	// HookBodyBytes is the largest hook post; HookRate and HookBurst its rate per launch.
	HookBodyBytes = 1 << 20
	HookRate      = 100
	HookBurst     = 100

	// MaxHookHold is the longest a post may be held for a reply; DefaultHookHoldMax is a launch's
	// own ceiling when it does not name one.
	MaxHookHold        = 10 * time.Minute
	DefaultHookHoldMax = MaxHookHold

	// MaxHeldPerLaunch and MaxHeld bound the posts waiting for a reply.
	MaxHeldPerLaunch = 32
	MaxHeld          = 256

	// HookEventBody is the most of a post's body its event carries. Bigger bodies have their long
	// strings shortened, keeping every key and id, so the event is still one frame and the log's
	// memory stays bounded.
	HookEventBody = 256 << 10

	// EventLogBytes bounds the event log's memory by size as well as by count: twenty thousand
	// hook events of a quarter megabyte each would otherwise be five gigabytes.
	EventLogBytes = 64 << 20

	// LaunchGrace is how long a launch outlives the last of its terminals: hooks a CLI fires as it
	// exits (Claude's SessionEnd) may land just after the exit is seen.
	LaunchGrace = 30 * time.Second

	// DefaultLaunchTTL and MaxLaunchTTL bound a launch no terminal has been started with yet.
	DefaultLaunchTTL = 10 * time.Minute
	MaxLaunchTTL     = 24 * time.Hour

	// MaxLaunches bounds the launches registered at once.
	MaxLaunches = 512

	// MaxLaunchFiles and MaxLaunchFileBytes bound the overlay files of one launch.
	MaxLaunchFiles     = 32
	MaxLaunchFileBytes = 512 << 10

	// MaxLaunchPutFiles bounds the files added to a launch after it started (long messages).
	MaxLaunchPutFiles = 1024
)
