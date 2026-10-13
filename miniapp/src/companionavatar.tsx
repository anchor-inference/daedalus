// The companion's face: four parameters and one small SVG, so the page header, the Agents list and
// (later) the desktop's tray draw the same face at their own sizes from what `GET /api/companion`
// returns. Drawn in a 64-unit square; the body sits low so a hat or a halo has room above it.

import { t } from "./i18n";
import { useQuery } from "./store";

export type CompanionShape = "orb" | "drop" | "square" | "star" | "cloud";
export type CompanionEyes = "dots" | "wide" | "sleepy" | "happy" | "visor";
export type CompanionAccessory = "none" | "antenna" | "halo" | "hat" | "glasses" | "bow";
export type AvatarParams = { shape: CompanionShape; color: string; eyes: CompanionEyes; accessory: CompanionAccessory };

export const SHAPES: CompanionShape[] = ["orb", "drop", "square", "star", "cloud"];
export const EYES: CompanionEyes[] = ["dots", "wide", "sleepy", "happy", "visor"];
export const ACCESSORIES: CompanionAccessory[] = ["none", "antenna", "halo", "hat", "glasses", "bow"];
/** A few colours that read on both themes; the editor also takes any #rrggbb. */
export const SWATCHES = ["#7c6cf0", "#3fa7f5", "#2bb673", "#f5a524", "#ef5d7a", "#9aa3b2"];

export const DEFAULT_AVATAR: AvatarParams = { shape: "orb", color: "#7c6cf0", eyes: "dots", accessory: "none" };

/** What the server sent, with anything it did not send (an older host) or anything unknown put back to the default. */
export function avatarOf(raw: Partial<Record<keyof AvatarParams, string>> | null | undefined): AvatarParams {
  const pick = <T extends string>(value: string | undefined, allowed: T[], fallback: T): T => (allowed.includes(value as T) ? (value as T) : fallback);
  return {
    shape: pick(raw?.shape, SHAPES, DEFAULT_AVATAR.shape),
    color: /^#[0-9a-fA-F]{6}$/.test(raw?.color ?? "") ? (raw!.color as string) : DEFAULT_AVATAR.color,
    eyes: pick(raw?.eyes, EYES, DEFAULT_AVATAR.eyes),
    accessory: pick(raw?.accessory, ACCESSORIES, DEFAULT_AVATAR.accessory),
  };
}

function body(shape: CompanionShape, color: string) {
  switch (shape) {
    case "drop":
      return <path d="M32 12 C 40 24, 54 32, 54 42 A 22 20 0 0 1 10 42 C 10 32, 24 24, 32 12 Z" fill={color} />;
    case "square":
      return <rect x="11" y="18" width="42" height="40" rx="11" fill={color} />;
    case "star":
      return <path d="M32 13 L38.5 27 L54 29 L42.5 39.5 L45.5 55 L32 47.5 L18.5 55 L21.5 39.5 L10 29 L25.5 27 Z" fill={color} stroke={color} strokeWidth="5" strokeLinejoin="round" />;
    case "cloud":
      return (
        <g fill={color}>
          <circle cx="22" cy="40" r="13" />
          <circle cx="42" cy="40" r="13" />
          <circle cx="32" cy="30" r="14" />
          <rect x="16" y="38" width="32" height="16" rx="8" />
        </g>
      );
    default:
      return <circle cx="32" cy="38" r="22" fill={color} />;
  }
}

function eyes(kind: CompanionEyes) {
  const ink = "#1b1d24";
  switch (kind) {
    case "wide":
      return (
        <g>
          <circle cx="24" cy="38" r="6" fill="#fff" />
          <circle cx="40" cy="38" r="6" fill="#fff" />
          <circle cx="25" cy="39" r="3" fill={ink} />
          <circle cx="41" cy="39" r="3" fill={ink} />
        </g>
      );
    case "sleepy":
      return <path d="M19 38 Q24 42 29 38 M35 38 Q40 42 45 38" stroke={ink} strokeWidth="2.6" fill="none" strokeLinecap="round" />;
    case "happy":
      return <path d="M19 40 Q24 34 29 40 M35 40 Q40 34 45 40" stroke={ink} strokeWidth="2.6" fill="none" strokeLinecap="round" />;
    case "visor":
      return (
        <g>
          <rect x="16" y="33" width="32" height="10" rx="5" fill={ink} />
          <rect x="20" y="36" width="9" height="4" rx="2" fill="#7ef0ff" />
          <rect x="35" y="36" width="9" height="4" rx="2" fill="#7ef0ff" />
        </g>
      );
    default:
      return (
        <g fill={ink}>
          <circle cx="25" cy="38" r="3.2" />
          <circle cx="39" cy="38" r="3.2" />
        </g>
      );
  }
}

function accessory(kind: CompanionAccessory, color: string) {
  const ink = "#1b1d24";
  switch (kind) {
    case "antenna":
      return (
        <g>
          <line x1="32" y1="17" x2="32" y2="6" stroke={ink} strokeWidth="2.4" strokeLinecap="round" />
          <circle cx="32" cy="5" r="3.4" fill={color} stroke={ink} strokeWidth="1.6" />
        </g>
      );
    case "halo":
      return <ellipse cx="32" cy="8" rx="13" ry="3.6" fill="none" stroke="#f5c542" strokeWidth="2.8" />;
    case "hat":
      return (
        <g fill={ink}>
          <rect x="18" y="15" width="28" height="3.6" rx="1.8" />
          <rect x="23" y="3" width="18" height="13" rx="2" />
        </g>
      );
    case "glasses":
      return (
        <g fill="none" stroke={ink} strokeWidth="2">
          <circle cx="25" cy="38" r="6.5" />
          <circle cx="39" cy="38" r="6.5" />
          <line x1="31.5" y1="38" x2="32.5" y2="38" />
        </g>
      );
    case "bow":
      return (
        <g fill="#ef5d7a" stroke={ink} strokeWidth="1.2" strokeLinejoin="round">
          <path d="M40 15 L48 10 L48 20 Z" />
          <path d="M40 15 L32 10 L32 20 Z" />
          <circle cx="40" cy="15" r="2.4" />
        </g>
      );
    default:
      return null;
  }
}

/** The face. `state` is the profile's one word; the stylesheet gives `thinking` and `listening` their motion. */
export function CompanionAvatar({ avatar, size = 32, state, name }: { avatar: AvatarParams; size?: number; state?: string; name?: string }) {
  const { shape, color, eyes: eyeKind, accessory: extra } = avatarOf(avatar);
  return (
    <svg
      className={`companion-avatar${state ? ` state-${state}` : ""}`}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label={name ? t("companion.avatar", { name }) : undefined}
      aria-hidden={name ? undefined : true}
    >
      {body(shape, color)}
      <ellipse cx="24" cy="26" rx="7" ry="4" fill="#fff" opacity="0.22" />
      {eyes(eyeKind)}
      {accessory(extra, color)}
    </svg>
  );
}

/** The companion's face wherever its folder or session is listed: read from the profile every
 *  client shares, so a new colour chosen on its page reaches the Agents list on the next poll. */
export function CompanionFace({ size = 16 }: { size?: number }) {
  const { data } = useQuery<{ name?: string; avatar?: Partial<AvatarParams> }>("/api/companion", { staleMs: 60000 });
  return <CompanionAvatar avatar={avatarOf(data?.avatar)} size={size} />;
}
