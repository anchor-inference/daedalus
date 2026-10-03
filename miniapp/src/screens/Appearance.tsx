import { usePetModel, usePetPreference } from "../ui/pet";
// Appearance is this browser's look: the named themes, then the few adjustments that are not a new theme.

import { useState } from "react";
import { FONT_CATALOG, FONT_ROLES, FontRole, THEMES, ThemeId, fontUrlAllowed, readPrefs, resolvedTheme, resetColors, updatePrefs, type ColorKey, type Column, type Leading, type Prefs, type ProseStep, type Radius, type Scale } from "../appearance";
import { t } from "../i18n";
import { Dropdown, Segmented, Switch } from "../ui/index";
import { Row } from "../settingsrow";
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

export function AppearancePanel() {
  const [prefs, update, replace] = usePrefs();
  const [pet, setPet] = usePetPreference();
  const [petModel, setPetModel] = usePetModel();
  const petSettings = useQuery<Settings>(pet ? "/api/settings" : null);
  const [role, setRole] = useState<FontRole>("prose");
  const [query, setQuery] = useState("");
  const [url, setUrl] = useState("");
  const [urlBad, setUrlBad] = useState(false);
  const env = { scheme: null as null, prefersDark: window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? true };
  let scheme: "dark" | "light" | null = null;
  try {
    const stored = localStorage.getItem("daedalus.scheme");
    scheme = stored === "dark" || stored === "light" ? stored : null;
  } catch {
    /* private mode */
  }
  const resolved = resolvedTheme(prefs, { scheme, prefersDark: env.prefersDark });
  const needle = query.trim().toLowerCase();
  const faces = FONT_CATALOG.filter((face) => !needle || face.family.toLowerCase().includes(needle));
  const known = FONT_CATALOG.some((face) => face.family.toLowerCase() === needle);

  const setFace = (family: string, address = "") => {
    if (role === "ui") update({ fontUi: family, fontUiUrl: address });
    else if (role === "prose") update({ fontProse: family, fontProseUrl: address });
    else update({ fontCode: family, fontCodeUrl: address });
  };

  const color = (key: ColorKey, fallback: string) => prefs.colors[key] ?? fallback;

  return (
    <>
      <p className="sub">{t("theme.lead")}</p>
      <div className="card">
        <Row title={t("pet.title")} desc={t("pet.hint")}><Switch checked={pet} onChange={setPet} label={t("pet.title")} /></Row>
        {pet && <Row title={t("pet.model")} desc={t("pet.model.hint")}>
          <select className="field" value={petModel} aria-label={t("pet.model")} onChange={(event) => setPetModel(event.target.value)}>
            <option value="">{t("pet.model.off")}</option>
            {Object.entries(petSettings.data?.presets ?? {}).map(([id, preset]) => <option key={id} value={id}>{preset.label || `${preset.provider}/${preset.model}`}</option>)}
          </select>
        </Row>}
      </div>
      <div className="settings-themes" role="listbox" aria-label={t("settings.sec.appearance")}>
        {THEMES.map((item) => (
          <button key={item.id} type="button" className={resolved.id === item.id ? "on" : ""} aria-pressed={resolved.id === item.id} onClick={() => update({ follow: false, theme: item.id })}>
            <span className="settings-swatch" style={{ background: item.wash }} />
            <span>{t(`theme.${item.id}`)}</span>
          </button>
        ))}
      </div>
      <div className="card">
        <Row title={t("theme.follow")} desc={t("theme.follow.sub", { dark: t(`theme.${prefs.dark}`), light: t(`theme.${prefs.light}`) })}>
          <Switch checked={prefs.follow} onChange={(follow) => update({ follow })} label={t("theme.follow")} />
        </Row>
        {prefs.follow && (
          <>
            <Row title={t("theme.follow.dark")}>
              <Dropdown label={t("theme.follow.dark")} value={prefs.dark} onChange={(dark: ThemeId) => update({ dark })} options={THEMES.filter((item) => !item.light).map((item) => ({ id: item.id, label: t(`theme.${item.id}`) }))} />
            </Row>
            <Row title={t("theme.follow.light")}>
              <Dropdown label={t("theme.follow.light")} value={prefs.light} onChange={(light: ThemeId) => update({ light })} options={THEMES.filter((item) => item.light).map((item) => ({ id: item.id, label: t(`theme.${item.id}`) }))} />
            </Row>
          </>
        )}
      </div>

      <div className="section-title">{t("theme.colors")}</div>
      <p className="sub">{t("theme.colors.lead")}</p>
      <div className="card">
        {(["bg", "surface", "fg", "fg2", "accent", "send"] as const).map((key) => {
          const fallback = key === "bg" ? resolved.side : key === "surface" ? resolved.surface : key === "fg" ? resolved.fg : key === "fg2" ? resolved.fg2 : key === "accent" ? resolved.accent : resolved.send.startsWith("#") ? resolved.send : resolved.accent;
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
        <Row title={t("theme.wash")} desc={t("theme.wash.sub")}>
          <Switch checked={prefs.wash} onChange={(wash) => update({ wash })} label={t("theme.wash")} />
        </Row>
        <div className="btnrow">
          <button type="button" className="btn small" onClick={() => replace(resetColors())}>{t("theme.reset")}</button>
        </div>
      </div>

      <div className="section-title">{t("theme.font")}</div>
      <p className="sub">{t("theme.font.lead")}</p>
      <div className="card settings-font">
        <Segmented value={role} onChange={setRole} options={FONT_ROLES.map((id) => ({ id, label: t(`theme.font.${id}`) }))} />
        <div className="settings-font-preview">
          <h2 className="settings-specimen" style={{ fontFamily: familyOf(prefs, role) ? `"${familyOf(prefs, role)}"` : undefined }}>{t("theme.font.sample")}</h2>
          <div className="sub">{familyOf(prefs, role) || t("theme.font.system")}{urlOf(prefs, role) ? ` · ${urlOf(prefs, role)}` : ""}</div>
        </div>
        <div className="settings-font-catalogue">
          <input className="field" value={query} placeholder={t("theme.font.search")} aria-label={t("theme.font.search")} onChange={(e) => setQuery(e.target.value)} />
          <div className="settings-faces">
            <button type="button" className={!familyOf(prefs, role) ? "on" : ""} aria-pressed={!familyOf(prefs, role)} onClick={() => setFace("")}>{t("theme.font.system")}</button>
            {faces.map((face) => (
              <button key={face.family} type="button" className={familyOf(prefs, role) === face.family ? "on" : ""} aria-pressed={familyOf(prefs, role) === face.family} style={{ fontFamily: face.local ? `"${face.family}"` : undefined }} onClick={() => setFace(face.family)}>
                {face.family}
                {face.local && <span className="sub"> · {t("theme.font.builtin")}</span>}
              </button>
            ))}
            {needle && !known && (
              <button type="button" onClick={() => setFace(query.trim())}>{t("theme.font.load", { name: query.trim() })}</button>
            )}
          </div>
        </div>
        <form className="settings-font-url" onSubmit={(e) => {
          e.preventDefault();
          const address = url.trim();
          if (!fontUrlAllowed(address) || !address) { setUrlBad(true); return; }
          setUrlBad(false);
          let family = familyOf(prefs, role);
          try {
            const parsed = new URL(address).searchParams.get("family");
            if (parsed) family = decodeURIComponent(parsed.split(":")[0].replace(/\+/g, " "));
          } catch { /* the allow check already rejected a bad address */ }
          if (/\.woff2$/i.test(address)) family = role === "ui" ? "Daedalus UI" : role === "prose" ? "Daedalus Prose" : "Daedalus Code";
          setFace(family, address);
        }}>
          <input className="field" value={url} placeholder={t("theme.font.url")} aria-label={t("theme.font.url")} onChange={(e) => { setUrl(e.target.value); setUrlBad(false); }} />
          <button type="submit" className="btn small">{t("theme.font.use")}</button>
        </form>
        {urlBad && <div className="sub attn">{t("theme.font.url.bad")}</div>}
      </div>

      <div className="section-title">{t("theme.size")}</div>
      <p className="sub">{t("theme.size.lead")}</p>
      <div className="card">
        <Row title={t("theme.size.step")}><Segmented value={prefs.scale} onChange={(scale: Scale) => update({ scale })} options={(["sm", "md", "lg", "xl"] as const).map((id) => ({ id, label: t(`theme.scale.${id}`) }))} /></Row>
        <Row title={t("theme.prose")} desc={t("theme.prose.sub")}><Segmented value={prefs.prose} onChange={(prose: ProseStep) => update({ prose })} options={([{ id: "auto" as const, label: t("theme.prose.auto") }, { id: "17" as const, label: "17" }, { id: "19" as const, label: "19" }])} /></Row>
        <Row title={t("theme.leading")}><Segmented value={prefs.leading} onChange={(leading: Leading) => update({ leading })} options={(["tight", "normal", "open"] as const).map((id) => ({ id, label: t(`theme.leading.${id}`) }))} /></Row>
        <Row title={t("theme.column")} desc={t("theme.column.sub")}><Segmented value={prefs.column} onChange={(column: Column) => update({ column })} options={(["narrow", "normal", "wide"] as const).map((id) => ({ id, label: t(`theme.column.${id}`) }))} /></Row>
      </div>

      <div className="section-title">{t("theme.more")}</div>
      <div className="card">
        <Row title={t("theme.radius")}><Segmented value={prefs.radius} onChange={(radius: Radius) => update({ radius })} options={(["sharp", "normal", "round"] as const).map((id) => ({ id, label: t(`theme.radius.${id}`) }))} /></Row>
        <Row title={t("theme.motion")}><Segmented value={prefs.motion} onChange={(motion) => update({ motion })} options={([{ id: "full" as const, label: t("theme.motion.full") }, { id: "reduce" as const, label: t("theme.motion.reduce") }])} /></Row>
        <Row title={t("theme.contrast")} desc={t("theme.contrast.sub")}><Segmented value={prefs.contrast} onChange={(contrast) => update({ contrast })} options={([{ id: "normal" as const, label: t("theme.contrast.normal") }, { id: "high" as const, label: t("theme.contrast.high") }])} /></Row>
        <Row title={t("theme.code")} desc={t("theme.code.sub")}><Segmented value={prefs.code} onChange={(code) => update({ code })} options={([{ id: "theme" as const, label: t("theme.code.theme") }, { id: "dark" as const, label: t("theme.code.dark") }])} /></Row>
        <Row title={t("theme.bubble")}><Segmented value={prefs.bubble} onChange={(bubble) => update({ bubble })} options={([{ id: "card" as const, label: t("theme.bubble.card") }, { id: "plain" as const, label: t("theme.bubble.plain") }])} /></Row>
      </div>
    </>
  );
}
