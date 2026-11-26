// Daedalus's own face, flat: the dark rounded head with two eyes, the mint crest and the mint wings
// that the installable icon (public/icons/icon-512.png) and the 3D companion (chibi2.js in the desktop
// launcher's assets) both draw. The coordinates are the icon's own, measured on its 512-unit square,
// so the mark and the icon stay one figure. What differs is for the sizes the mark is shown at (22 to
// 36 px tall): the wings are drawn at 70 % of their length around their root, because at full length
// the figure is 1.75 times wider than tall and its head would shrink to a dozen pixels; the feathers
// and the crest are thickened by a stroke of their own colour, and the outline, eyes and pupils are
// heavier than the icon's, because at 22 px the icon's own widths fall under a pixel and the mint
// read as nothing at all.

/** The view box: the figure with the shortened wings, the thickened mint and a little air, in icon units. */
const BOX = { x: 64, y: 120, w: 384, h: 258 };
/** Where the wings meet the head; they are shortened toward this point. */
const ROOT = { x: 131, y: 272 };
const WING_SCALE = 0.7;

/** The three feathers of the left wing, lightest on top, as the icon draws them. */
const FEATHERS = [
  { d: "M71 166 L131 254 L117 263 Z", tone: "1" },
  { d: "M51 222 L130 270 L118 287 Z", tone: "2" },
  { d: "M54 279 L130 289 L122 313 Z", tone: "3" },
];

function Wing({ mirror = false }: { mirror?: boolean }) {
  const shorten = `translate(${ROOT.x} ${ROOT.y}) scale(${WING_SCALE}) translate(${-ROOT.x} ${-ROOT.y})`;
  return (
    <g transform={mirror ? `translate(512 0) scale(-1 1) ${shorten}` : shorten}>
      {FEATHERS.map((f) => <path key={f.tone} className={`mascot-mint mascot-mint-${f.tone}`} d={f.d} />)}
    </g>
  );
}

/** The mark, `size` pixels tall; it is about 1.5 times as wide. Decorative unless given a `label`. */
export function Mascot({ size = 22, label, className = "" }: { size?: number; label?: string; className?: string }) {
  const width = Math.round((size * BOX.w) / BOX.h);
  return (
    <svg
      className={`mascot ${className}`.trim()}
      width={width}
      height={size}
      viewBox={`${BOX.x} ${BOX.y} ${BOX.w} ${BOX.h}`}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      <Wing />
      <Wing mirror />
      <path className="mascot-mint mascot-mint-1 mascot-crest" d="M256 139 Q249 158 231 173 L281 173 Q263 158 256 139 Z" transform="translate(256 173) scale(1.3) translate(-256 -173)" />
      <rect className="mascot-head" x="134" y="172" width="244" height="198" rx="48" />
      <circle className="mascot-eye" cx="200.5" cy="279" r="24" />
      <circle className="mascot-eye" cx="310.5" cy="279" r="24" />
      <circle className="mascot-pupil" cx="200.5" cy="279" r="10" />
      <circle className="mascot-pupil" cx="310.5" cy="279" r="10" />
    </svg>
  );
}
