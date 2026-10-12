// Package human plans input the way a person's hand makes it: a pointer that travels to its target
// on a curve rather than appearing there, a press held for a moment, keys typed one after another.
// It only plans — points and waits — and the page model sends them. Every plan fits a budget, so
// an agent's action is never slower than the deployment allows.
package human

import (
	"math"
	"math/rand/v2"
	"sync"
	"time"
)

// Point is a position in the viewport, in CSS pixels.
type Point struct{ X, Y float64 }

// Step is one point of a pointer's travel and how long to wait before sending it.
type Step struct {
	At   Point
	Wait time.Duration
}

// Planner draws the random parts of every plan from one source. It is safe for concurrent use.
type Planner struct {
	mu  sync.Mutex
	rng *rand.Rand
}

// New returns a planner whose plans follow from seed: the same seed, the same plans, which is what
// lets a test hold a trajectory to its shape.
func New(seed uint64) *Planner {
	return &Planner{rng: rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))}
}

// Float is a number drawn evenly from [0, 1).
func (p *Planner) Float() float64 { return p.float() }

func (p *Planner) float() float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.rng.Float64()
}

// Between is a duration drawn evenly from lo..hi.
func (p *Planner) Between(lo, hi time.Duration) time.Duration {
	if hi <= lo {
		return lo
	}
	return lo + time.Duration(p.float()*float64(hi-lo))
}

// Budget is what is left of the time an action may add. Take hands out at most what is left.
type Budget struct{ left time.Duration }

// NewBudget is a budget of d.
func NewBudget(d time.Duration) *Budget { return &Budget{left: max(0, d)} }

// Take spends up to d and returns what was granted.
func (b *Budget) Take(d time.Duration) time.Duration {
	d = min(max(0, d), b.left)
	b.left -= d
	return d
}

// Left is what has not been spent.
func (b *Budget) Left() time.Duration { return b.left }

// The shape of a travel. A person's pointer reaches a target in roughly a + b·log2(1 + distance /
// size) (Fitts's law); these constants put a 500-pixel reach at about 180 ms, a short hop at 50.
const (
	fittsBase   = 40 * time.Millisecond
	fittsPerBit = 30 * time.Millisecond
	targetSize  = 24.0 // the size the law's index assumes when the target's own is not given
	stepEvery   = 16 * time.Millisecond
	minSteps    = 3
	maxSteps    = 14
)

// TravelTime is how long a person takes to move the pointer dist pixels to a target.
func TravelTime(dist float64) time.Duration {
	if dist < 1 {
		return 0
	}
	return fittsBase + time.Duration(float64(fittsPerBit)*math.Log2(1+dist/targetSize))
}

// Path plans the pointer's travel from a to b within d: points along a cubic Bézier curve bowed to
// one side by up to a tenth of the distance, spaced by an ease-in-out so the pointer speeds up and
// slows into the target, each but the last moved by a jitter of up to a pixel that fades as the
// target nears. The last step is b exactly, so the press lands where it was aimed. A travel of
// under a pixel, or no time, is the single step to b.
func (p *Planner) Path(a, b Point, d time.Duration) []Step {
	dx, dy := b.X-a.X, b.Y-a.Y
	dist := math.Hypot(dx, dy)
	if dist < 1 || d <= 0 {
		return []Step{{At: b}}
	}
	n := int(d / stepEvery)
	n = min(max(n, minSteps), maxSteps)
	// The bow: perpendicular to the line, to one side or the other, near a third and two thirds.
	nx, ny := -dy/dist, dx/dist
	bow := (p.float()*2 - 1) * dist * 0.1
	c1 := Point{a.X + dx*0.3 + nx*bow*(0.6+0.4*p.float()), a.Y + dy*0.3 + ny*bow*(0.6+0.4*p.float())}
	c2 := Point{a.X + dx*0.7 + nx*bow*(0.6+0.4*p.float()), a.Y + dy*0.7 + ny*bow*(0.6+0.4*p.float())}
	steps := make([]Step, 0, n)
	per := d / time.Duration(n)
	for i := 1; i <= n; i++ {
		u := ease(float64(i) / float64(n))
		pt := bezier(a, c1, c2, b, u)
		if i < n {
			fade := 1 - u
			pt.X += (p.float()*2 - 1) * fade
			pt.Y += (p.float()*2 - 1) * fade
		} else {
			pt = b
		}
		// Steps not evenly spaced in time either: within a fifth of the mean either way.
		wait := per + time.Duration((p.float()*0.4-0.2)*float64(per))
		steps = append(steps, Step{At: pt, Wait: wait})
	}
	return fit(steps, d)
}

// fit scales the waits so they add up to no more than d, which the jitter in each can overshoot.
func fit(steps []Step, d time.Duration) []Step {
	var total time.Duration
	for _, s := range steps {
		total += s.Wait
	}
	if total <= d || total == 0 {
		return steps
	}
	for i := range steps {
		steps[i].Wait = time.Duration(float64(steps[i].Wait) * float64(d) / float64(total))
	}
	return steps
}

// ease is a smoothstep: slow at both ends, fastest in the middle.
func ease(u float64) float64 { return u * u * (3 - 2*u) }

func bezier(p0, p1, p2, p3 Point, u float64) Point {
	v := 1 - u
	a, b, c, d := v*v*v, 3*v*v*u, 3*v*u*u, u*u*u
	return Point{a*p0.X + b*p1.X + c*p2.X + d*p3.X, a*p0.Y + b*p1.Y + c*p2.Y + d*p3.Y}
}

// KeyDelays are the pauses before each of n keys, drawn from a typist's 30..90 ms and scaled down
// together when they would not fit in d: a long text is typed faster, never slower than the budget.
func (p *Planner) KeyDelays(n int, d time.Duration) []time.Duration {
	out := make([]time.Duration, n)
	var total time.Duration
	for i := range out {
		out[i] = p.Between(30*time.Millisecond, 90*time.Millisecond)
		total += out[i]
	}
	if total > d && total > 0 {
		for i := range out {
			out[i] = time.Duration(float64(out[i]) * float64(max(0, d)) / float64(total))
		}
	}
	return out
}

// WheelSteps splits a scroll of dy pixels into notches a wheel sends: a hundred pixels each,
// as a mouse's, at most n of them, the last one carrying what remains. A scroll needing more than n
// notches is left with what n reach; the caller brings the rest in at once.
func WheelSteps(dy float64, n int) []float64 {
	const notch = 100.0
	if math.Abs(dy) < 1 || n <= 0 {
		return nil
	}
	count := int(math.Ceil(math.Abs(dy) / notch))
	sign := 1.0
	if dy < 0 {
		sign = -1
	}
	if count > n {
		// Bigger notches rather than fewer pixels, up to five times a mouse's (a fast flick).
		per := math.Min(math.Abs(dy)/float64(n), notch*5)
		out := make([]float64, n)
		for i := range out {
			out[i] = sign * per
		}
		return out
	}
	out := make([]float64, 0, count)
	left := math.Abs(dy)
	for left >= 1 {
		step := math.Min(notch, left)
		out = append(out, sign*step)
		left -= step
	}
	return out
}
