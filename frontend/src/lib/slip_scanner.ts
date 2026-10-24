/**
 * The slip scanner (Group 126): a bookmaker's bet slip, as a screenshot, a camera photo or pasted text, read by OCR on
 * the BetDoc server, matched to the live board, vetted by the Consultant's own engines and re-calibrated. Backend
 * `app/api/v1/slip_scanner.py`. The image is never stored; nothing is placed, and a booking code is shown, never opened.
 * Money and odds arrive as decimal strings, probabilities as fractions, percentages as numbers; a figure BetDoc could not
 * measure is null. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import type { Inspection, Skin } from "./backtest_and_parlay";
import type { SlipLeg } from "../store/useParlaySlipStore";

export type ScanBook = "auto" | "parimatch" | "1xbet" | "stake";
export type SlipVerdict = "APPROVED" | "PARTIAL" | "DECLINED" | "UNRATED" | "UNREADABLE";
export type LegStatus = "MATCHED" | "NO_ODDS" | "FIXTURE_NOT_READ" | "NOT_ON_BOARD" | "MARKET_NOT_COVERED" | "MARKET_NOT_PRICED";
export type Severity = "CRITICAL" | "WARNING" | "MILD";

export interface OcrLine {
  text: string;
  confidence: number;
  parsed: boolean; // false: a word on the line was read too uncertainly to use
}

export interface ScannedLeg {
  number: number;
  status: LegStatus;
  note: string;
  fixture_read: string | null;
  market_read: string;
  selection_read: string;
  odds_read: string | null;
  confidence: number;
  leg_id: string | null;
  fixture: string | null;
  sport_key: string | null;
  kickoff: string | null;
  market: string | null;
  selection: string | null;
  label: string | null;
  fair_probability: number | null;
  consensus_books: number | null;
  ev_at_slip_odds: number | null;
  book_price_now: string | null;
  ev_at_book_price: number | null;
}

export interface SlipFault {
  source: "scanner" | "doctor";
  code: string;
  severity: Severity;
  legs: number[];
  text: string;
}

export interface ReCalibratedLeg {
  action: "RETAINED" | "SUBSTITUTED" | "DROPPED";
  leg_id: string | null;
  fixture: string;
  label: string;
  price: string | null;
  fair_probability: number | null;
  ev: number | null;
  replaces: string | null;
  rationale: string;
}

export interface ScanResult {
  developer_credit: string;
  scan_id: string;
  source: "image" | "text";
  bookmaker: string | null;
  skin: Skin;
  ocr_lines: OcrLine[];
  detected_booking_code: string | null;
  detected_kind: string | null;
  detected_stake_inr: string | null;
  printed_total_odds: string | null;
  printed_potential_return_inr: string | null;
  odds_check: "MATCH" | "MISMATCH" | null;
  legs_scanned: ScannedLeg[];
  coverage: { read: number; vetted: number };
  total_scanned_odds: string | null;
  stake_inr: string | null;
  estimated_payout_inr: string | null;
  joint_win_probability_pct: number | null;
  overall_ev_pct: number | null;
  vetting_score: number | null;
  tier: string | null;
  verdict: SlipVerdict;
  verdict_status: string | null;
  verdict_badge: string;
  verdict_explanation: string;
  bookmaker_margin_pct: number | null;
  rating: Inspection["rating"] | null;
  faults_detected: SlipFault[];
  is_recalibrated: boolean;
  recalibrated_legs: ReCalibratedLeg[];
  recalibration_rounds: { replaced: { leg_id: string; label: string; fixture: string; reason: string }; before: { score: number; tier: string }; after: { score: number; tier: string } }[];
  recalibrated_score: number | null;
  recalibrated_tier: string | null;
  recalibrated_total_odds: string | null;
  recalibrated_joint_win_prob_pct: number | null;
  recalibrated_ev_pct: number | null;
  recalibrated_projected_payout_inr: string | null;
  recommended_kelly_stake_inr: string | null;
  recalibration_summary: string | null;
  consultant_legs: SlipLeg[];
  recalibrated_consultant_legs: SlipLeg[];
}

export interface ScanInput {
  image?: Blob | null;
  text?: string | null;
  bookmaker: ScanBook;
  stakeInr?: number | null;
}

/** OCR, the board, the fortress and up to two cure rounds: longer than a page's usual answer. */
export const SCAN_TIMEOUT_MS = 120_000;
/** A wider image is scaled to this before upload; a tall one keeps its width (the server reads it in bands). */
export const UPLOAD_MAX_WIDTH = 2048;
const UPLOAD_KEEP_BYTES = 4_000_000;
const UPLOAD_MAX_PIXELS = 30_000_000;
const SENT_AS_IS = ["image/png", "image/jpeg", "image/webp"];

/**
 * The image as it is uploaded: a phone screenshot or a modest photo as it is (the server reads its EXIF orientation);
 * a wide or heavy photo, or a format the server does not read (an iPhone HEIC, where the browser can decode it), drawn
 * upright onto a canvas no wider than UPLOAD_MAX_WIDTH and sent as a JPEG. When the browser cannot decode it, the file
 * goes as it is and the server says what it is.
 */
export async function prepareUpload(file: Blob): Promise<Blob> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    return file;
  }
  try {
    const { width, height } = bitmap;
    if (width <= UPLOAD_MAX_WIDTH && file.size <= UPLOAD_KEEP_BYTES && SENT_AS_IS.includes(file.type)) return file;
    const scale = Math.min(1, UPLOAD_MAX_WIDTH / width, Math.sqrt(UPLOAD_MAX_PIXELS / (width * height)));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.fillStyle = "#FFFFFF"; // a transparent screenshot reads as dark-on-white, not dark-on-black
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    return blob ?? file;
  } finally {
    bitmap.close();
  }
}

/** Read, match, vet and re-calibrate a slip: an image goes as multipart, text as JSON. Nothing is stored. */
export async function scanSlip({ image, text, bookmaker, stakeInr }: ScanInput): Promise<ScanResult> {
  if (image) {
    const form = new FormData();
    const upload = await prepareUpload(image);
    const name = upload.type === "image/jpeg" ? "slip.jpg" : upload.type === "image/webp" ? "slip.webp" : "slip.png";
    form.append("file", upload, image instanceof File && upload === image ? image.name : name);
    form.append("bookmaker", bookmaker);
    if (stakeInr) form.append("stake_inr", String(stakeInr));
    return apiClient.post<ScanResult>("/slip-scanner/upload-and-rate", form, true, SCAN_TIMEOUT_MS);
  }
  return apiClient.post<ScanResult>("/slip-scanner/scan-and-rate", { slip_text: text ?? "", bookmaker, stake_inr: stakeInr || null }, false, SCAN_TIMEOUT_MS);
}

export const VERDICT_STYLE: Record<SlipVerdict, { tone: "good" | "warning" | "critical" | "neutral"; icon: string; label: string }> = {
  APPROVED: { tone: "good", icon: "verified", label: "Approved" },
  PARTIAL: { tone: "warning", icon: "rule", label: "Partly vetted" },
  DECLINED: { tone: "critical", icon: "gpp_bad", label: "Declined" },
  UNRATED: { tone: "neutral", icon: "help", label: "Not vetted" },
  UNREADABLE: { tone: "neutral", icon: "visibility_off", label: "Not read" },
};

export const STATUS_LABEL: Record<LegStatus, string> = {
  MATCHED: "Vetted",
  NO_ODDS: "Price not read",
  FIXTURE_NOT_READ: "Fixture not read",
  NOT_ON_BOARD: "Not on the board",
  MARKET_NOT_COVERED: "Market not priced by Ashoka",
  MARKET_NOT_PRICED: "Not on the board now",
};

export const pct = (value: number | null | undefined, digits: number = 1): string =>
  value === null || value === undefined || !Number.isFinite(value) ? "—" : `${value.toFixed(digits)}%`;

/** A percentage the server sent as a number (-12.81), signed: "−12.81%", "+19.23%". */
export const signedPoints = (value: number | null | undefined, digits: number = 2): string =>
  value === null || value === undefined || !Number.isFinite(value) ? "—" : `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}%`;

export const signedPct = (fraction: number | null | undefined): string =>
  fraction === null || fraction === undefined || !Number.isFinite(fraction) ? "—" : `${fraction >= 0 ? "+" : "−"}${Math.abs(fraction * 100).toFixed(1)}%`;
