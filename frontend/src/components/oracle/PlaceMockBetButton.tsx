/**
 * Place Mock Bet Button for Ashoka Value Recommendations (Group 80).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * PAPER mode: one click forward-tests the slip on the sandbox. ASHVATTHAMA snaps each leg's live Parimatch price and
 * fills at it (or refuses when the price moved more than the tolerance, or the market is suspended).
 * LIVE mode: the button asks ASHVATTHAMA for the real-money ticket and opens Parimatch; nothing is placed automatically.
 * System bets (Trixie, Yankee...) are several bets at once: the sandbox takes singles and parlays only.
 */
import { useState } from "react";
import { requestLiveTicket, type OracleSlipInput } from "../../lib/paper_trading";
import { SYSTEM_KINDS, type Slip } from "../../lib/oracle";
import { explainPaperError, usePaperTradingStore } from "../../store/usePaperTradingStore";
import { toast } from "../../store/useToastStore";
import { Button } from "../../ui/kit";
import { formatINR } from "../../utils/formatCurrency";

const TARGET = "parimatch";

type FlatProps = {
  slip?: undefined;
  sportKey: string;
  matchId: string;
  eventName: string;
  marketType: string;
  selection: string;
  odds: number;
  recommendedStakeINR?: number;
};
type SlipProps = { slip: Slip; book?: string };
type PlaceMockBetButtonProps = FlatProps | SlipProps;

interface Plan {
  input: OracleSlipInput | null;
  flat: FlatProps | null;
  stake: number;
  odds: number;
  description: string;
  blocked: string | null;
}

function plan(props: PlaceMockBetButtonProps): Plan {
  if (props.slip === undefined) {
    const stake = props.recommendedStakeINR ?? 1000;
    return { input: null, flat: props, stake, odds: props.odds, description: `${props.eventName}: ${props.selection}`, blocked: null };
  }
  const slip = props.slip;
  const stake = Number(slip.stake_inr) > 0 ? Number(slip.stake_inr) : Number(slip.reference_stake_inr);
  const legs = slip.legs.map((leg) => ({
    sport_key: leg.sport_key,
    match_id: leg.fixture_id,
    market: leg.market,
    selection: leg.selection,
    odds: leg.prices[TARGET] ?? leg.prices[props.book ?? slip.book] ?? (slip.legs.length === 1 ? slip.odds : Number.NaN),
    event_name: leg.fixture,
  }));
  const odds = legs.reduce((acc, leg) => acc * leg.odds, 1);
  const fair = slip.legs.reduce((acc, leg) => acc * leg.fair_probability, 1);
  const blocked = SYSTEM_KINDS.includes(slip.kind)
    ? "System bets are several bets at once: the sandbox takes singles and parlays"
    : legs.some((leg) => !Number.isFinite(leg.odds) || leg.odds <= 1)
      ? "A leg has no price to forward-test at"
      : !(stake > 0)
        ? "No stake recommended"
        : null;
  return {
    input: { legs, stake_amount: stake, target_bookmaker: TARGET, fair_probability: fair > 0 && fair < 1 ? fair : null, strategy: "ashoka", slip_id: slip.slip_id, event_name: slip.title },
    flat: null,
    stake,
    odds,
    description: slip.title,
    blocked,
  };
}

export const PlaceMockBetButton = (props: PlaceMockBetButtonProps) => {
  const isPaperMode = usePaperTradingStore((s) => s.isPaperMode);
  const placeMockBet = usePaperTradingStore((s) => s.placeMockBet);
  const placeAshokaSlip = usePaperTradingStore((s) => s.placeAshokaSlip);
  const [busy, setBusy] = useState(false);
  const [placed, setPlaced] = useState(false);
  const p = plan(props);

  const paper = async () => {
    setBusy(true);
    const bet = p.input
      ? await placeAshokaSlip(p.input)
      : await placeMockBet({
          sport_key: p.flat?.sportKey, match_id: p.flat?.matchId, event_name: p.flat?.eventName, market_type: p.flat?.marketType,
          selection: p.flat?.selection, odds: p.flat?.odds, stake_amount: p.stake, target_bookmaker: TARGET, source: "ASHOKA",
        });
    setBusy(false);
    if (bet) {
      setPlaced(true);
      window.setTimeout(() => setPlaced(false), 3_000);
    }
  };

  const live = async () => {
    setBusy(true);
    try {
      const ticket = await requestLiveTicket({ target_bookmaker: TARGET, odds: Number(p.odds.toFixed(4)), stake_amount: p.stake, description: p.description });
      toast.info("ASHVATTHAMA ticket", ticket.reason ?? "Place it on Parimatch and record it in the ledger.");
      if (ticket.venue_url) window.open(ticket.venue_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      toast.error("No ticket", explainPaperError(err));
    } finally {
      setBusy(false);
    }
  };

  if (!isPaperMode) {
    return (
      <Button size="sm" icon="open_in_new" busy={busy} disabled={p.blocked !== null} title={p.blocked ?? "Real money: ASHVATTHAMA issues the Parimatch ticket"} onClick={() => void live()}>
        Parimatch ticket
      </Button>
    );
  }
  return (
    <Button
      size="sm"
      variant={placed ? "primary" : "secondary"}
      icon={placed ? "check" : "experiment"}
      busy={busy}
      disabled={placed || p.blocked !== null}
      title={p.blocked ?? "Forward-test on the Paper Trading Sandbox at Parimatch's live price"}
      onClick={() => void paper()}
    >
      {placed ? "Mock bet placed" : `Mock bet (${formatINR(p.stake)})`}
    </Button>
  );
};
