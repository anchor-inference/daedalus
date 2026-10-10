// One icon set for the whole app: 24-unit stroked paths, drawn with the current text colour.

export type IconName =
  | "back" | "more" | "plus" | "up" | "stop" | "model" | "terminal" | "file" | "pen" | "search" | "globe" | "attach" | "image"
  | "question" | "skill" | "spawn" | "bulb" | "wrench" | "clock" | "calendar" | "plug" | "dot" | "compact"
  | "folder" | "settings" | "bots" | "inbox" | "board" | "changes" | "chart" | "loop" | "pause" | "play" | "trash" | "check" | "close" | "send"
  | "download" | "share" | "split" | "key" | "link" | "unlink" | "down" | "copy" | "paste" | "columns" | "eye" | "mic" | "fork" | "undo" | "phone" | "expand" | "external" | "reload" | "forward" | "panel" | "chevron" | "bolt" | "volume" | "mute" | "lock" | "bell" | "shield" | "conductor" | "journal" | "alert" | "compass" | "user" | "ask" | "archive" | "braces" | "sidebar" | "reply" | "flag" | "menu" | "pin" | "pushpin"
  | "compose" | "grid" | "vdots" | "camera" | "offline" | "logo" | "sliders";

const PATHS: Record<IconName, string> = {
  back: "M15 18l-6-6 6-6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  plus: "M12 5v14M5 12h14",
  up: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  model: "M4 12l8-8 8 8-8 8-8-8z",
  terminal: "M4 5h16v14H4zM7 9l3 3-3 3M12 15h5",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  pen: "M4 20l4-1 11-11-3-3L5 16zM13 6l3 3",
  search: "M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM20 20l-4-4",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
  attach: "M21 12l-8 8a5 5 0 0 1-7-7l9-9a3 3 0 0 1 4 4l-9 9a1 1 0 0 1-2-2l8-8",
  image: "M4 5h16v14H4zM8 13l3-3 4 4 2-2 3 3",
  question: "M9 9a3 3 0 1 1 4 3c-1 .5-1 1-1 2M12 17h.01",
  // The same question in a ring: what waits for the operator, where a bare "?" would read as help.
  ask: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .8-1 1.5v.3M12 16.8h.01",
  skill: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  spawn: "M12 3v6M12 15v6M3 12h6M15 12h6",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3 11v1h6v-1a6 6 0 0 0-3-11z",
  wrench: "M14 4a5 5 0 0 0 6 6l-9 9-3-3 9-9a5 5 0 0 0-3-3z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
  calendar: "M4 5h16v16H4zM4 9h16M8 3v4M16 3v4M8 13h3M14 13h3M8 17h3",
  plug: "M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3",
  dot: "M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  compact: "M4 7h16M4 12h10M4 17h6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  archive: "M3 4h18v4H3zM5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M10 12h4",
  braces: "M8 4c-2 0-2 1-2 3v2c0 1.5-.8 2.4-2 3 1.2.6 2 1.5 2 3v2c0 2 0 3 2 3M16 4c2 0 2 1 2 3v2c0 1.5.8 2.4 2 3-1.2.6-2 1.5-2 3v2c0 2 0 3-2 3",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  bots: "M5 8h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zM12 4v4M9 13h.01M15 13h.01M9 17h6",
  inbox: "M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6z",
  board: "M4 5h16M4 12h16M4 19h10",
  changes: "M6 3v12M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9c0 4-4 4-8 6",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  loop: "M4 12a8 8 0 0 1 14-5.3L20 8M20 4v4h-4M20 12a8 8 0 0 1-14 5.3L4 16M4 20v-4h4",
  pause: "M8 6h3v12H8zM13 6h3v12h-3z",
  play: "M7 5l12 7-12 7z",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  check: "M5 12l5 5 9-11",
  close: "M6 6l12 12M18 6L6 18",
  send: "M4 12l16-8-6 16-2-6z",
  download: "M12 4v12M6 10l6 6 6-6M4 20h16",
  share: "M4 12v8h16v-8M12 3v12M7 8l5-5 5 5",
  split: "M4 5h16v14H4zM12 5v14",
  key: "M15 3a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM10 14l-7 7M6 18l2 2M9 15l2 2",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
  // A terminal in the sandbox: it writes only to the project's folders.
  shield: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  unlink: "M10 14a4 4 0 0 0 5.7 0l1.3-1.3M14 10a4 4 0 0 0-5.7 0l-1.3 1.3M4 4l16 16",
  down: "M12 5v14M5 12l7 7 7-7",
  copy: "M9 9h11v11H9zM15 9V4H4v11h5",
  // A clipboard: what comes off it goes onto the page.
  paste: "M9 3h6v4H9zM9 5H5v16h14V5h-4M9 13h6M9 17h4",
  // A branch leaving the line it came from: what forking a session from a turn does.
  fork: "M7 4a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM17 4a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM12 15a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM7 9v1a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3V9M12 13v2",
  // The undo arrow: the history goes back to this point.
  undo: "M4 9h11a5 5 0 0 1 0 10H8M4 9l4-4M4 9l4 4",
  reply: "M9 14l-5-5 5-5M4 9h10a6 6 0 0 1 6 6v4",
  columns: "M3 5h18v14H3zM12 5v14",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM6 11a6 6 0 0 0 12 0M12 17v4M9 21h6",
  phone: "M8 3h8a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM11 18h2",
  expand: "M15 4h5v5M20 4l-6 6M9 20H4v-5M4 20l6-6",
  external: "M14 4h6v6M20 4l-9 9M18 13v6H5V6h6",
  reload: "M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5",
  forward: "M9 18l6-6-6-6",
  panel: "M3 5h18v14H3zM15 5v14",
  // The panel's mirror, a column on the left: the file tree beside a previewed file.
  sidebar: "M3 5h18v14H3zM9 5v14",
  chevron: "M6 9l6 6 6-6",
  bolt: "M13 3L6 13h6l-2 8 8-11h-6l1-7z",
  volume: "M4 10v4h3l5 4V6L7 10H4zM16 9.5a3.5 3.5 0 0 1 0 5",
  mute: "M4 10v4h3l5 4V6L7 10H4zM16 9l5 6M21 9l-5 6",
  bell: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0",
  // The orchestrator: one point that hands work down to three.
  alert: "M12 3l10 18H2zM12 10v4M12 17.5h.01",
  conductor: "M12 3a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM12 8v4M5 16v-2h14v2M5 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM12 12v4M12 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM19 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  journal: "M6 3h11a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6zM9 3v18M12 8h3M12 12h3",
  compass: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM15.5 8.5l-2 5-5 2 2-5z",
  user: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM4.5 20.5c.8-3.5 3.8-5.5 7.5-5.5s6.7 2 7.5 5.5",
  // A task's priority, and the planner's drawer of calendars and tasks on a phone.
  flag: "M5 21V4M5 4h11l-2 4 2 4H5",
  menu: "M4 7h16M4 12h16M4 17h16",
  // An event's place.
  pin: "M12 21s-6-5.6-6-11a6 6 0 0 1 12 0c0 5.4-6 11-6 11zM12 8a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  // A chat or a project held at the top of the sidebar: a drawing pin, not the map pin above.
  pushpin: "M12 16v5M8 4h8M9.5 4v5.5L7 13v3h10v-3l-2.5-3.5V4",
  // A spoken conversation, as opposed to the microphone of a voice note: the phone composer's white
  // circle when the field is empty.
  // A page and a pen: a new chat, the one creation action of the phone drawer.
  compose: "M12 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6M18.4 2.6a2 2 0 0 1 2.9 2.9L12 15l-4 1 1-4z",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  vdots: "M12 5h.01M12 12h.01M12 19h.01",
  camera: "M4 8a2 2 0 0 1 2-2h2l1.5-2h5L16 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM12 9.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z",
  offline: "M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 13a10 10 0 0 1 5.2-2.8M19 13a10 10 0 0 0-2.3-1.6M2 8.8a15 15 0 0 1 4.2-2.7M22 8.8A15 15 0 0 0 11 5M12 20h.01",
  // The brand's mark: a frame inside a frame around a point, drawn in the accent on the phone's home.
  logo: "M10 3.5H3.5v17h17v-17H14M10 16.5H7.5v-9h9v9H14M12 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6z",
  // A list's options: what it shows and how, behind one glyph on a phone's board.
  sliders: "M4 7h9M17 7h3M4 17h3M11 17h9M15 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM9 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
};

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg className={`ic ic-${name}`} viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
