import { useEffect, useRef } from "react";

export type PetPose = { emotion: string; action: string; prop: string };

type Stage = {
  setEmotion: (id: string) => void;
  setAction: (id: string) => void;
  setProp: (id: string) => void;
  setFraming: (mode: string) => void;
  setVoiceLevel: (level: number) => void;
  talk: (seconds: number) => void;
  dispose: () => void;
};

export function PetStage({ pose, speaking, large = false, variant = "daedalus", framing = "full", voiceLevel = 0 }: { pose: PetPose; speaking: number; large?: boolean; variant?: "daedalus" | "head"; framing?: "full" | "medium" | "portrait"; voiceLevel?: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<Stage | null>(null);
  useEffect(() => {
    let cancelled = false;
    // The renderer and Three.js are downloaded only after the operator enables the companion.
    // @ts-expect-error The shared studio scene is plain JavaScript.
    import("../../mascot-studio/stage.js").then(({ MascotStage }) => {
      if (cancelled || !canvas.current) return;
      try {
        const next = new MascotStage(canvas.current, { compact: !large, embedded: true, variant }) as Stage;
        stage.current = next;
        next.setFraming(framing);
        next.setEmotion(pose.emotion);
        next.setAction(pose.action);
        next.setProp(pose.prop);
      } catch { /* WebGL may be unavailable; the text companion still works. */ }
    }).catch(() => { /* A text companion remains usable offline. */ });
    return () => { cancelled = true; stage.current?.dispose(); stage.current = null; };
  }, [large, variant]);
  useEffect(() => {
    stage.current?.setEmotion(pose.emotion);
    stage.current?.setAction(pose.action);
    stage.current?.setProp(pose.prop);
  }, [pose]);
  useEffect(() => { if (speaking) stage.current?.talk(2.5); }, [speaking]);
  useEffect(() => { stage.current?.setFraming(framing); }, [framing]);
  useEffect(() => { stage.current?.setVoiceLevel(voiceLevel); }, [voiceLevel]);
  return <canvas ref={canvas} className="pet-canvas" aria-hidden />;
}
