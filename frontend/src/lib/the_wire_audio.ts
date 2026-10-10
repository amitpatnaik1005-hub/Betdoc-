/**
 * Spoken alerts for The Wire: when armed, CRITICAL news and steam catalysts arriving on the live wire are read
 * aloud (the browser's speech synthesis; nothing leaves the machine). Off by default; remembered on this device.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useCallback, useEffect, useState } from "react";
import { type WireFrame } from "./the_wire";

const KEY = "betdoc:wire:audio";

const readPref = (): boolean => {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
};

export const speakable = (frame: WireFrame): string | null => {
  if (frame.type === "news" && frame.tactical_impact === "CRITICAL") return `Vidur critical: ${frame.title}`;
  if (frame.type === "catalyst") return `Vidur catalyst: ${frame.headline}. ${frame.selection} moved ${frame.shift_pct.toFixed(1)} points.`;
  return null;
};

export function useTacticalAudio(): { armed: boolean; toggle: () => void; announce: (frame: WireFrame) => void; supported: boolean } {
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  const [armed, setArmed] = useState(readPref);
  useEffect(() => {
    try {
      localStorage.setItem(KEY, armed ? "1" : "0");
    } catch {
      /* private mode: the toggle still works for this visit */
    }
    if (!armed && supported) window.speechSynthesis.cancel();
  }, [armed, supported]);
  const announce = useCallback((frame: WireFrame) => {
    const text = speakable(frame);
    if (!armed || !supported || !text) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    window.speechSynthesis.speak(utterance);
  }, [armed, supported]);
  return { armed, toggle: () => setArmed((a) => !a), announce, supported };
}

