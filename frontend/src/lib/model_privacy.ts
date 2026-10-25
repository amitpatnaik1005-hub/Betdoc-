/**
 * Model privacy (Group 131): the engines' internal names stay off the screen. Developed for Amit Ashok Kumar Patnaik.
 *
 * The API keeps every model's probability by name (the settlement feedback grades each model against results, and the
 * slip scanner reads the same leg payload), so the names are taken out where a leg is shown, not where it is stored.
 */

/** A leg's rationale without the "models dixon-coles 52%, poisson 50%" clause (its fair probability is already in it). */
export const publicRationale = (rationale: string): string =>
  rationale
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part && !/^models\b/i.test(part))
    .join("; ");
