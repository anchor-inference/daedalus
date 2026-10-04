function parts(value: Date, timezone: string): string {
  const formatter = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const fields = Object.fromEntries(formatter.formatToParts(value).map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}

export function wallInput(instant: string, timezone: string): string {
  return parts(new Date(instant), timezone);
}

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
