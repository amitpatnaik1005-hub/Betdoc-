/**
 * The betslip's pick rule (Group 131): one pick per market of a fixture. Developed for Amit Ashok Kumar Patnaik.
 *
 * Ashoka's leg ids are `fixture|market|selection`. A second selection of the same fixture's market (Home after Away,
 * Over 2.5 after Under 2.5) replaces the first: the two cannot both win. Clicking a picked selection again takes it off
 * the slip (toggle); adding it again keeps it. Different markets of one fixture stay allowed: the inspection's
 * independence pillar weighs those.
 */
interface Pickable {
  leg_id: string;
}

/** The fixture and market a leg belongs to. */
export const marketOf = (legId: string): string => legId.split("|").slice(0, 2).join("|");

export interface PickResult<L extends Pickable = Pickable> {
  action: "added" | "removed" | "replaced" | "kept";
  replaced: L | null;
}

/** The slip after picking `leg`: added, replacing its market's other selection, kept, or (toggleOff) taken off. */
export const pick = <L extends Pickable>(legs: L[], leg: L, toggleOff: boolean): { legs: L[]; result: PickResult<L> } => {
  const at = legs.findIndex((l) => marketOf(l.leg_id) === marketOf(leg.leg_id));
  if (at < 0) return { legs: [...legs, leg], result: { action: "added", replaced: null } };
  if (legs[at].leg_id === leg.leg_id) {
    return toggleOff ? { legs: legs.filter((_, i) => i !== at), result: { action: "removed", replaced: null } } : { legs, result: { action: "kept", replaced: null } };
  }
  return { legs: legs.map((l, i) => (i === at ? leg : l)), result: { action: "replaced", replaced: legs[at] } };
};
