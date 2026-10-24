/**
 * The BetDoc Consultant (Group 105, ASHVATTHAMA, points b.6 and b.13b): a board of three specialists on the Oracle's
 * slip. The Doctor diagnoses the slip from the 15-pillar fortress and prescribes the auto-fix's best quoted leg; the
 * Lawyer lists the settlement rules its legs fall under at the book; the Consultant sizes it against the CFO bankroll
 * with KUMBHA's fractional Kelly, the Section 115BBJ tax and the stop-loss floor; TipMaster sets the measured crowd
 * against BetDoc's maths. The verdict is signed and recorded. The slip is the one the match board and the bet builder
 * share; nothing is placed from here. Developed for Amit Ashok Kumar Patnaik.
 */
import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { CapitalAdvisorCard } from "../components/consultant/CapitalAdvisorCard";
import { DoctorCard } from "../components/consultant/DoctorCard";
import { LawyerCard } from "../components/consultant/LawyerCard";
import { TipMasterConsensusPanel } from "../components/consultant/TipMasterConsensusPanel";
import { VerdictBanner } from "../components/consultant/VerdictBanner";
import { VerdictHistory } from "../components/consultant/VerdictHistory";
import { SlipScannerModal } from "../components/slip_scanner/SlipScannerModal";
import { BOOKMAKER_THEMES, type Skin, STRUCTURES } from "../lib/backtest_and_parlay";
import { applyPrescription, type ConsultantReport, consultSlip, toSlipLeg } from "../lib/consultant";
import { invalidate } from "../lib/resource";
import { priceAt, slipSignature, useParlaySlipStore } from "../store/useParlaySlipStore";
import { toast } from "../store/useToastStore";
import { CommanderHero } from "../ui/hero";
import { MOTIFS } from "../ui/motifs";
import { Button, ComponentGuideBanner, EmptyState, Page, Panel, Pill, Select } from "../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");

export const TheConsultant = () => {
  const navigate = useNavigate();
  const legs = useParlaySlipStore((s) => s.legs);
  const skin = useParlaySlipStore((s) => s.skin);
  const setSkin = useParlaySlipStore((s) => s.setSkin);
  const removeLeg = useParlaySlipStore((s) => s.remove);
  const replaceLeg = useParlaySlipStore((s) => s.replace);
  const [kind, setKind] = useState<string | null>(null);
  // a report holds only for the exact legs, structure and skin it examined
  const [result, setResult] = useState<{ sig: string; report: ConsultantReport } | null>(null);
  const [failure, setFailure] = useState<{ sig: string; message: string } | null>(null);
  const [busy, setBusy] = useState<"consult" | "apply" | null>(null);
  const [scanning, setScanning] = useState(false); // Group 126: a slip from outside BetDoc, read and vetted
  const requested = useRef<string | null>(null);
  const seq = useRef(0);

  const theme = BOOKMAKER_THEMES[skin];
  const structures = STRUCTURES.filter((s) => legs.length >= s.legs[0] && legs.length <= s.legs[1]);
  const chosen = kind && structures.some((s) => s.kind === kind) ? kind : structures[0]?.kind ?? null;
  const signature = slipSignature(legs, skin, chosen);
  const report = result?.sig === signature ? result.report : null;

  const run = useCallback(async (sig: string, body: { leg_ids: string[]; kind: string | null; skin: Skin }) => {
    const id = ++seq.current;
    setBusy("consult");
    try {
      const r = await consultSlip(body);
      if (id !== seq.current) return;
      setResult({ sig, report: r });
      setFailure(null);
      invalidate("consultant:verdicts");
    } catch (err) {
      if (id === seq.current) setFailure({ sig, message: refusal(err) });
    } finally {
      if (id === seq.current) setBusy(null);
    }
  }, []);

  // consult on arrival and whenever the slip, its structure or the book changes (each consultation is a signed verdict)
  useEffect(() => {
    if (!legs.length || requested.current === signature) return;
    const timer = setTimeout(() => {
      requested.current = signature;
      void run(signature, { leg_ids: legs.map((l) => l.leg_id), kind: chosen, skin });
    }, 400);
    return () => clearTimeout(timer);
  }, [signature, legs, chosen, skin, run]);

  const reconsult = () => {
    requested.current = signature;
    void run(signature, { leg_ids: legs.map((l) => l.leg_id), kind: chosen, skin });
  };

  const apply = async () => {
    const rx = report?.doctor.prescription;
    if (!rx?.available || !rx.replacement) return;
    const replacement = rx.replacement;
    seq.current += 1; // a consultation still in flight no longer describes the slip
    setBusy("apply");
    try {
      const r = await applyPrescription({ leg_ids: legs.map((l) => l.leg_id), kind: chosen, skin, replace_leg_id: rx.replaced.leg_id, with_leg_id: replacement.leg_id });
      const fresh = toSlipLeg(replacement);
      const sig = slipSignature(legs.map((l) => (l.leg_id === rx.replaced.leg_id ? fresh : l)), skin, chosen);
      requested.current = sig;
      replaceLeg(rx.replaced.leg_id, fresh);
      setResult({ sig, report: r });
      setFailure(null);
      invalidate("consultant:verdicts");
      toast.success(`Slip Score ${Math.round(rx.before.score)} → ${Math.round(r.rating.score)}`, `${rx.replaced.label} swapped for ${replacement.label}: ${r.verdict.title}.`);
    } catch (err) {
      toast.error("Prescription refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };

  const headline = report
    ? `${report.verdict.title}: ${legs.length}-leg slip at ${report.slip.book ?? theme.label}, Slip Score ${Math.round(report.rating.score)}.`
    : legs.length
      ? `ASHVATTHAMA examining a ${legs.length}-leg slip at ${theme.label}…`
      : "ASHVATTHAMA ready. Build a slip on the match board or in the bet builder, then bring it here.";
  const style = { "--wb-primary": theme.primary } as CSSProperties;
  const stale = failure?.sig === signature ? failure.message : null;

  return (
    <Page>
      <CommanderHero
        commander="ASHVATTHAMA"
        headline={headline}
        motif={MOTIFS.constellation}
        detail={report ? <>Sealed {report.seal_algorithm} · {report.doctor.pathologies.length} patholog{report.doctor.pathologies.length === 1 ? "y" : "ies"} · {report.lawyer.clauses.length} rule{report.lawyer.clauses.length === 1 ? "" : "s"} · crowd: {report.tipmaster.label.toLowerCase()}</> : undefined}
        actions={<>
          <Button variant="primary" icon="document_scanner" onClick={() => setScanning(true)}>Scan External Slip</Button>
          <Button icon="stadium" onClick={() => navigate("/oracle?tab=board")}>Match board</Button>
        </>}
      />
      <ComponentGuideBanner
        className="lg:col-span-12"
        moduleName="BetDoc Consultant"
        purpose="Three specialists on the slip you built: the Doctor diagnoses it from the 15-pillar fortress, the Lawyer reads the settlement rules its legs fall under, and the Consultant sizes it against your bankroll; TipMaster sets the measured crowd against BetDoc's maths. The verdict is signed. Nothing is placed from here."
        steps={[
          "Build the slip on the match board or in the bet builder, then pick the book you bet from here",
          "Read the Doctor's pathologies and apply the 1-click prescription when one is offered; check the Lawyer's rules",
          "Stake at most the Consultant's Kelly figure, note the stop-loss floor, then inspect and record it in the workbench",
        ]}
        kpiCodes={["SLIP_SCORE", "EV", "OVERROUND", "KELLY_STAKE"]}
      />
      <Panel title="The slip on the board" icon="receipt_long" subtitle="the Oracle's slip, shared with the match board and the bet builder" className="lg:col-span-12"
             actions={legs.length ? <Button size="sm" icon="refresh" busy={busy === "consult"} onClick={reconsult}>Consult again</Button> : undefined}>
        {legs.length === 0 ? (
          <EmptyState icon="medical_services" title="No slip to examine"
                      detail="The Consultant examines the slip you build. Pick prices on the match board, or load a slip from the bet builder."
                      action={<div className="flex gap-2"><Button variant="primary" icon="stadium" onClick={() => navigate("/oracle?tab=board")}>Match board</Button>
                        <Button icon="construction" onClick={() => navigate("/oracle?tab=builder")}>Bet builder</Button>
                        <Button icon="document_scanner" onClick={() => setScanning(true)}>Scan a slip</Button></div>} />
        ) : (
          <div className="flex flex-col gap-3" style={style}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Bookmaker">
                {(Object.keys(BOOKMAKER_THEMES) as Skin[]).map((key) => {
                  const t = BOOKMAKER_THEMES[key];
                  const on = skin === key;
                  return (
                    <button key={key} type="button" role="radio" aria-checked={on} onClick={() => setSkin(key)}
                            className="rounded-full px-3 py-1 text-xs font-bold transition"
                            style={on ? { background: t.primary, color: t.onPrimary } : { background: "color-mix(in srgb, currentColor 6%, transparent)" }}>
                      {t.label}
                    </button>
                  );
                })}
              </div>
              {structures.length > 1 && (
                <Select aria-label="Structure" value={chosen ?? ""} onChange={(e) => setKind(e.target.value)} className="w-auto">
                  {structures.map((s) => <option key={s.kind} value={s.kind}>{s.label}</option>)}
                </Select>
              )}
            </div>
            <ul className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
              {legs.map((l) => {
                const price = priceAt(l, theme.book);
                return (
                  <li key={l.leg_id} className="flex items-center justify-between gap-2 rounded-xl border border-stone-200 p-2.5 text-xs dark:border-white/[0.08]">
                    <span className="min-w-0">
                      <span className="block truncate font-semibold">{l.fixture}</span>
                      <span className="block truncate text-stone-500 dark:text-stone-400">{l.label}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="font-mono font-bold">{price === null ? "—" : price.toFixed(2)}</span>
                      <button type="button" aria-label={`Remove ${l.label}`} onClick={() => removeLeg(l.leg_id)} className="text-stone-400 hover:text-rose-500">
                        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">close</span>
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>
            {busy === "consult" && !report && <Pill tone="info" icon="hourglass_top">The three specialists are examining the slip…</Pill>}
            {stale && <p className="rounded-xl bg-rose-50 p-3 text-xs text-rose-700 dark:bg-rose-950/30 dark:text-rose-300">The Consultant could not examine this slip: {stale}</p>}
          </div>
        )}
      </Panel>
      {report && (
        <>
          <VerdictBanner report={report} onRecord={() => navigate("/oracle?tab=builder&mode=manual")} onTicket={() => navigate(`/bet-slip?report=${report.report_id}`)} />
          <DoctorCard report={report} onApply={() => void apply()} applying={busy === "apply"} />
          <LawyerCard report={report} />
          <CapitalAdvisorCard report={report} />
          <TipMasterConsensusPanel report={report} />
        </>
      )}
      <VerdictHistory />
      {scanning && <SlipScannerModal onClose={() => setScanning(false)} />}
    </Page>
  );
};

export default TheConsultant;
