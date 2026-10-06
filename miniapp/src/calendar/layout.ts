// Where things go on the grid: events that overlap in time stand side by side in a day's column,
// and anything that spans days (an all-day event, a conference) is one bar across the days it covers,
// on the lowest lane that is free.

/** A timed item in one day's column, in minutes after that day's midnight. */
export type Slot = { id: string; start: number; end: number };
/** Where a slot is drawn: `left` and `width` are fractions of the column. */
export type Placed = { left: number; width: number; column: number; columns: number };

/** How far a slot reaches under its right-hand neighbour, as a share of its own column. */
export const OVERLAP = 1.7;

/** Places overlapping slots side by side, the way dedicated calendars do.

    Slots are taken in start order (longer first on a tie) and grouped into clusters that overlap one
    another transitively; each slot takes the first column free at its start, and every slot of a
    cluster is as narrow as the cluster's widest moment needs. A slot then stretches right over the
    columns no overlapping neighbour uses, so a short meeting beside a long one does not leave a hole.
    A slot shorter than `minimum` minutes is laid out as if it lasted that long, because that is the
    height it is drawn at and two such slots would otherwise be drawn on top of each other. */
export function layoutDay(slots: Slot[], minimum = 20): Map<string, Placed> {
  const sorted = [...slots].map((s) => ({ ...s, end: Math.max(s.end, s.start + minimum) })).sort((a, b) => a.start - b.start || b.end - a.end || a.id.localeCompare(b.id));
  const result = new Map<string, Placed>();
  let cluster: { slot: Slot; column: number }[] = [];
  let clusterEnd = -Infinity;
  const flush = () => {
    const columns = Math.max(1, ...cluster.map((c) => c.column + 1));
    for (const item of cluster) {
      let span = 1;
      // Stretch right while no overlapping slot holds the next column.
      while (item.column + span < columns && !cluster.some((other) => other.column === item.column + span && other.slot.start < item.slot.end && item.slot.start < other.slot.end)) span++;
      const left = item.column / columns;
      // A slot with a neighbour to its right reaches under that neighbour, which is drawn on top of
      // it: three side by side in a week's narrow column were each a third of it, too thin for a
      // word, and the title at a slot's top left is what has to stay readable.
      const width = item.column + span < columns ? Math.min(1 - left, (span / columns) * OVERLAP) : span / columns;
      result.set(item.slot.id, { left, width, column: item.column, columns });
    }
    cluster = [];
  };
  for (const slot of sorted) {
    if (slot.start >= clusterEnd) flush();
    const taken = new Set(cluster.filter((c) => c.slot.end > slot.start).map((c) => c.column));
    let column = 0;
    while (taken.has(column)) column++;
    cluster.push({ slot, column });
    clusterEnd = Math.max(clusterEnd, slot.end);
  }
  flush();
  return result;
}

/** An item across days: indices into the row of days it is drawn on, `last` inclusive. */
export type Span = { id: string; first: number; last: number };

/** Gives every span the lowest lane where it collides with nothing, longest spans first so a long bar
 *  is not broken into steps by short ones laid before it. With `ordered`, the spans are laid in the
 *  order given: the month view puts its bars before its timed lines whatever their lengths. */
export function packLanes(spans: Span[], ordered = false): Map<string, number> {
  const sorted = ordered ? spans : [...spans].sort((a, b) => a.first - b.first || (b.last - b.first) - (a.last - a.first) || a.id.localeCompare(b.id));
  const lanes: Span[][] = [];
  const result = new Map<string, number>();
  for (const span of sorted) {
    let lane = 0;
    while (lanes[lane]?.some((other) => other.first <= span.last && span.first <= other.last)) lane++;
    (lanes[lane] ??= []).push(span);
    result.set(span.id, lane);
  }
  return result;
}

/** The part of a run of days `[startIndex, endIndex)` that falls inside a row of `length` days, or
 *  null when none does; with whether it continues past either edge (drawn as a cut end). */
export function clip(startIndex: number, endIndex: number, length: number): { first: number; last: number; before: boolean; after: boolean } | null {
  const first = Math.max(0, startIndex);
  const last = Math.min(length - 1, endIndex - 1);
  if (last < first) return null;
  return { first, last, before: startIndex < 0, after: endIndex > length };
}
