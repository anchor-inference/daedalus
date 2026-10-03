import { useEffect, useRef } from "react";

export type PetPose = { emotion: string; action: string; prop: string };

type Stage = {
  setEmotion: (id: string) => void;
  setAction: (id: string) => void;
  setProp: (id: string) => void;
  setFraming: (mode: string) => void;
  talk: (seconds: number) => void;
  dispose: () => void;
};

export function PetStage({ pose, speaking, large = false }: { pose: PetPose; speaking: number; large?: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<Stage | null>(null);
  useEffect(() => {
    let cancelled = false;
    // The renderer and Three.js are downloaded only after the operator enables the companion.
    // @ts-expect-error The shared studio scene is plain JavaScript.
    import("../../mascot-studio/stage.js").then(({ MascotStage }) => {
      if (cancelled || !canvas.current) return;
      try {
        const next = new MascotStage(canvas.current, { compact: !large }) as Stage;
        stage.current = next;
        next.setFraming("full");
        next.setEmotion(pose.emotion);
        next.setAction(pose.action);
        next.setProp(pose.prop);
      } catch { /* WebGL may be unavailable; the text companion still works. */ }
    }).catch(() => { /* A text companion remains usable offline. */ });
    return () => { cancelled = true; stage.current?.dispose(); stage.current = null; };
  }, [large]);
  useEffect(() => {
    stage.current?.setEmotion(pose.emotion);
    stage.current?.setAction(pose.action);
    stage.current?.setProp(pose.prop);
  }, [pose]);
  useEffect(() => { if (speaking) stage.current?.talk(2.5); }, [speaking]);
  return <canvas ref={canvas} className="pet-canvas" aria-hidden />;
}
