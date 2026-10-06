// Instants and wall clocks. The planner draws in one time zone — the operator's, from the calendar
// settings — whatever zone the browser happens to be in, so every conversion between an instant the
// host sends and a day and minute on the grid goes through here, and nothing else calls `new Date()`
// on a wall time.

import type { Day } from "./dates";

const formatters = new Map<string, Intl.DateTimeFormat>();

/** One formatter per zone: building a DateTimeFormat costs far more than using one, and a week of
 *  events asks for hundreds of conversions per render. */
function formatter(timezone: string): Intl.DateTimeFormat {
  let found = formatters.get(timezone);
  if (!found) {
    try {
      found = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    } catch {
      // A zone name the browser does not know (an old tzdata) falls back to UTC rather than throwing
      // out of every render of the screen.
      found = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    }
    formatters.set(timezone, found);
  }
  return found;
}

function parts(value: Date, timezone: string): string {
  const fields = Object.fromEntries(formatter(timezone).formatToParts(value).map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}

/** The browser's own zone, the fallback when the settings name none. */
export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Whether the browser can draw times in this zone. */
export function validZone(timezone: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    return true;
  } catch {
    return false;
  }
}

/** The instant as `YYYY-MM-DDTHH:MM` on the zone's wall clock. */
export function wallInput(instant: string | number | Date, timezone: string): string {
  return parts(new Date(instant), timezone);
}

/** The instant as a day and minutes after its midnight on the zone's wall clock. */
export function toWall(instant: string | number | Date, timezone: string): { day: Day; minutes: number } {
  const text = wallInput(instant, timezone);
  return { day: text.slice(0, 10), minutes: Number(text.slice(11, 13)) * 60 + Number(text.slice(14, 16)) };
}

/** The wall clock `YYYY-MM-DDTHH:MM` in the zone as an ISO instant, or null when that time does not
 *  exist there (the hour skipped when the clocks go forward). */
export function wallInstant(value: string, timezone: string): string | null {
  const match = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)$/.exec(value);
  if (!match) return null;
  const [, year, month, day, hour, minute] = match.map(Number);
  const wanted = Date.UTC(year, month - 1, day, hour, minute);
  let guess = wanted;
  for (let pass = 0; pass < 3; pass++) {
    const shown = parts(new Date(guess), timezone);
    const pieces = shown.match(/\d+/g)!.map(Number);
    guess += wanted - Date.UTC(pieces[0], pieces[1] - 1, pieces[2], pieces[3], pieces[4]);
  }
  // A daylight-saving jump can make a wall-clock time nonexistent.
  return parts(new Date(guess), timezone) === value ? new Date(guess).toISOString() : null;
}

/** A day and minutes (which may run past midnight, 1440 being the next midnight) as an instant.

    Unlike `wallInstant` this never refuses: a slot dragged onto the skipped hour lands on the first
    minute that exists after it, because a drag that silently did nothing would read as a broken grid. */
export function fromWall(day: Day, minutes: number, timezone: string): string {
  const base = new Date(`${day}T00:00:00Z`);
  base.setUTCMinutes(base.getUTCMinutes() + minutes);
  const text = base.toISOString().slice(0, 16);
  const exact = wallInstant(text, timezone);
  if (exact) return exact;
  for (let step = 15; step <= 120; step += 15) {
    const later = new Date(base.getTime() + step * 60000).toISOString().slice(0, 16);
    const found = wallInstant(later, timezone);
    if (found) return found;
  }
  return base.toISOString();
}

/** Today on the zone's wall clock. */
export function todayIn(timezone: string, now: number = Date.now()): Day {
  return toWall(now, timezone).day;
}

/** The zone's offset from UTC at an instant, as "+03:00" — for the picker's labels. */
export function offsetLabel(timezone: string, at: number = Date.now()): string {
  const shown = wallInput(at, timezone);
  const asUtc = Date.parse(`${shown}:00Z`);
  const minutes = Math.round((asUtc - Math.floor(at / 60000) * 60000) / 60000);
  const sign = minutes < 0 ? "−" : "+";
  const abs = Math.abs(minutes);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

/** Every zone the browser knows, or a short list where it cannot say. */
export function allZones(): string[] {
  try {
    const zones = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone");
    if (zones?.length) return zones.includes("UTC") ? zones : ["UTC", ...zones];
  } catch {
    /* an older engine */
  }
  return ["UTC", "Europe/London", "Europe/Berlin", "Europe/Moscow", "Asia/Dubai", "Asia/Tokyo", "America/New_York", "America/Los_Angeles"];
}
