// The rows a catalogue of downloadable models is drawn in: the voices that read answers aloud and the
// models that turn speech into words. They are the same list — a name with a few tags, one line of
// what it is, one line of what it costs, two scores to compare, and the actions at the row's end — so
// the two screens share the row and differ only in what they put in it.
//
// It is a list of rows in one card, not a wall of bordered tiles: a dozen tiles each with its own
// border and its own buttons read as a dozen things to look at, where the choice is one thing made of
// a dozen lines that can be compared by running an eye down them.

import type React from "react";

/** One 0-100 score as a bar. Two of these side by side are the whole comparison most people make. */
export function Meter({ label, value }: { label: string; value: number }) {
  return (
    <div className="cat-meter">
      <span>{label}</span>
      <span className="cat-track">
        <span className="cat-fill" style={{ width: `${Math.max(3, Math.min(100, value))}%` }} />
      </span>
    </div>
  );
}

/** How far a download has got, as a bar and a word beside it. */
export function CatalogueProgress({ fraction, label }: { fraction: number; label: string }) {
  return (
    <div className="cat-progress">
      <span className="cat-track">
        <span className="cat-fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
      </span>
      <span>{label}</span>
    </div>
  );
}

/** One entry. `badges` follow the name, `facts` is the grey line under the note, `meters` are the two
 *  scores under them, `status` is what is happening to it now (a download, a failure) and `actions` end the row. */
export function CatalogueRow({ using, name, badges, note, facts, meters, status, actions, ...data }: {
  using?: boolean;
  name: React.ReactNode;
  badges?: React.ReactNode;
  note?: React.ReactNode;
  facts?: React.ReactNode;
  meters?: React.ReactNode;
  status?: React.ReactNode;
  actions: React.ReactNode;
  [data: `data-${string}`]: string | undefined;
}) {
  return (
    <div className="cat-row" data-using={using ? "" : undefined} {...data}>
      <div className="cat-main">
        <div className="cat-name">
          <b>{name}</b>
          {badges}
        </div>
        {note && <div className="cat-note">{note}</div>}
        {facts && <div className="cat-facts">{facts}</div>}
        {meters && <div className="cat-meters">{meters}</div>}
        {status}
      </div>
      <div className="cat-actions">{actions}</div>
    </div>
  );
}
