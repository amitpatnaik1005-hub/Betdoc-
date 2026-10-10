/** The Vault: fleet credentials, accounts and fleet configuration (backend `app/api/v1/vault_admin.py`, Group 70). */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export interface VaultAccount {
  id: string;
  bookmaker_id: string;
  bookmaker_name: string;
  label: string;
  username_hint: string | null;
  has_password: boolean;
  has_api_key: boolean;
  has_token: boolean;
  has_2fa: boolean;
  has_notes: boolean;
  target_host: string | null;
  currency: string;
  adapter: string;
  is_active: boolean;
  priority: number;
  balance: string | null;
  stake_cap: string | null;
  reserved: string;
  free_funds: string | null;
  capacity: string | null;
  open_holds: number;
  verification_status: "UNVERIFIED" | "OK" | "FAILED" | "UNSUPPORTED";
  verification_detail: string | null;
  last_verified_at: string | null;
  last_used_at: string | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface VaultProvider {
  id: string;
  provider_id: string;
  provider_name: string;
  generic: boolean;
  label: string;
  key_hint: string | null;
  has_secret: boolean;
  base_url: string | null;
  linked_source_id: string | null;
  fleet_source: string | null;
  is_active: boolean;
  verification_status: string;
  verification_detail: string | null;
  last_verified_at: string | null;
  source: string;
  created_at: string | null;
}

export interface VaultSport {
  key: string;
  source: "environment" | "vault";
  polling: boolean;
  offered: boolean | null;
  markets: string;
  credits_per_call: number;
}

export interface VaultQuota {
  remaining: number | null;
  used: number | null;
  limit: number | null;
  fraction: number | null;
  floor: number;
  regions: string;
  poll_interval_seconds: number;
  credits_per_hour: number;
  hours_left: number | null;
}

export interface VaultStatus {
  generated_at: string;
  overlay_version: number;
  account_routing: boolean;
  quiet_hours: { start: string | null; end: string | null; timezone: string; active: boolean };
  sports: VaultSport[];
  odds_api_index_known: boolean;
  quota: VaultQuota;
  bookmakers: Record<string, { accounts: number; active: number }>;
  vault_configured: boolean;
  path_imports: boolean;
}

export interface PreviewAccount {
  line: number;
  bookmaker: string;
  bookmaker_name: string;
  label: string;
  username_hint: string | null;
  currency: string;
  has_password: boolean;
  has_api_key: boolean;
  has_token: boolean;
  has_2fa: boolean;
  target_host: string | null;
  adapter: string;
  url_only: boolean;
}

export interface PreviewProvider {
  line: number;
  provider: string;
  provider_name: string;
  label: string;
  key_hint: string;
  has_secret: boolean;
  fleet_source: string | null;
  generic: boolean;
}

export interface ImportChanges {
  accounts_created: number;
  accounts_updated: number;
  accounts_unchanged: number;
  providers_created: number;
  providers_updated: number;
  providers_unchanged: number;
  providers_linked: string[];
  sports_added: string[];
}

export interface ImportPreview {
  accounts_found: number;
  providers_found: number;
  sports_found: number;
  syntax_warnings: string[];
  accounts: PreviewAccount[];
  providers: PreviewProvider[];
  open_endpoints: { line: number; name: string; host: string }[];
  sports: string[];
  lines: number;
  changes?: ImportChanges;
  vault_configured: boolean;
}

export interface ImportResult {
  accounts_found: number;
  providers_found: number;
  sports_found: number;
  syntax_warnings: string[];
  report: ImportChanges & { warnings: string[]; run_id: string | null };
}

/** Exactly one source per call: a dropped file, pasted markdown, or a path under VAULT_IMPORT_ALLOWED_DIRS. */
export type ImportSource = { kind: "file"; file: File } | { kind: "text"; text: string } | { kind: "path"; path: string };

const form = (source: ImportSource): FormData => {
  const body = new FormData();
  if (source.kind === "file") body.append("file", source.file, source.file.name);
  else if (source.kind === "text") body.append("text", source.text);
  else body.append("path", source.path);
  return body;
};

export const previewImport = (source: ImportSource) => apiClient.post<ImportPreview>("/vault/import-preview", form(source), true);
export const saveImport = (source: ImportSource) => apiClient.post<ImportResult>("/vault/import-markdown", form(source), true);

export interface AccountPatch {
  label?: string;
  currency?: string;
  priority?: number;
  balance?: number | null;
  stake_cap?: number | null;
  is_active?: boolean;
  secrets?: Partial<Record<"username" | "password" | "api_key" | "token" | "totp_seed" | "notes" | "url", string>>;
}

export const updateAccount = (id: string, patch: AccountPatch) => apiClient.patch<VaultAccount>(`/vault/accounts/${id}`, patch);
export const toggleAccount = (id: string, isActive: boolean) => apiClient.patch<VaultAccount>(`/vault/accounts/${id}/toggle`, { is_active: isActive });
export const toggleProvider = (id: string, isActive: boolean) => apiClient.patch<VaultProvider>(`/vault/providers/${id}`, { is_active: isActive });

export const useVaultAccounts = () => useResource<VaultAccount[]>("vault:accounts", () => apiClient.get<VaultAccount[]>("/vault/accounts"), { intervalMs: 30_000 });
export const useVaultProviders = () => useResource<VaultProvider[]>("vault:providers", () => apiClient.get<VaultProvider[]>("/vault/providers"), { intervalMs: 60_000 });
export const useVaultStatus = () => useResource<VaultStatus>("vault:status", () => apiClient.get<VaultStatus>("/vault/status"), { intervalMs: 30_000 });

/** ``parimatch_***``: what a secret looks like on screen when the vault never sent a hint for it. */
export const maskedIdentity = (account: VaultAccount): string =>
  account.username_hint ?? (account.has_api_key || account.has_token ? `${account.bookmaker_id}_key_***` : account.target_host ? `${account.bookmaker_id}_url_only` : `${account.bookmaker_id}_***`);
