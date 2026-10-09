/** Nalanda: the tick lake and the hash-chained settlement warehouse (backend `app/api/v1/nalanda.py`, Group 67). */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export const RECORD_KINDS = ["LEDGER_POSTING", "SETTLEMENT_RECEIPT", "AUDIT_EVENT", "MARKET_RESULT", "BOOKMAKER_RESPONSE"] as const;
export type RecordKind = (typeof RECORD_KINDS)[number];
export const MAINTENANCE_TASKS = ["preallocate", "vacuum", "compress", "mirror", "anchor", "verify"] as const;
export type MaintenanceTask = (typeof MAINTENANCE_TASKS)[number];

export interface Partition {
  partition: string;
  table: string;
  bound: string;
  rows_estimate: number;
  bytes: number;
  dead_tuples: number;
  last_vacuum: string | null;
  last_analyze: string | null;
  week_start: string | null;
  is_default: boolean;
}

export interface Telemetry {
  generated_at: string;
  storage: {
    database_bytes: number | null;
    tables: Record<string, { partitions: number; bytes: number; rows_estimate: number }>;
    brin: Record<string, { index_bytes: number; heap_bytes: number; heap_per_index_byte: number | null; index_pct_of_heap: number | null }>;
  };
  partitions: Partition[];
  dead_tuples: number;
  default_partition_rows: number;
  ticks_24h: number;
  anomalies_24h: number;
  firehose: {
    available: boolean;
    stream_length?: number;
    pending?: number | null;
    lag?: number | null;
    consumers?: number | null;
    leader?: boolean;
    totals?: Record<string, number | string>;
    rows_per_second_1m?: number;
    series?: { t: string; rows: number }[];
    last_verification?: VerifyReport | null;
  };
  chain: { head_seq: number; head_hash: string | null; head_at: string | null; records: Record<string, number>; anchors: number };
  mirror: { kind: string; watermark: string | null; mirrored: number; updated_at: string | null }[];
  cold: { parquet: { root: string; files: number; bytes: number; by_table: Record<string, { files: number; bytes: number }> }; exports: Record<string, { files: number; rows: number; bytes: number }> };
  rollups: { days: number; last_day: string | null; candles: number };
  maintenance: Record<string, { status: string; started_at: string | null; finished_at: string | null; detail: Record<string, unknown> }>;
  settings: { weeks_ahead: number; rollup_after_days: number; cold_after_days: number; candle_retention_days: number; read_work_mem: string; s3_mirror: boolean };
}

export interface VerifyReport {
  chain: string;
  ok: boolean;
  rows: number;
  first_seq: number | null;
  last_seq: number | null;
  last_hash: string;
  head_seq: number | null;
  head_hash: string | null;
  anchors_checked: number;
  failures: { seq: number | null; problem: string; detail: string }[];
  failures_total: number;
  elapsed_ms: number;
  verified_at: string;
  cached?: boolean;
  age_seconds?: number;
}

export interface TickRow {
  created_at: string;
  observed_at: string;
  fixture_id: string;
  market: string;
  selection: string;
  source: string;
  bookmaker_id: string;
  odds: string;
  is_suspended: boolean;
  is_anomaly: boolean;
  anomaly_z: number | null;
  stream_id: string;
}

export interface ArchiveRecordView {
  seq: number;
  created_at: string;
  record_kind: RecordKind;
  source: string;
  source_id: string;
  user_id: string | null;
  bot_id: string | null;
  ledger_id: string | null;
  fixture_id: string | null;
  amount_inr: string | null;
  occurred_at: string | null;
  prev_hash: string;
  row_hash: string;
  payload: Record<string, unknown>;
}

export interface RebuildReport {
  postings: number;
  as_of_seq: number | null;
  ok: boolean;
  rebuilt_at: string;
  accounts: {
    user_id: string;
    bot_id: string | null;
    status: "MATCH" | "DRIFT" | "NOT_ARCHIVED" | "MISSING_LIVE";
    derived_available: string;
    derived_exposure: string;
    live_available: string | null;
    live_exposure: string | null;
    drift_available?: string;
    drift_exposure?: string;
  }[];
  unbalanced_journals: { journal_id: string; sum: string }[];
  mirrored_first: Record<string, number>;
}

export interface ColdExportRow {
  id: string;
  table: string;
  partition: string | null;
  from: string;
  to: string;
  file: string;
  rows: number;
  bytes: number;
  sha256: string;
  status: string;
  mirror_target: string | null;
  mirrored_at: string | null;
  dropped_at: string | null;
  created_at: string;
}

export const useNalandaTelemetry = () => useResource("nalanda:telemetry", () => apiClient.get<Telemetry>("/nalanda/telemetry"), { intervalMs: 5_000 });
export const useColdExports = (enabled: boolean) => useResource(enabled ? "nalanda:exports" : null, () => apiClient.get<ColdExportRow[]>("/nalanda/exports", { limit: 50 }));

export const formatBytes = (bytes: number | null | undefined): string => {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
};
