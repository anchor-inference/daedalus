package human

import (
	"math"
	"testing"
	"time"
)

func TestPathIsDeterministicForASeed(t *testing.T) {
	a, b := Point{10, 20}, Point{610, 420}
	one := New(7).Path(a, b, 160*time.Millisecond)
	two := New(7).Path(a, b, 160*time.Millisecond)
	if len(one) != len(two) {
		t.Fatalf("%d steps and %d", len(one), len(two))
	}
	for i := range one {
		if one[i] != two[i] {
			t.Fatalf("step %d: %+v and %+v", i, one[i], two[i])
		}
	}
	other := New(8).Path(a, b, 160*time.Millisecond)
	same := true
	for i := range other {
		if i < len(one) && other[i].At != one[i].At {
			same = false
		}
	}
	if same {
		t.Fatal("another seed planned the same travel")
	}
}

func TestPathShape(t *testing.T) {
	a, b := Point{100, 100}, Point{700, 400}
	dist := math.Hypot(b.X-a.X, b.Y-a.Y)
	for seed := uint64(0); seed < 200; seed++ {
		d := 50*time.Millisecond + time.Duration(seed)*time.Millisecond
		steps := New(seed).Path(a, b, d)
		if len(steps) < minSteps || len(steps) > maxSteps {
			t.Fatalf("seed %d: %d steps", seed, len(steps))
		}
		// It ends exactly where the press is aimed.
		if last := steps[len(steps)-1].At; last != b {
			t.Fatalf("seed %d: ends at %+v", seed, last)
		}
		var total time.Duration
		progress := 0.0
		for i, s := range steps {
			total += s.Wait
			if s.Wait < 0 {
				t.Fatalf("seed %d: a negative wait", seed)
			}
			// Never far off the line: the bow is a tenth of the distance, the jitter a pixel.
			if off := distToLine(s.At, a, b); off > dist*0.1+1.5 {
				t.Fatalf("seed %d step %d: %.1f px off the line", seed, i, off)
			}
			// Always onward: each step nearer the target than the one before, give or take the
			// jitter.
			along := ((s.At.X-a.X)*(b.X-a.X) + (s.At.Y-a.Y)*(b.Y-a.Y)) / (dist * dist)
			if along < progress-0.01 {
				t.Fatalf("seed %d step %d: went back from %.3f to %.3f", seed, i, progress, along)
			}
			progress = along
		}
		if total > d {
			t.Fatalf("seed %d: %s of waits in a budget of %s", seed, total, d)
		}
	}
}

func TestPathEasesInAndOut(t *testing.T) {
	steps := New(3).Path(Point{0, 0}, Point{1000, 0}, 14*stepEvery)
	gap := func(i int) float64 {
		from := Point{}
		if i > 0 {
			from = steps[i-1].At
		}
		return math.Hypot(steps[i].At.X-from.X, steps[i].At.Y-from.Y)
	}
	mid := len(steps) / 2
	if !(gap(0) < gap(mid) && gap(len(steps)-1) < gap(mid)) {
		t.Fatalf("the pointer does not speed up and slow down: first %.1f, middle %.1f, last %.1f", gap(0), gap(mid), gap(len(steps)-1))
	}
}

func TestShortOrUnbudgetedTravelIsOneStep(t *testing.T) {
	p := New(1)
	for _, steps := range [][]Step{p.Path(Point{5, 5}, Point{5.5, 5}, time.Second), p.Path(Point{0, 0}, Point{300, 300}, 0)} {
		if len(steps) != 1 || steps[0].Wait != 0 {
			t.Fatalf("%+v", steps)
		}
	}
}

func TestTravelTimeFollowsDistance(t *testing.T) {
	if TravelTime(0) != 0 || !(TravelTime(50) < TravelTime(500)) || TravelTime(500) > 250*time.Millisecond {
		t.Fatalf("travel times %s %s %s", TravelTime(0), TravelTime(50), TravelTime(500))
	}
}

func TestKeyDelaysFitTheBudget(t *testing.T) {
	p := New(5)
	short := p.KeyDelays(3, time.Second)
	for _, d := range short {
		if d < 30*time.Millisecond || d > 90*time.Millisecond {
			t.Fatalf("a key delay of %s", d)
		}
	}
	var total time.Duration
	for _, d := range p.KeyDelays(200, 300*time.Millisecond) {
		total += d
	}
	if total > 300*time.Millisecond {
		t.Fatalf("200 keys took %s of a 300 ms budget", total)
	}
}

func TestBudget(t *testing.T) {
	b := NewBudget(100 * time.Millisecond)
	if b.Take(60*time.Millisecond) != 60*time.Millisecond || b.Take(60*time.Millisecond) != 40*time.Millisecond ||
		b.Take(time.Millisecond) != 0 || b.Left() != 0 {
		t.Fatal("the budget handed out more than it had")
	}
}

func TestWheelSteps(t *testing.T) {
	sum := func(s []float64) float64 {
		total := 0.0
		for _, v := range s {
			total += v
		}
		return total
	}
	if s := WheelSteps(250, 8); len(s) != 3 || sum(s) != 250 || s[0] != 100 {
		t.Fatalf("250 px: %v", s)
	}
	if s := WheelSteps(-180, 8); len(s) != 2 || sum(s) != -180 {
		t.Fatalf("-180 px: %v", s)
	}
	// Too far for n notches: n bigger ones, at most five of a mouse's each.
	if s := WheelSteps(5000, 4); len(s) != 4 || sum(s) != 2000 {
		t.Fatalf("5000 px in 4: %v", s)
	}
	if s := WheelSteps(0.5, 4); s != nil {
		t.Fatalf("half a pixel: %v", s)
	}
}

func distToLine(p, a, b Point) float64 {
	dx, dy := b.X-a.X, b.Y-a.Y
	return math.Abs(dy*p.X-dx*p.Y+b.X*a.Y-b.Y*a.X) / math.Hypot(dx, dy)
}
