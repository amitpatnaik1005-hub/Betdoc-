/**
 * Single source of truth for the 17 BetDoc commanders.
 * IDs mirror the PostgreSQL enum `hive_legendary_bot` exactly.
 */

export const COMMANDER_IDS = [
  "KAUTILYA",
  "ASHOKA",
  "KUMBHA",
  "PANINI",
  "GARUDA",
  "DEVRAYA",
  "BAJIRAO",
  "ARYABHATA",
  "VIDUR",
  "TODAR MAL",
  "PRATAP",
  "SHIVAJI",
  "PRITHVIRAJ",
  "BIRBAL",
  "PORUS",
  "VARAHMIHIR",
  "KAPILENDRADEVA",
] as const;

export type CommanderId = (typeof COMMANDER_IDS)[number];
export type CommanderProtocol = "REST" | "WEBSOCKET" | "HYBRID";
export type CommanderStatus = "ACTIVE" | "STANDBY" | "ENGAGED";
export type CommanderSceneKey = "command" | "lab" | "factory" | "phantom";

export interface CommanderTheme {
  primary: string; // #RRGGBB
  glow: string; // any CSS colour (rgba recommended)
  accent: string; // #RRGGBB
}

export interface CommanderProfile {
  id: CommanderId;
  name: string;
  domain: string;
  routePrefixes: readonly string[];
  networkProtocol: CommanderProtocol;
  theme: CommanderTheme;
  clearanceLevel: number;
  status: CommanderStatus;
  scene: CommanderSceneKey;
}

export const DEFAULT_COMMANDER_ID: CommanderId = "KAUTILYA";

export const COMMANDER_REGISTRY: Readonly<Record<CommanderId, CommanderProfile>> = {
  KAUTILYA: {
    id: "KAUTILYA",
    name: "Kautilya",
    domain: "Supreme Command / Dashboard",
    routePrefixes: ["/dashboard", "/command"],
    networkProtocol: "HYBRID",
    theme: { primary: "#F59E0B", glow: "rgba(245, 158, 11, 0.55)", accent: "#FDE68A" },
    clearanceLevel: 10,
    status: "ACTIVE",
    scene: "command",
  },
  ASHOKA: {
    id: "ASHOKA",
    name: "Ashoka",
    domain: "The Oracle",
    routePrefixes: ["/oracle", "/predictions"],
    networkProtocol: "REST",
    theme: { primary: "#3B82F6", glow: "rgba(59, 130, 246, 0.55)", accent: "#BFDBFE" },
    clearanceLevel: 8,
    status: "STANDBY",
    scene: "command",
  },
  KUMBHA: {
    id: "KUMBHA",
    name: "Kumbha",
    domain: "The Risk Fortress",
    routePrefixes: ["/risk", "/vault"],
    networkProtocol: "REST",
    theme: { primary: "#EF4444", glow: "rgba(239, 68, 68, 0.55)", accent: "#FECACA" },
    clearanceLevel: 9,
    status: "STANDBY",
    scene: "command",
  },
  PANINI: {
    id: "PANINI",
    name: "Panini",
    domain: "The Lab / Math Models",
    routePrefixes: ["/lab", "/models"],
    networkProtocol: "REST",
    theme: { primary: "#8B5CF6", glow: "rgba(139, 92, 246, 0.55)", accent: "#DDD6FE" },
    clearanceLevel: 7,
    status: "STANDBY",
    scene: "lab",
  },
  GARUDA: {
    id: "GARUDA",
    name: "Garuda",
    domain: "Phantom Execution",
    routePrefixes: ["/phantom", "/execution"],
    networkProtocol: "HYBRID",
    theme: { primary: "#06B6D4", glow: "rgba(6, 182, 212, 0.55)", accent: "#A5F3FC" },
    clearanceLevel: 9,
    status: "STANDBY",
    scene: "phantom",
  },
  DEVRAYA: {
    id: "DEVRAYA",
    name: "Devraya",
    domain: "HFT Worker",
    routePrefixes: ["/hft"],
    networkProtocol: "WEBSOCKET",
    theme: { primary: "#22C55E", glow: "rgba(34, 197, 94, 0.55)", accent: "#BBF7D0" },
    clearanceLevel: 8,
    status: "STANDBY",
    scene: "command",
  },
  BAJIRAO: {
    id: "BAJIRAO",
    name: "Bajirao",
    domain: "The Arena",
    routePrefixes: ["/arena", "/live"],
    networkProtocol: "WEBSOCKET",
    theme: { primary: "#F97316", glow: "rgba(249, 115, 22, 0.55)", accent: "#FED7AA" },
    clearanceLevel: 7,
    status: "STANDBY",
    scene: "command",
  },
  ARYABHATA: {
    id: "ARYABHATA",
    name: "Aryabhata",
    domain: "Statistical Analytics",
    routePrefixes: ["/analytics", "/stats"],
    networkProtocol: "REST",
    theme: { primary: "#0EA5E9", glow: "rgba(14, 165, 233, 0.55)", accent: "#BAE6FD" },
    clearanceLevel: 6,
    status: "STANDBY",
    scene: "command",
  },
  VIDUR: {
    id: "VIDUR",
    name: "Vidur",
    domain: "The Hive / Wire",
    routePrefixes: ["/hive", "/wire"],
    networkProtocol: "HYBRID",
    theme: { primary: "#EAB308", glow: "rgba(234, 179, 8, 0.55)", accent: "#FEF08A" },
    clearanceLevel: 7,
    status: "STANDBY",
    scene: "command",
  },
  "TODAR MAL": {
    id: "TODAR MAL",
    name: "Todar Mal",
    domain: "Finance / Archive",
    routePrefixes: ["/finance", "/archive"],
    networkProtocol: "REST",
    theme: { primary: "#10B981", glow: "rgba(16, 185, 129, 0.55)", accent: "#A7F3D0" },
    clearanceLevel: 8,
    status: "STANDBY",
    scene: "command",
  },
  PRATAP: {
    id: "PRATAP",
    name: "Pratap",
    domain: "Core Infrastructure",
    routePrefixes: ["/infrastructure", "/system"],
    networkProtocol: "REST",
    theme: { primary: "#94A3B8", glow: "rgba(148, 163, 184, 0.55)", accent: "#E2E8F0" },
    clearanceLevel: 9,
    status: "STANDBY",
    scene: "factory",
  },
  SHIVAJI: {
    id: "SHIVAJI",
    name: "Shivaji",
    domain: "Guerrilla Markets",
    routePrefixes: ["/guerrilla"],
    networkProtocol: "WEBSOCKET",
    theme: { primary: "#D946EF", glow: "rgba(217, 70, 239, 0.55)", accent: "#F5D0FE" },
    clearanceLevel: 6,
    status: "STANDBY",
    scene: "command",
  },
  PRITHVIRAJ: {
    id: "PRITHVIRAJ",
    name: "Prithviraj",
    domain: "Head-to-Head Matchups",
    routePrefixes: ["/matchups", "/h2h"],
    networkProtocol: "REST",
    theme: { primary: "#F43F5E", glow: "rgba(244, 63, 94, 0.55)", accent: "#FECDD3" },
    clearanceLevel: 5,
    status: "STANDBY",
    scene: "command",
  },
  BIRBAL: {
    id: "BIRBAL",
    name: "Birbal",
    domain: "Logic Engine / Fallback",
    routePrefixes: ["/logic"],
    networkProtocol: "REST",
    theme: { primary: "#84CC16", glow: "rgba(132, 204, 22, 0.55)", accent: "#D9F99D" },
    clearanceLevel: 5,
    status: "STANDBY",
    scene: "command",
  },
  PORUS: {
    id: "PORUS",
    name: "Porus",
    domain: "Defensive Hedging",
    routePrefixes: ["/hedging"],
    networkProtocol: "REST",
    theme: { primary: "#14B8A6", glow: "rgba(20, 184, 166, 0.55)", accent: "#99F6E4" },
    clearanceLevel: 6,
    status: "STANDBY",
    scene: "command",
  },
  VARAHMIHIR: {
    id: "VARAHMIHIR",
    name: "Varahmihir",
    domain: "Temporal Forecasting",
    routePrefixes: ["/forecast", "/temporal"],
    networkProtocol: "HYBRID",
    theme: { primary: "#6366F1", glow: "rgba(99, 102, 241, 0.55)", accent: "#C7D2FE" },
    clearanceLevel: 7,
    status: "STANDBY",
    scene: "command",
  },
  KAPILENDRADEVA: {
    id: "KAPILENDRADEVA",
    name: "Kapilendradeva",
    domain: "Expansion Markets",
    routePrefixes: ["/expansion"],
    networkProtocol: "REST",
    theme: { primary: "#FB923C", glow: "rgba(251, 146, 60, 0.55)", accent: "#FFEDD5" },
    clearanceLevel: 5,
    status: "STANDBY",
    scene: "command",
  },
};

export const COMMANDER_LIST: readonly CommanderProfile[] = COMMANDER_IDS.map((id) => COMMANDER_REGISTRY[id]);

export const primaryRoute = (commander: CommanderProfile): string => commander.routePrefixes[0] ?? "/";

const normalizePath = (pathname: string): string => {
  const path = pathname.split(/[?#]/)[0] ?? "/";
  const trimmed = path.replace(/\/+$/, "").toLowerCase();
  return trimmed === "" ? "/" : trimmed;
};

/** Longest-prefix match on whole path segments (`/risk` matches `/risk/x`, never `/risky`). */
const PREFIX_TABLE: ReadonlyArray<{ prefix: string; commander: CommanderProfile }> = COMMANDER_LIST.flatMap(
  (commander) => commander.routePrefixes.map((prefix) => ({ prefix: normalizePath(prefix), commander })),
).sort((a, b) => b.prefix.length - a.prefix.length);

export function resolveCommander(pathname: string): CommanderProfile {
  const path = normalizePath(pathname);
  const hit = PREFIX_TABLE.find(({ prefix }) => path === prefix || path.startsWith(`${prefix}/`));
  return hit?.commander ?? COMMANDER_REGISTRY[DEFAULT_COMMANDER_ID];
}

/** Compare against `SELECT unnest(enum_range(NULL::hive_legendary_bot))` to detect drift. */
export function diffAgainstDatabaseEnum(databaseIds: readonly string[]): { missing: string[]; unexpected: string[] } {
  const known = new Set<string>(COMMANDER_IDS);
  const db = new Set(databaseIds);
  return {
    missing: databaseIds.filter((id) => !known.has(id)),
    unexpected: COMMANDER_IDS.filter((id) => !db.has(id)),
  };
}

const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function validateRegistry(): void {
  const seen = new Map<string, CommanderId>();
  for (const commander of COMMANDER_LIST) {
    if (commander.routePrefixes.length === 0) throw new Error(`${commander.id}: routePrefixes must not be empty.`);
    if (!HEX_COLOR.test(commander.theme.primary) || !HEX_COLOR.test(commander.theme.accent)) {
      throw new Error(`${commander.id}: theme.primary and theme.accent must be #RRGGBB.`);
    }
    for (const prefix of commander.routePrefixes) {
      if (!prefix.startsWith("/") || prefix === "/") throw new Error(`${commander.id}: invalid route prefix "${prefix}".`);
      const owner = seen.get(normalizePath(prefix));
      if (owner) throw new Error(`Route prefix "${prefix}" is claimed by both ${owner} and ${commander.id}.`);
      seen.set(normalizePath(prefix), commander.id);
    }
  }
}

if (import.meta.env.DEV) validateRegistry();
