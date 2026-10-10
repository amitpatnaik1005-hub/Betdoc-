/**
 * The audio wire's switch (the hook: `lib/the_wire_audio.ts`). Developed for Amit Ashok Kumar Patnaik.
 */
import { Button } from "../../ui/kit";

export const TacticalAudioAlert = ({ armed, toggle, supported }: { armed: boolean; toggle: () => void; supported: boolean }) => (
  <Button icon={armed ? "volume_up" : "volume_off"} onClick={toggle} disabled={!supported} variant={armed ? "primary" : "secondary"}>
    {supported ? (armed ? "Audio wire armed" : "Audio muted") : "No speech in this browser"}
  </Button>
);
