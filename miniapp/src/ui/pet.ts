import { useEffect, useState } from "react";

const KEY = "daedalus.pet";
const EVENT = "daedalus:pet-preference";
let temporaryEnabled = false;
function read(): boolean {
  try { return localStorage.getItem(KEY) === "on"; } catch { return temporaryEnabled; }
}
export function usePetPreference(): [boolean, (enabled: boolean) => void] {
  const [enabled, setEnabled] = useState(read);
  useEffect(() => {
    const update = () => setEnabled(read());
    window.addEventListener(EVENT, update);
    window.addEventListener("storage", update);
    return () => { window.removeEventListener(EVENT, update); window.removeEventListener("storage", update); };
  }, []);
  const choose = (next: boolean) => {
    temporaryEnabled = next;
    try { localStorage.setItem(KEY, next ? "on" : "off"); } catch { /* Lasts for this window in private mode. */ }
    setEnabled(next);
    window.dispatchEvent(new Event(EVENT));
  };
  return [enabled, choose];
}

const MODEL_KEY = "daedalus.pet.model";
let temporaryModel = "";
export function usePetModel(): [string, (model: string) => void] {
  const [model, setModel] = useState(() => {
    try { return localStorage.getItem(MODEL_KEY) || ""; } catch { return temporaryModel; }
  });
  useEffect(() => {
    const update = () => { try { setModel(localStorage.getItem(MODEL_KEY) || ""); } catch { setModel(temporaryModel); } };
    window.addEventListener(EVENT, update);
    window.addEventListener("storage", update);
    return () => { window.removeEventListener(EVENT, update); window.removeEventListener("storage", update); };
  }, []);
  const choose = (next: string) => {
    temporaryModel = next;
    try { localStorage.setItem(MODEL_KEY, next); } catch { /* private mode */ }
    setModel(next);
    window.dispatchEvent(new Event(EVENT));
  };
  return [model, choose];
}

export type VoiceMascot = "full" | "head" | "off";
const VOICE_KEY = "daedalus.voice.mascot";
export function useVoiceMascot(): [VoiceMascot, (value: VoiceMascot) => void] {
  const [value, setValue] = useState<VoiceMascot>(() => {
    try {
      const saved = localStorage.getItem(VOICE_KEY);
      return saved === "head" || saved === "off" ? saved : "full";
    } catch { return "full"; }
  });
  const choose = (next: VoiceMascot) => {
    setValue(next);
    try { localStorage.setItem(VOICE_KEY, next); } catch { /* private mode */ }
  };
  return [value, choose];
}
