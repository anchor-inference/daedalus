import { useEffect, useState } from "react";
import { type SessionList } from "../api";
import { useQuery } from "../store";
import { readPrefs } from "../appearance";

const KEY = "daedalus.pet";
const EVENT = "daedalus:pet-preference";
function read(): boolean {
  try { return localStorage.getItem(KEY) === "on"; } catch { return false; }
}
export function usePetPreference(): [boolean, (enabled: boolean) => void] {
  const [enabled, setEnabled] = useState(read);
  useEffect(() => {
    const update = () => setEnabled(read());
    window.addEventListener(EVENT, update);
    return () => window.removeEventListener(EVENT, update);
  }, []);
  const choose = (next: boolean) => {
    try { localStorage.setItem(KEY, next ? "on" : "off"); } catch { /* Lasts for this window in private mode. */ }
    setEnabled(next);
    window.dispatchEvent(new Event(EVENT));
  };
  return [enabled, choose];
}

/** Only the native companion receives a status; its renderer never has auth or conversation text. */
export function useDesktopPet(authed: boolean, needsReply: boolean) {
  const [enabled, choose] = usePetPreference();
  const active = authed && enabled && !!window.daedalus?.pet;
  const working = useQuery<SessionList>(active ? "/api/sessions?view=working" : null, { pollMs:5000, staleMs:3000 });
  const attention = useQuery<SessionList>(active ? "/api/sessions?view=attention" : null, { pollMs:5000, staleMs:3000 });
  useEffect(() => {
    void window.daedalus?.pet?.(active);
  }, [active]);
  useEffect(() => window.daedalus?.onPetHidden?.(() => choose(false)), []);
  const state = needsReply ? "waiting" : attention.data?.sessions.some((s) => s.status === "failed") ? "failed" : working.data?.sessions.length ? "running" : "idle";
  useEffect(() => {
    if (!active) return;
    const send = () => window.daedalus?.petState?.({ state, reduced:readPrefs().motion === "reduce" || window.matchMedia("(prefers-reduced-motion: reduce)").matches });
    send();
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    media.addEventListener("change", send);
    const observer = new MutationObserver(send);
    observer.observe(document.documentElement, { attributes:true, attributeFilter:["data-motion"] });
    return () => { media.removeEventListener("change", send); observer.disconnect(); };
  }, [active, state]);
}
