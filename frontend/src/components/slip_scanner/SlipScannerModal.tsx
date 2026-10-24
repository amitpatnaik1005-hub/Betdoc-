/**
 * The slip scanner (Group 126): bring a bookmaker's slip, as a screenshot, a camera photo or its text, and BetDoc reads
 * it on its own server (RapidOCR; the image is never stored), matches each leg to the live board, vets it with the
 * Consultant's engines (the 15-pillar fortress, the Slip Score, the Doctor) and re-calibrates it with quoted legs when
 * a cure scores higher. Every figure is the server's; a leg BetDoc cannot vet is said, never scored. Either slip opens
 * in the Consultant, which seals it. Nothing is placed, and a booking code is shown, never opened.
 *
 * Laptop and phone alike: drag and drop, the file picker, Ctrl+V / Cmd+V anywhere in the dialog, the clipboard button
 * (where the browser allows reading it: a secure origin), the rear camera on a phone, and a text tab for a pasted or
 * typed slip. Developed for Amit Ashok Kumar Patnaik.
 */
import { type ChangeEvent, type CSSProperties, type DragEvent, type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../../api/client";
import { BOOKMAKER_THEMES, type Tier, TIER_STYLE } from "../../lib/backtest_and_parlay";
import { inr } from "../../lib/slip_ornament";
import {
  pct,
  type ReCalibratedLeg,
  type ScanBook,
  type ScannedLeg,
  type ScanResult,
  scanSlip,
  signedPct,
  signedPoints,
  type SlipFault,
  STATUS_LABEL,
  VERDICT_STYLE,
} from "../../lib/slip_scanner";
import { type SlipLeg, useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Button, Pill } from "../../ui/kit";
import { SlipScoreMeter } from "../builder/SlipScoreMeter";

type Mode = "image" | "text";

const BOOKS: { value: ScanBook; label: string }[] = [
  { value: "auto", label: "The book the slip names" },
  { value: "parimatch", label: "Parimatch" },
  { value: "1xbet", label: "1xBet" },
  { value: "stake", label: "Stake" },
];
const TAP = "min-h-11 sm:min-h-0"; // a thumb-sized target on a phone, compact on a laptop
const IMAGE_NAME = /\.(png|jpe?g|webp|bmp|gif|heic|heif)$/i;
const TEXT_PLACEHOLDER = "Arsenal - Chelsea\n1X2: Arsenal  2.45\nEverton v Fulham\nBoth Teams To Score: Yes  1.72\nTotal odds 4.21\nStake 1,000";

const TONE: Record<"good" | "warning" | "critical" | "neutral", string> = {
  good: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-500/40 dark:bg-emerald-500/10 dark:text-emerald-200",
  warning: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200",
  critical: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-500/40 dark:bg-rose-500/10 dark:text-rose-200",
  neutral: "border-stone-300 bg-stone-50 text-stone-800 dark:border-white/10 dark:bg-white/[0.04] dark:text-stone-200",
};
const SEVERITY: Record<SlipFault["severity"], { icon: string; className: string }> = {
  CRITICAL: { icon: "error", className: "text-rose-600 dark:text-rose-400" },
  WARNING: { icon: "warning", className: "text-amber-600 dark:text-amber-400" },
  MILD: { icon: "info", className: "text-sky-600 dark:text-sky-400" },
};
const ACTION: Record<ReCalibratedLeg["action"], { tone: "good" | "info" | "neutral"; label: string }> = {
  RETAINED: { tone: "good", label: "Kept" },
  SUBSTITUTED: { tone: "info", label: "Substituted" },
  DROPPED: { tone: "neutral", label: "Left out" },
};

const imageIn = (files: Iterable<File> | ArrayLike<File> | null | undefined): File | null =>
  Array.from(files ?? []).find((f) => f.type.startsWith("image/") || IMAGE_NAME.test(f.name)) ?? null;

const clipboardImage = (items: DataTransferItemList | null | undefined): File | null => {
  for (const item of Array.from(items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) return item.getAsFile();
  }
  return null;
};

const canReadClipboard = (): boolean =>
  typeof window !== "undefined" && window.isSecureContext && typeof navigator.clipboard?.read === "function";

const coarsePointer = (): boolean => typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)").matches === true;

const evTone = (ev: number | null): string =>
  ev === null ? "" : ev > 0 ? "text-emerald-700 dark:text-emerald-400" : ev < 0 ? "text-rose-600 dark:text-rose-400" : "";

export interface SlipScannerModalProps {
  onClose: () => void;
}

export const SlipScannerModal = ({ onClose }: SlipScannerModalProps) => {
  const navigate = useNavigate();
  const titleId = useId();
  const textId = useId();
  const box = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);
  const depth = useRef(0);
  const seq = useRef(0);

  const [mode, setMode] = useState<Mode>("image");
  const [image, setImage] = useState<File | null>(null);
  const [imageNote, setImageNote] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [book, setBook] = useState<ScanBook>("auto");
  const [stake, setStake] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ScanResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clipboard] = useState(canReadClipboard);
  const [phone] = useState(coarsePointer);

  const slipLegs = useParlaySlipStore((s) => s.legs);
  const clear = useParlaySlipStore((s) => s.clear);
  const addMany = useParlaySlipStore((s) => s.addMany);
  const setSkin = useParlaySlipStore((s) => s.setSkin);

  const close = useCallback(() => {
    seq.current += 1; // a scan still running no longer has anywhere to land
    onClose();
  }, [onClose]);

  // focus in, Escape out, the page behind held still, and focus handed back on close
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      opener?.focus?.();
    };
  }, [close]);

  // a file dropped beside the drop zone is still the slip, not a page the browser opens in place of BetDoc
  useEffect(() => {
    const hold = (e: globalThis.DragEvent) => {
      if (e.dataTransfer?.types.includes("Files")) e.preventDefault();
    };
    window.addEventListener("dragover", hold);
    window.addEventListener("drop", hold);
    return () => {
      window.removeEventListener("dragover", hold);
      window.removeEventListener("drop", hold);
    };
  }, []);

  const preview = useMemo(() => (image ? URL.createObjectURL(image) : null), [image]);
  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);

  const takeImage = useCallback((file: File, how: string) => {
    if (!file.type.startsWith("image/") && !IMAGE_NAME.test(file.name)) {
      toast.error("Not an image", `${file.name || "This file"} is not a screenshot or a photo.`);
      return;
    }
    seq.current += 1;
    setImage(file);
    setImageNote(`${how} · ${file.size >= 1e6 ? `${(file.size / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(file.size / 1e3))} KB`}`);
    setMode("image");
    setResult(null);
    setError(null);
    setBusy(false);
  }, []);

  const takeText = useCallback((value: string) => {
    seq.current += 1;
    setText(value);
    setMode("text");
    setResult(null);
    setError(null);
    setBusy(false);
  }, []);

  // Ctrl+V / Cmd+V: a screenshot is taken anywhere in the dialog; text goes where the user is typing, else to the text tab
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const file = clipboardImage(e.clipboardData?.items);
      if (file) {
        e.preventDefault();
        takeImage(file, "Pasted");
        return;
      }
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest("textarea, input, [contenteditable='true']")) return;
      const pasted = e.clipboardData?.getData("text/plain") ?? "";
      if (pasted.trim()) {
        e.preventDefault();
        takeText(pasted);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [takeImage, takeText]);

  const readClipboard = async () => {
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = item.types.find((t) => t.startsWith("image/"));
        if (type) {
          const blob = await item.getType(type);
          takeImage(new File([blob], `clipboard.${type.split("/")[1] ?? "png"}`, { type }), "From the clipboard");
          return;
        }
      }
      for (const item of items) {
        if (item.types.includes("text/plain")) {
          const value = await (await item.getType("text/plain")).text();
          if (value.trim()) {
            takeText(value);
            return;
          }
        }
      }
      toast.info("Nothing to scan on the clipboard", "Copy a screenshot of the slip, or its text, then try again.");
    } catch {
      toast.error("The clipboard was not read", "Allow BetDoc to read the clipboard, or press Ctrl+V (Cmd+V) in this window.");
    }
  };

  const onDrag = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.type === "dragenter") {
      depth.current += 1;
      setDragging(true);
    } else if (e.type === "dragleave") {
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    } else if (e.type === "dragover") {
      e.dataTransfer.dropEffect = "copy";
    }
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    depth.current = 0;
    setDragging(false);
    const file = imageIn(e.dataTransfer.files);
    if (file) {
      takeImage(file, "Dropped");
      return;
    }
    const dropped = e.dataTransfer.getData("text/plain");
    if (dropped.trim()) takeText(dropped);
    else if (e.dataTransfer.files.length) toast.error("Not an image", "Drop a screenshot or a photo of the slip (PNG, JPEG or WEBP).");
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>, how: string) => {
    const file = imageIn(e.target.files);
    e.target.value = ""; // the same file chosen again is a new pick
    if (file) takeImage(file, how);
  };

  const stakeValue = stake.trim() ? Number(stake) : null;
  const stakeValid = stakeValue === null || (Number.isFinite(stakeValue) && stakeValue > 0 && stakeValue <= 1e8);
  const ready = (mode === "image" ? image !== null : text.trim().length > 0) && stakeValid && !busy;

  const scan = async () => {
    if (!ready) return;
    const id = ++seq.current;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await scanSlip({ image: mode === "image" ? image : null, text: mode === "text" ? text : null, bookmaker: book, stakeInr: stakeValue });
      if (id !== seq.current) return;
      setResult(r);
    } catch (err) {
      if (id === seq.current) setError(err instanceof ApiError ? err.message : "No answer from the server: check the connection and scan again.");
    } finally {
      if (id === seq.current) setBusy(false);
    }
  };

  const openIn = (legs: SlipLeg[], where: "consultant" | "workbench", which: string) => {
    if (!result || !legs.length) return;
    clear();
    addMany(legs);
    setSkin(result.skin);
    close();
    navigate(where === "consultant" ? "/consultant" : "/oracle?tab=builder&mode=manual");
    toast.success(where === "consultant" ? "Opened in the Consultant" : "Loaded into the workbench",
                  `${which}: ${legs.length} leg${legs.length === 1 ? "" : "s"} at ${BOOKMAKER_THEMES[result.skin].label}`);
  };

  const again = () => {
    seq.current += 1;
    setResult(null);
    setError(null);
    setImage(null);
    setImageNote(null);
    setText("");
    setBusy(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center overflow-y-auto bg-stone-950/60 backdrop-blur-sm sm:items-start sm:p-4" role="presentation"
         onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} onDragEnter={onDrag} onDragOver={onDrag} onDragLeave={onDrag} onDrop={onDrop}
           className="relative flex min-h-full w-full flex-col bg-white text-stone-900 shadow-2xl sm:my-6 sm:min-h-0 sm:max-w-4xl sm:rounded-3xl dark:bg-stone-900 dark:text-stone-100 dark:ring-1 dark:ring-white/10">
        <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-stone-200 bg-white/95 px-4 py-3 backdrop-blur sm:rounded-t-3xl sm:px-6 dark:border-white/10 dark:bg-stone-900/95">
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-[#C89B3C]/15 text-[#A47C26] dark:text-[#E3C06A]" aria-hidden="true">
              <span className="material-symbols-outlined">document_scanner</span>
            </span>
            <div className="min-w-0">
              <h2 id={titleId} className="font-display text-base font-bold tracking-tight sm:text-lg">Scan a bet slip</h2>
              <p className="truncate text-[11px] text-stone-500 dark:text-stone-400">Read on BetDoc's server, vetted on the live board · nothing stored, nothing placed</p>
            </div>
          </div>
          <button type="button" onClick={close} aria-label="Close the scanner"
                  className="grid size-10 shrink-0 place-items-center rounded-full text-stone-500 transition hover:bg-stone-100 hover:text-stone-900 dark:hover:bg-white/10 dark:hover:text-white">
            <span className="material-symbols-outlined" aria-hidden="true">close</span>
          </button>
        </header>

        <div className="flex flex-1 flex-col gap-5 px-4 py-5 sm:px-6">
          {!result && (
            <>
              <div className="inline-flex self-start rounded-full bg-stone-100 p-1 text-xs font-semibold dark:bg-white/[0.06]" role="tablist" aria-label="What to scan">
                {(["image", "text"] as const).map((m) => (
                  <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => setMode(m)}
                          className={`rounded-full px-4 py-2 transition ${mode === m ? "bg-white shadow-sm dark:bg-stone-700" : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"}`}>
                    <span className="material-symbols-outlined mr-1 align-[-4px] text-[16px]" aria-hidden="true">{m === "image" ? "image" : "notes"}</span>
                    {m === "image" ? "Screenshot or photo" : "Paste or type"}
                  </button>
                ))}
              </div>

              {mode === "image" ? (
                <div role="tabpanel" className="flex flex-col gap-3">
                  <div data-autofocus="" tabIndex={0} role="button" aria-label="Choose a screenshot or photo of the slip, or drop one here"
                       onClick={() => picker.current?.click()} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), picker.current?.click())}
                       className={`relative flex min-h-[220px] cursor-pointer flex-col items-center justify-center gap-3 overflow-hidden rounded-2xl border-2 border-dashed p-5 text-center transition focus:outline-none focus-visible:ring-2 focus-visible:ring-[#C89B3C] ${
                         dragging ? "border-[#C89B3C] bg-[#C89B3C]/10" : "border-stone-300 hover:border-stone-400 dark:border-white/15 dark:hover:border-white/30"}`}>
                    {preview ? (
                      <>
                        <img src={preview} alt="The slip to scan" className="max-h-72 w-auto rounded-xl object-contain shadow-sm" />
                        <p className="text-xs text-stone-500 dark:text-stone-400">{imageNote} · {phone ? "tap to replace" : "click or drop another to replace"}</p>
                      </>
                    ) : (
                      <>
                        <span className="material-symbols-outlined text-5xl text-[#C89B3C]" aria-hidden="true">{dragging ? "file_download" : "add_photo_alternate"}</span>
                        <p className="text-sm font-semibold">{dragging ? "Drop the slip here" : phone ? "Tap to choose a screenshot" : "Drop a screenshot here, or click to choose one"}</p>
                        <p className="max-w-md text-xs text-stone-500 dark:text-stone-400">
                          {phone ? "Or take a photo of the slip with the camera below." : "Or press Ctrl+V (Cmd+V on a Mac) to paste one you copied."} The whole slip, straight on: every leg, its price, the total.
                        </p>
                      </>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" icon="folder_open" onClick={() => picker.current?.click()}>Choose a file</Button>
                    {phone && <Button size="sm" icon="photo_camera" onClick={() => camera.current?.click()}>Take a photo</Button>}
                    {clipboard && <Button size="sm" icon="content_paste" onClick={() => void readClipboard()}>Paste from the clipboard</Button>}
                    {image && <Button size="sm" variant="ghost" icon="delete" onClick={() => { setImage(null); setImageNote(null); }}>Remove</Button>}
                  </div>
                  <input ref={picker} type="file" accept="image/*" className="hidden" onChange={(e) => onPick(e, "Chosen")} aria-hidden="true" tabIndex={-1} />
                  <input ref={camera} type="file" accept="image/*" capture="environment" className="hidden" onChange={(e) => onPick(e, "Photographed")} aria-hidden="true" tabIndex={-1} />
                </div>
              ) : (
                <div role="tabpanel" className="flex flex-col gap-2">
                  <label htmlFor={textId} className="text-xs font-medium text-stone-500 dark:text-stone-400">
                    The slip's text: each leg as its fixture (Team A vs Team B), its market and selection, and its price
                  </label>
                  <textarea id={textId} data-autofocus="" value={text} onChange={(e) => setText(e.target.value)} rows={9} spellCheck={false} placeholder={TEXT_PLACEHOLDER}
                            className="w-full rounded-2xl bg-stone-100/80 px-4 py-3 font-mono text-sm text-stone-900 placeholder:text-stone-400 focus:bg-white focus:shadow-soft focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/40 dark:bg-white/[0.06] dark:text-stone-100 dark:focus:bg-stone-800" />
                  {clipboard && <Button size="sm" icon="content_paste" className="self-start" onClick={() => void readClipboard()}>Paste from the clipboard</Button>}
                </div>
              )}

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-stone-500 dark:text-stone-400">Vet at</span>
                  <select value={book} onChange={(e) => setBook(e.target.value as ScanBook)}
                          className="w-full rounded-2xl bg-stone-100/80 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/40 dark:bg-white/[0.06]">
                    {BOOKS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
                  </select>
                </label>
                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-stone-500 dark:text-stone-400">Stake (₹, optional: else the slip's own)</span>
                  <input type="number" inputMode="decimal" min={1} step="any" value={stake} onChange={(e) => setStake(e.target.value)} placeholder="e.g. 1000" aria-invalid={!stakeValid}
                         className={`w-full rounded-2xl bg-stone-100/80 px-4 py-2.5 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-[#C89B3C]/40 dark:bg-white/[0.06] ${stakeValid ? "" : "ring-2 ring-rose-400"}`} />
                </label>
              </div>

              <div aria-live="polite" className="flex flex-col gap-2">
                {busy && (
                  <p className="flex items-center gap-2 rounded-2xl bg-sky-50 p-3 text-xs text-sky-900 dark:bg-sky-500/10 dark:text-sky-200">
                    <span className="material-symbols-outlined animate-spin text-[18px]" aria-hidden="true">progress_activity</span>
                    {mode === "image" ? "Reading the slip, matching each leg to the live board and vetting it through the fortress…" : "Matching each leg to the live board and vetting it through the fortress…"}
                  </p>
                )}
                {error && <p role="alert" className="rounded-2xl bg-rose-50 p-3 text-xs text-rose-800 dark:bg-rose-500/10 dark:text-rose-200">{error}</p>}
              </div>
            </>
          )}

          {result && <ScanReport result={result} />}
        </div>

        <footer className="sticky bottom-0 flex flex-col gap-2 border-t border-stone-200 bg-white/95 px-4 py-3 backdrop-blur sm:rounded-b-3xl sm:px-6 dark:border-white/10 dark:bg-stone-900/95">
          {result ? (
            <>
              <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
                <Button size="sm" variant="primary" icon="medical_services" className={TAP} disabled={!result.consultant_legs.length}
                        onClick={() => openIn(result.consultant_legs, "consultant", "The scanned slip")}>
                  Open in the Consultant
                </Button>
                {result.is_recalibrated && result.recalibrated_consultant_legs.length > 0 && (
                  <Button size="sm" icon="auto_fix_high" className={TAP} onClick={() => openIn(result.recalibrated_consultant_legs, "consultant", "The re-calibrated slip")}>
                    Open the re-calibrated slip
                  </Button>
                )}
                <Button size="sm" variant="ghost" icon="construction" className={TAP} disabled={!result.consultant_legs.length}
                        onClick={() => openIn(result.consultant_legs, "workbench", "The scanned slip")}>
                  Load into the workbench
                </Button>
                <Button size="sm" variant="ghost" icon="restart_alt" className={TAP} onClick={again}>Scan another</Button>
              </div>
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                {result.consultant_legs.length ? "Only vetted legs open: the Consultant seals the verdict." : "No leg is on the live board, so there is nothing to open."}
                {slipLegs.length > 0 && result.consultant_legs.length > 0 && ` Opening replaces the ${slipLegs.length}-leg slip on the board.`}
              </p>
            </>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-[11px] text-stone-500 dark:text-stone-400">Developed for Amit Ashok Kumar Patnaik</p>
              <Button variant="primary" icon="document_scanner" busy={busy} disabled={!ready} onClick={() => void scan()}>Scan and rate</Button>
            </div>
          )}
        </footer>
      </div>
    </div>
  );
};

// ================================================================ the report: every figure the server's
const Fact = ({ label, value, hint, className = "" }: { label: string; value: ReactNode; hint?: ReactNode; className?: string }) => (
  <div className={`min-w-0 rounded-2xl bg-stone-50 p-3 dark:bg-white/[0.04] ${className}`}>
    <span className="block text-[10px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">{label}</span>
    <span className="block truncate font-mono text-sm font-bold tabular-nums">{value}</span>
    {hint && <span className="block text-[11px] text-stone-500 dark:text-stone-400">{hint}</span>}
  </div>
);

const Section = ({ title, icon, children, aside }: { title: string; icon: string; children: ReactNode; aside?: ReactNode }) => (
  <section className="flex flex-col gap-3">
    <h3 className="flex items-center justify-between gap-2 text-sm font-bold">
      <span className="flex items-center gap-2"><span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">{icon}</span>{title}</span>
      {aside}
    </h3>
    {children}
  </section>
);

const ScanReport = ({ result }: { result: ScanResult }) => {
  const style = VERDICT_STYLE[result.verdict];
  const theme = BOOKMAKER_THEMES[result.skin];
  const skinVars = { "--wb-primary": theme.primary, "--wb-on-primary": theme.onPrimary, "--wb-muted": theme.muted, "--wb-card": theme.card,
                     background: theme.surface, color: theme.text } as CSSProperties;
  const recalTier = result.recalibrated_tier && result.recalibrated_tier in TIER_STYLE ? TIER_STYLE[result.recalibrated_tier as Tier] : null;
  const beforeTier = result.tier && result.tier in TIER_STYLE ? TIER_STYLE[result.tier as Tier] : null;

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      toast.success("Booking code copied", code);
    } catch {
      toast.error("Not copied", "The browser refused the clipboard: select the code instead.");
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <div className={`flex flex-col gap-2 rounded-2xl border p-4 ${TONE[style.tone]}`} role="status">
        <div className="flex flex-wrap items-center gap-2">
          <span className="material-symbols-outlined" aria-hidden="true">{style.icon}</span>
          <strong className="text-sm uppercase tracking-wide">{style.label}</strong>
          <span className="rounded-full bg-black/5 px-2.5 py-0.5 text-[11px] font-bold dark:bg-white/10">{result.verdict_badge}</span>
          {result.vetting_score !== null && <span className="ml-auto font-mono text-lg font-black tabular-nums">{Math.round(result.vetting_score)}<span className="text-xs font-semibold"> / 100</span></span>}
        </div>
        <p className="text-xs leading-relaxed">{result.verdict_explanation}</p>
        <p className="text-[11px] opacity-80">
          {result.coverage.vetted} of {result.coverage.read} leg{result.coverage.read === 1 ? "" : "s"} vetted · read from {result.source === "image" ? "the image" : "the text"}
          {result.bookmaker ? ` · at ${BOOKMAKER_THEMES[result.skin].label}` : " · at the best retail price"} · {result.scan_id}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Fact label="Total odds" value={result.total_scanned_odds ?? result.printed_total_odds ?? "—"}
              hint={result.odds_check === "MATCH" ? "matches the slip's total" : result.odds_check === "MISMATCH" ? `the slip prints ${result.printed_total_odds}` : result.printed_total_odds ? "printed on the slip" : undefined} />
        <Fact label="Stake" value={inr(result.stake_inr)} hint={result.detected_stake_inr && result.stake_inr === result.detected_stake_inr ? "from the slip" : undefined} />
        <Fact label="Payout if every leg wins" value={inr(result.estimated_payout_inr ?? result.printed_potential_return_inr)}
              hint={result.estimated_payout_inr ? "stake × the legs' prices" : result.printed_potential_return_inr ? "printed on the slip" : undefined} />
        <Fact label="Joint win chance" value={pct(result.joint_win_probability_pct)} hint="the fortress, every leg vetted" />
        <Fact label="EV at the slip's prices" value={<span className={evTone(result.overall_ev_pct)}>{signedPoints(result.overall_ev_pct)}</span>}
              hint="against the books' consensus" />
        <Fact label="Book's margin" value={pct(result.bookmaker_margin_pct, 2)} hint="its overround on these markets" />
        <Fact label="Structure" value={result.detected_kind ? result.detected_kind.replace(/_/g, " ").toLowerCase() : "not printed"} />
        <Fact label="Booking code" value={result.detected_booking_code ?? "none read"}
              hint={result.detected_booking_code ? <button type="button" className="underline" onClick={() => void copyCode(result.detected_booking_code ?? "")}>copy</button> : undefined} />
      </div>
      {result.detected_booking_code && (
        <p className="-mt-3 text-[11px] text-stone-500 dark:text-stone-400">A booking code is shown, never opened: loading it would need the book's own site, which BetDoc does not scrape.</p>
      )}

      <Section title="The legs" icon="format_list_numbered">
        <ol className="flex flex-col gap-2">
          {result.legs_scanned.map((leg) => <LegCard key={leg.number} leg={leg} book={result.bookmaker ? BOOKMAKER_THEMES[result.skin].label : null} />)}
        </ol>
      </Section>

      {result.rating && (
        <Section title="BetDoc Slip Score" icon="speed" aside={<span className="text-[11px] font-normal text-stone-500 dark:text-stone-400">the 15-pillar fortress, as the Consultant scores it</span>}>
          <div className="rounded-2xl p-1" style={skinVars}>
            <SlipScoreMeter rating={result.rating} />
          </div>
        </Section>
      )}

      {result.faults_detected.length > 0 && (
        <Section title="What the scan found" icon="stethoscope">
          <ul className="flex flex-col gap-2">
            {result.faults_detected.map((f, i) => (
              <li key={`${f.code}-${i}`} className="flex gap-2 rounded-2xl border border-stone-200 p-3 text-xs dark:border-white/10">
                <span className={`material-symbols-outlined shrink-0 text-[18px] ${SEVERITY[f.severity].className}`} aria-label={f.severity.toLowerCase()}>{SEVERITY[f.severity].icon}</span>
                <span className="min-w-0">
                  <span className="mb-0.5 flex flex-wrap items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">
                    {f.source === "doctor" ? "The Doctor" : "The scanner"} · {f.code.replace(/_/g, " ").toLowerCase()}
                    {f.legs.length > 0 && <span className="rounded bg-stone-100 px-1.5 py-0.5 dark:bg-white/10">leg {f.legs.join(", ")}</span>}
                  </span>
                  <span className="leading-relaxed">{f.text}</span>
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {(result.is_recalibrated || result.recalibration_summary) && (
        <Section title="Re-calibration" icon="auto_fix_high"
                 aside={result.is_recalibrated && result.vetting_score !== null && result.recalibrated_score !== null ? (
                   <span className="font-mono text-xs tabular-nums">
                     <span style={{ color: beforeTier?.color }}>{Math.round(result.vetting_score)}</span> → <strong style={{ color: recalTier?.color }}>{Math.round(result.recalibrated_score)} {result.recalibrated_tier}</strong>
                   </span>
                 ) : undefined}>
          {result.recalibration_summary && <p className="text-xs leading-relaxed text-stone-600 dark:text-stone-300">{result.recalibration_summary}</p>}
          {result.is_recalibrated && (
            <>
              <ol className="flex flex-col gap-2">
                {result.recalibrated_legs.map((leg, i) => (
                  <li key={`${leg.leg_id ?? leg.fixture}-${i}`} className={`rounded-2xl border p-3 text-xs ${leg.action === "DROPPED" ? "border-dashed border-stone-300 opacity-75 dark:border-white/15" : "border-stone-200 dark:border-white/10"}`}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-2">
                        <Pill tone={ACTION[leg.action].tone}>{ACTION[leg.action].label}</Pill>
                        <span className="truncate font-semibold">{leg.fixture}</span>
                      </span>
                      <span className="font-mono font-bold">{leg.price ?? "—"}</span>
                    </div>
                    <p className="mt-1 text-stone-600 dark:text-stone-300">{leg.label}
                      {leg.fair_probability !== null && <> · fair {(leg.fair_probability * 100).toFixed(1)}%</>}
                      {leg.ev !== null && <> · EV <span className={evTone(leg.ev)}>{signedPct(leg.ev)}</span></>}
                    </p>
                    {leg.replaces && <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">In place of {leg.replaces}</p>}
                    <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">{leg.rationale}</p>
                  </li>
                ))}
              </ol>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                <Fact label="Total odds" value={result.recalibrated_total_odds ?? "—"} />
                <Fact label="Joint win chance" value={pct(result.recalibrated_joint_win_prob_pct)} />
                <Fact label="Joint EV" value={<span className={evTone(result.recalibrated_ev_pct)}>{signedPoints(result.recalibrated_ev_pct)}</span>} />
                <Fact label="Payout at your stake" value={inr(result.recalibrated_projected_payout_inr)} />
                <Fact label="KUMBHA's Kelly stake" value={inr(result.recommended_kelly_stake_inr)} className="col-span-2 sm:col-span-1"
                      hint={result.recommended_kelly_stake_inr ? "fractional Kelly on your bankroll" : "no bankroll to size against: fund the CFO main account"} />
              </div>
            </>
          )}
        </Section>
      )}

      {result.ocr_lines.length > 0 && (
        <details className="rounded-2xl border border-stone-200 p-3 text-xs dark:border-white/10">
          <summary className="cursor-pointer font-semibold">What was read ({result.ocr_lines.length} line{result.ocr_lines.length === 1 ? "" : "s"})</summary>
          <ol className="mt-2 flex flex-col gap-0.5 font-mono">
            {result.ocr_lines.map((line, i) => (
              <li key={i} className={`flex justify-between gap-3 ${line.parsed ? "" : "text-stone-400 dark:text-stone-500"}`} title={line.parsed ? undefined : "read too uncertainly to use in full"}>
                <span className="min-w-0 whitespace-pre-wrap break-words">{line.text}</span>
                <span className="shrink-0 tabular-nums">{Math.round(line.confidence * 100)}%</span>
              </li>
            ))}
          </ol>
        </details>
      )}
      <p className="text-center text-[10px] text-stone-500 dark:text-stone-400">Nothing is placed from here; no result is promised. <strong>Developed for {result.developer_credit}</strong></p>
    </div>
  );
};

const LegCard = ({ leg, book }: { leg: ScannedLeg; book: string | null }) => {
  const matched = leg.status === "MATCHED";
  const read = [leg.market_read && leg.selection_read ? `${leg.market_read}: ${leg.selection_read}` : leg.market_read || leg.selection_read].filter(Boolean).join("");
  return (
    <li className={`rounded-2xl border p-3 text-xs ${matched ? "border-stone-200 dark:border-white/10" : "border-amber-300/70 bg-amber-50/50 dark:border-amber-500/30 dark:bg-amber-500/[0.06]"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <span className="grid size-6 shrink-0 place-items-center rounded-full bg-stone-100 font-mono text-[11px] font-bold dark:bg-white/10">{leg.number}</span>
          <span className="truncate font-semibold">{leg.fixture ?? leg.fixture_read ?? "Fixture not read"}</span>
        </span>
        <span className="flex items-center gap-2">
          <Pill tone={matched ? "good" : "warning"} icon={matched ? "check_circle" : "help"}>{STATUS_LABEL[leg.status]}</Pill>
          <span className="font-mono text-sm font-bold">{leg.odds_read ?? "—"}</span>
        </span>
      </div>
      <p className="mt-1.5 text-stone-600 dark:text-stone-300">
        <span className="text-stone-400 dark:text-stone-500">Read: </span>{leg.fixture_read ?? "no fixture"}{read ? ` · ${read}` : ""}
        {leg.confidence < 0.8 && <span className="ml-1 text-amber-600 dark:text-amber-400">({Math.round(leg.confidence * 100)}% sure)</span>}
      </p>
      {matched ? (
        <p className="mt-1 text-stone-600 dark:text-stone-300">
          <span className="text-stone-400 dark:text-stone-500">Board: </span>{leg.label}
          {leg.fair_probability !== null && <> · fair {(leg.fair_probability * 100).toFixed(1)}%{leg.consensus_books ? ` (${leg.consensus_books} books)` : ""}</>}
          {leg.ev_at_slip_odds !== null && <> · EV at {leg.odds_read} <span className={evTone(leg.ev_at_slip_odds)}>{signedPct(leg.ev_at_slip_odds)}</span></>}
          {leg.book_price_now && <> · {book ?? "the book"} now {leg.book_price_now}</>}
        </p>
      ) : (
        <p className="mt-1 text-amber-800 dark:text-amber-300">{leg.note}</p>
      )}
    </li>
  );
};

export default SlipScannerModal;
