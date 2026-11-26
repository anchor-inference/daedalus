// Appearance is this browser's look: the named themes, then the few adjustments that are not a new theme.

import { useState } from "react";
import { usePetModel, usePetPreference } from "../ui/pet";
import { COLOR_KEYS, FONT_CATALOG, FONT_ROLES, PROSE_MAX, PROSE_MIN, usualProse, FontRole, THEMES, ThemeId, fontUrlAllowed, readPrefs, resolvedTheme, resetColors, updatePrefs, type ColorKey, type Column, type Leading, type Prefs, type Radius, type Scale } from "../appearance";
import { LANGS, t, useLang } from "../i18n";
import { Button, Combobox, Segmented, Select, Switch } from "../ui/primitives";
import { Row, SetBlock, SetFoot, SetGroup } from "../settingsrow";
import { type Settings } from "../api";
import { useQuery } from "../store";

function usePrefs(): [Prefs, (patch: Partial<Prefs>) => void, (next: Prefs) => void] {
  const [prefs, setPrefs] = useState(readPrefs);
  return [prefs, (patch) => setPrefs(updatePrefs(patch)), setPrefs];
}

function familyOf(prefs: Prefs, role: FontRole): string {
  return role === "ui" ? prefs.fontUi : role === "prose" ? prefs.fontProse : prefs.fontCode;
}

function urlOf(prefs: Prefs, role: FontRole): string {
  return role === "ui" ? prefs.fontUiUrl : role === "prose" ? prefs.fontProseUrl : prefs.fontCodeUrl;
}

/** Answer text, one pixel at a time. The number shown is always the size drawn, so "usual" shows what
 *  the current interface step makes of it; the reset appears only while a size is pinned. */
function ProseStepper({ prefs, update }: { prefs: Prefs; update: (patch: Partial<Prefs>) => void }) {
  const phone = window.matchMedia?.("(max-width: 1023px)")?.matches ?? false;
  const usual = Math.round(usualProse(prefs.scale, phone));
  const size = prefs.prose ?? usual;
  const set = (next: number) => update({ prose: Math.min(PROSE_MAX, Math.max(PROSE_MIN, next)) });
  return (
    <div className="prose-stepper" data-pinned={prefs.prose !== null ? "true" : "false"}>
      <Button size="sm" aria-label={t("theme.prose.smaller")} disabled={size <= PROSE_MIN} onClick={() => set(size - 1)}>{"\u2212"}</Button>
      <output className="prose-stepper-value" aria-live="polite">{t("theme.prose.px", { n: size })}{prefs.prose === null ? ` \u00b7 ${t("theme.prose.auto")}` : ""}</output>
      <Button size="sm" aria-label={t("theme.prose.larger")} disabled={size >= PROSE_MAX} onClick={() => set(size + 1)}>+</Button>
      <Button size="sm" variant="quiet" disabled={prefs.prose === null} onClick={() => update({ prose: null })}>{t("theme.prose.reset")}</Button>
    </div>
  );
}

/** What a face's combobox offers: the system face, a face of the catalogue, or what was typed — a name
 *  to fetch from Google Fonts, or an address (a stylesheet or a .woff2 file). */
type FaceChoice = { kind: "system" } | { kind: "face"; family: string; local: boolean } | { kind: "load"; name: string } | { kind: "address"; address: string };

/** The family and the address that choosing a typed address gives, as the form it replaces did: a
 *  Google Fonts stylesheet names its family, a .woff2 file is given a family of its own per role. */
function familyForAddress(address: string, role: FontRole, current: string): string {
  let family = current;
  try {
    const parsed = new URL(address).searchParams.get("family");
    if (parsed) family = decodeURIComponent(parsed.split(":")[0].replace(/\+/g, " "));
  } catch { /* the allow check already rejected a bad address */ }
  if (/\.woff2$/i.test(address)) family = role === "ui" ? "Daedalus UI" : role === "prose" ? "Daedalus Prose" : "Daedalus Code";
  return family;
}

/** Two sections draw from here: the look (`theme`) — language, theme, colours, contrast, the companion
 *  — and `text`: typefaces, sizes, the column and how messages sit in it. One component still, since
 *  both read and write the same preferences of this browser. */
export function AppearancePanel({ part }: { part: "theme" | "text" }) {
  const [prefs, update, replace] = usePrefs();
  const [lang, pickLang] = useLang();
  const [pet, setPet] = usePetPreference();
  const [petModel, setPetModel] = usePetModel();
  const petSettings = useQuery<Settings>(pet ? "/api/settings" : null);
  // What was typed in each face's field; null while it shows the face in use.
  const [typed, setTyped] = useState<Partial<Record<FontRole, string>>>({});
  const [badAddress, setBadAddress] = useState<FontRole | null>(null);
  const env = { scheme: null as null, prefersDark: window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? true };
  let scheme: "dark" | "light" | null = null;
  try {
    const stored = localStorage.getItem("daedalus.scheme");
    scheme = stored === "dark" || stored === "light" ? stored : null;
  } catch {
    /* private mode */
  }
  const resolved = resolvedTheme(prefs, { scheme, prefersDark: env.prefersDark });

  const setFace = (role: FontRole, family: string, address = "") => {
    if (role === "ui") update({ fontUi: family, fontUiUrl: address });
    else if (role === "prose") update({ fontProse: family, fontProseUrl: address });
    else update({ fontCode: family, fontCodeUrl: address });
    setTyped((now) => ({ ...now, [role]: undefined }));
    setBadAddress(null);
  };
  const choices = (role: FontRole): FaceChoice[] => {
    const query = (typed[role] ?? "").trim();
    const needle = query.toLowerCase();
    if (/^https?:\/\//i.test(query)) return [{ kind: "address", address: query }];
    const faces = FONT_CATALOG.filter((face) => !needle || face.family.toLowerCase().includes(needle));
    const known = FONT_CATALOG.some((face) => face.family.toLowerCase() === needle);
    return [
      ...(needle ? [] : [{ kind: "system" } as FaceChoice]),
      ...faces.map((face): FaceChoice => ({ kind: "face", family: face.family, local: !!face.local })),
      ...(needle && !known ? [{ kind: "load", name: query } as FaceChoice] : []),
    ];
  };
  const choose = (role: FontRole, choice: FaceChoice) => {
    if (choice.kind === "system") setFace(role, "");
    else if (choice.kind === "face") setFace(role, choice.family);
    else if (choice.kind === "load") setFace(role, choice.name);
    else if (!fontUrlAllowed(choice.address)) setBadAddress(role);
    else setFace(role, familyForAddress(choice.address, role, familyOf(prefs, role)), choice.address);
  };
  const faceLabel = (choice: FaceChoice) => choice.kind === "system" ? t("theme.font.system") : choice.kind === "face" ? choice.family : choice.kind === "load" ? t("theme.font.load", { name: choice.name }) : t("theme.font.use");

  const color = (key: ColorKey, fallback: string) => prefs.colors[key] ?? fallback;

  if (part === "text") return (
    <>
      <SetGroup title={t("theme.font")} help={t("theme.font.lead")} data-setting="appearance.font">
        {FONT_ROLES.map((role) => {
          const family = familyOf(prefs, role);
          const address = urlOf(prefs, role);
          return (
            <Row key={role} title={t(`theme.font.${role}`)} desc={badAddress === role ? <span className="attn">{t("theme.font.url.bad")}</span> : address || undefined} data-font-role={role}>
              <Combobox<FaceChoice>
                className="set-face"
                label={t(`theme.font.${role}`)}
                placeholder={t("theme.font.search")}
                items={choices(role)}
                value={null}
                query={typed[role] ?? (family || t("theme.font.system"))}
                onQuery={(query) => setTyped((now) => ({ ...now, [role]: query }))}
                onChoose={(choice) => choose(role, choice)}
                toLabel={faceLabel}
                render={(choice) => (
                  <>
                    <span style={choice.kind === "face" && choice.local ? { fontFamily: `"${choice.family}"` } : undefined}>{faceLabel(choice)}</span>
                    {choice.kind === "face" && choice.local && <span className="sub"> · {t("theme.font.builtin")}</span>}
                  </>
                )}
              />
            </Row>
          );
        })}
        <SetBlock className="set-specimen">
          {FONT_ROLES.map((role) => (
            <p key={role} data-role={role}>
              <span className="set-specimen-role">{t(`theme.font.${role}`)}</span>
              <span style={{ fontFamily: familyOf(prefs, role) ? `"${familyOf(prefs, role)}"` : role === "code" ? "var(--mono)" : undefined }}>{t("theme.font.sample")}</span>
            </p>
          ))}
        </SetBlock>
      </SetGroup>

      <SetGroup title={t("theme.size")} help={t("theme.size.lead")}>
        <Row title={t("theme.size.step")} data-setting="appearance.scale"><Segmented value={prefs.scale} onChange={(scale: Scale) => update({ scale })} options={(["sm", "md", "lg", "xl"] as const).map((id) => ({ id, label: t(`theme.scale.${id}`) }))} /></Row>
        <Row title={t("theme.prose")} desc={t("theme.prose.sub")} data-setting="appearance.prose"><ProseStepper prefs={prefs} update={update} /></Row>
        <SetBlock className="set-specimen"><p className="prose-stepper-preview" style={{ fontSize: `${prefs.prose ?? Math.round(usualProse(prefs.scale, window.matchMedia?.("(max-width: 1023px)")?.matches ?? false))}px` }}>{t("theme.prose.preview")}</p></SetBlock>
        <Row title={t("theme.leading")} data-setting="appearance.leading"><Segmented value={prefs.leading} onChange={(leading: Leading) => update({ leading })} options={(["tight", "normal", "open"] as const).map((id) => ({ id, label: t(`theme.leading.${id}`) }))} /></Row>
        <Row title={t("theme.column")} desc={t("theme.column.sub")} data-setting="appearance.column"><Segmented value={prefs.column} onChange={(column: Column) => update({ column })} options={(["narrow", "normal", "wide"] as const).map((id) => ({ id, label: t(`theme.column.${id}`) }))} /></Row>
      </SetGroup>

      <SetGroup title={t("theme.layout")}>
        <Row title={t("theme.radius")} data-setting="appearance.radius"><Segmented value={prefs.radius} onChange={(radius: Radius) => update({ radius })} options={(["sharp", "normal", "round"] as const).map((id) => ({ id, label: t(`theme.radius.${id}`) }))} /></Row>
        <Row title={t("theme.motion")} data-setting="appearance.motion"><Segmented value={prefs.motion} onChange={(motion) => update({ motion })} options={([{ id: "full" as const, label: t("theme.motion.full") }, { id: "reduce" as const, label: t("theme.motion.reduce") }])} /></Row>
        <Row title={t("theme.replies")} desc={t("theme.replies.sub")} data-setting="appearance.replies"><Segmented label={t("theme.replies")} value={prefs.replies} onChange={(replies) => update({ replies })} options={([{ id: "split" as const, label: t("theme.replies.split") }, { id: "merged" as const, label: t("theme.replies.merged") }])} /></Row>
        <Row title={t("theme.bubble")} data-setting="appearance.bubble"><Segmented value={prefs.bubble} onChange={(bubble) => update({ bubble })} options={([{ id: "card" as const, label: t("theme.bubble.card") }, { id: "plain" as const, label: t("theme.bubble.plain") }])} /></Row>
      </SetGroup>
    </>
  );

  return (
    <>
      {/* The language and the companion live here, not in the More launcher: they are settings of this
          browser like the theme, and a menu of places had grown two switches nobody looked for there. */}
      <SetGroup title={t("settings.group.langtheme")}>
        <Row title={t("settings.sec.language")} desc={t("lang.hint")} data-setting="language">
          <Segmented label={t("lang.pick")} value={lang} onChange={pickLang} options={LANGS.map((id) => ({ id, label: id.toUpperCase() }))} />
        </Row>
      </SetGroup>
      <SetGroup title={t("theme.themes")} help={t("theme.lead")} data-setting="appearance.theme">
        <SetBlock>
          <div className="settings-themes" role="listbox" aria-label={t("settings.sec.appearance")}>
            {THEMES.map((item) => (
              <button key={item.id} type="button" className={resolved.id === item.id ? "on" : ""} aria-pressed={resolved.id === item.id} onClick={() => update({ follow: false, theme: item.id })}>
                {/* The page with the frame along its left edge: Dark and Soot differ more in the frame than in the page. */}
                <span className="settings-swatch" style={{ background: item.wash, boxShadow: `inset 0 0 0 1px var(--line), inset 18px 0 0 ${item.side}` }} />
                <span>{t(`theme.${item.id}`)}</span>
              </button>
            ))}
          </div>
        </SetBlock>
        <Row title={t("theme.follow")} data-setting="appearance.follow" desc={t("theme.follow.sub", { dark: t(`theme.${prefs.dark}`), light: t(`theme.${prefs.light}`) })}>
          <Switch checked={prefs.follow} onChange={(follow) => update({ follow })} label={t("theme.follow")} />
        </Row>
        {prefs.follow && (
          <>
            <Row title={t("theme.follow.dark")}>
              <Select label={t("theme.follow.dark")} value={prefs.dark} onChange={(dark: ThemeId) => update({ dark })} options={THEMES.filter((item) => !item.light).map((item) => ({ id: item.id, label: t(`theme.${item.id}`) }))} />
            </Row>
            <Row title={t("theme.follow.light")}>
              <Select label={t("theme.follow.light")} value={prefs.light} onChange={(light: ThemeId) => update({ light })} options={THEMES.filter((item) => item.light).map((item) => ({ id: item.id, label: t(`theme.${item.id}`) }))} />
            </Row>
          </>
        )}
      </SetGroup>

      <SetGroup title={t("theme.colors")} help={t("theme.colors.lead")} data-setting="appearance.colors">
        {COLOR_KEYS.map((key) => {
          const fallback = key === "bg" ? resolved.side : key === "surface" ? resolved.surface : key === "fg" ? resolved.fg : resolved.fg2;
          const value = color(key, fallback);
          return (
            <Row key={key} title={t(`theme.color.${key}`)} htmlFor={`theme-color-${key}`}>
              <span className="settings-color">
                <span className="mono sub">{value}</span>
                <input id={`theme-color-${key}`} type="color" value={value} aria-label={t(`theme.color.${key}`)} onChange={(e) => update({ colors: { [key]: e.target.value } })} />
              </span>
            </Row>
          );
        })}
        <Row title={t("theme.wash")} desc={t("theme.wash.sub")} data-setting="appearance.wash">
          <Switch checked={prefs.wash} onChange={(wash) => update({ wash })} label={t("theme.wash")} />
        </Row>
        <SetFoot>
          <Button size="sm" onClick={() => replace(resetColors())}>{t("theme.reset")}</Button>
        </SetFoot>
      </SetGroup>

      <SetGroup title={t("theme.more")}>
        <Row title={t("theme.contrast")} desc={t("theme.contrast.sub")} data-setting="appearance.contrast"><Segmented value={prefs.contrast} onChange={(contrast) => update({ contrast })} options={([{ id: "normal" as const, label: t("theme.contrast.normal") }, { id: "high" as const, label: t("theme.contrast.high") }])} /></Row>
        <Row title={t("theme.code")} desc={t("theme.code.sub")} data-setting="appearance.code"><Segmented value={prefs.code} onChange={(code) => update({ code })} options={([{ id: "theme" as const, label: t("theme.code.theme") }, { id: "dark" as const, label: t("theme.code.dark") }])} /></Row>
      </SetGroup>

      <SetGroup title={t("settings.group.companion")}>
        <Row title={t("pet.title")} desc={t("pet.hint")} data-setting="appearance.pet"><Switch checked={pet} onChange={setPet} label={t("pet.title")} /></Row>
        {pet && <Row title={t("pet.model")} desc={t("pet.model.hint")} data-setting="appearance.pet_model">
          <Select label={t("pet.model")} value={petModel} onChange={setPetModel} options={[{ id: "", label: t("pet.model.off") }, ...Object.entries(petSettings.data?.presets ?? {}).map(([id, preset]) => ({ id, label: preset.label || `${preset.provider}/${preset.model}` }))]} />
        </Row>}
      </SetGroup>
    </>
  );
}
