/**
 * The Infinity / Flexi view toggle (Group 107, point b.8): the workspace edge to edge, the sidebar and the dock tucked
 * away; the header's "Exit Infinity" pill (or this button) puts them back exactly as they were. Developed for Amit Ashok
 * Kumar Patnaik.
 */
import { useUIStore } from "../../store/useUIStore";
import { Button } from "../../ui/kit";

export const InfinityFlexiViewToggle = () => {
  const on = useUIStore((s) => s.isInfinityView);
  const toggle = useUIStore((s) => s.toggleInfinityView);
  return (
    <Button icon={on ? "fullscreen_exit" : "aspect_ratio"} variant={on ? "primary" : "secondary"} onClick={toggle} aria-pressed={on}
            title={on ? "Leave the Infinity view: the sidebar and dock come back as they were" : "Infinity view: the workspace edge to edge"}>
      {on ? "Leave Infinity view" : "Infinity view"}
    </Button>
  );
};
