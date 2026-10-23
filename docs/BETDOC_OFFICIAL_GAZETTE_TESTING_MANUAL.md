<div align="center" style="border-bottom: 2px solid #C89B3C; padding-bottom: 16px; margin-bottom: 20px;">

# THE GAZETTE OF BETDOC QUANTITATIVE SYSTEMS

**EXTRAORDINARY · PUBLISHED BY AUTHORITY OF THE ARCHITECT**

Ref. No. **BD/GAZ/2026/G116-SOP** · Issued **23 October 2026**

</div>

> **PUBLIC NOTICE.** This is BetDoc's own *standard operating procedure and testing manual*, issued for the guidance of
> authorised operators and testers under the authority of **Amit Ashok Kumar Patnaik**, chief architect. It is a product
> manual, not a government publication, and nothing in it is a promise of profit. The same text is served by the
> application at **`/manual`** (`frontend/src/pages/TestingManual.tsx`), where the checklist can be ticked and printed.

---

## PART I — THE CHARTER (THE KRISHNA-ARJUNA LAW)

**1. What BetDoc is.** An analytical advisor and an honest ledger. BetDoc does **not** hold your money, does **not** take
bank deposits, does **not** place bets or take cashouts at any bookmaker, and does not read your bookmaker account. The
rupees in its vault are your own record of what you did.

- **BetDoc is Krishna, the strategist and accountant.** ASHOKA prices the live board, ASHVATTHAMA (the Consultant) seals
  each verdict, KUMBHA sizes the stake, SHIVAJI guards the losses, VIDUR reads the news, and TODAR MAL keeps every rupee
  and the 30% TDS to the paisa.
- **You are Arjuna, the warrior and the owner of the money.** Your capital stays in your own Parimatch or 1xBet account.
  You read the advice, place each ticket with your own hands, copy the booking code back to BetDoc, and take any cashout
  at the book yourself.

---

## PART II — THE SIX STEPS OF A BETTING DAY

```
[ 1 ] Read the morning signal (Telegram / SMS, 07:55 IST)
  │
[ 2 ] Build the street-smart slip  (The Oracle → Bet builder → 5(d) Street-smart & splitter)
  │
[ 3 ] Split it into three tickets  (Fortress · Growth · Moonshot)
  │
[ 4 ] Place each ticket by hand at Parimatch / 1xBet, then enter its booking code
  │
[ 5 ] Follow the in-play cashout advisor and the stop-loss floor
  │
[ 6 ] Review the rupee ledger and the 30% TDS in The Vault
```

### Step 1 — Read the morning signal (07:55 IST)

At 07:55 IST TODAR MAL scores the day out of 100 from what it can measure: Parimatch and 1xBet's margins on today's match
odds, how many top-tier fixtures are priced, rotation news and depleted squads from the Wire, and how many legs are priced
at or above fair. What it cannot measure is named, never guessed.

| Score | Tier | What happens | Your action |
| :--- | :--- | :--- | :--- |
| below 60 | **REST DAY** | a warning with the measured reasons goes to Telegram and SMS | do not bet today |
| 60 – 79 | **SELECTIVE** | nothing is sent | light stakes, anchors only |
| 80 and above | **PRIME HARVEST** | the best anchors go to your phone | open BetDoc |

When the signal goes out, that morning's 08:00 hype stays silent. Send `/today` to the bot at any time for the current
score.

### Step 2 — Build the street-smart slip on The Oracle

The Oracle → Bet builder → **5(d) Street-smart & splitter**. Give a stake and a target return and choose Parimatch or
1xBet. BetDoc takes the safest legs on the live board that the book prices at or above fair, one per match, spread across
Football, Cricket, Tennis and Basketball as the board allows, until the target is reached. Each leg shows its price, its
fair probability and its EV. If the target is out of reach, it says so. Player markets (wickets, goalscorers) are not on
the board and are never used.

### Step 3 — Split it into three tickets

| Ticket | Legs | Stake | Purpose |
| :--- | :--- | :--- | :--- |
| **Slip A · Fortress** | the fewest anchors (3 to 5) whose odds return the whole session stake | 40% | the capital shield: on only when its payout covers the session stake |
| **Slip B · Growth** | about two thirds of the legs, up to 9 | 40% | compounding across sports |
| **Slip C · Moonshot** | every leg | 20% | the full upside |

**Read the shield honestly.** Slip A's anchors are on all three tickets. If a leg only B or C carries loses, Slip A can
still return the session; if one of Slip A's anchors loses, all three tickets lose. The page shows the four outcomes
with their chances and how often the session gets its stake back.

*Worked example (the splitter's own test):* fifteen legs at 1.25 each, ₹250 session. Slip A takes 5 legs (1.25⁴ = 2.44
would not cover; 1.25⁵ = 3.05 does): ₹100 returns ₹305.18. Slip B: 9 legs, ₹100 returns ₹745.06. Slip C: 15 legs, ₹50
returns ₹1,421.09. Slip A pays whenever its five anchors all win, which at 85% each is 44.4% of the time.

### Step 4 — Place each ticket by hand and enter its booking code

1. Press **Record the 3 tickets**: they are saved as ready to place; no money is locked.
2. Open the book from the ticket's link and place it there, at the stake and odds shown or at what the book gives.
3. Copy the booking code the book issues (for example `5X7A9B`).
4. Under **Your system tickets**, enter the code and press **Lock stake & confirm**. A Bet-Slip ticket from the Consultant
   is confirmed the same way on `/bet-slip`.

TODAR MAL then moves the stake from liquid cash to locked exposure, the ticket's legs are tracked for settlement, and the
in-play watch starts. SHIVAJI refuses the lock when cash is short, the 15% daily stop-loss is latched, a cool-down runs, or
the kill switch is on.

### Step 5 — Follow the in-play cashout advisor

BetDoc never sees the book's cashout offer: you read it in the app and type it on the placed ticket. The advice compares
it with the ticket's fair value (its live win probability when the board prices every open leg) and its stop-loss floor:

- **STOP-LOSS BREACH**: the offer is at or under the floor shown on the ticket (15% to 40% under the stake): cash out to
  keep the rest.
- **CASHOUT ADVISED**: the offer is at least 85% of fair value, or its profit reaches the target.
- **HOLD**: fair value is worth more than the offer.

The in-play watch re-prices placed slips every few seconds when the live board covers them and pages your phone on a
stop-loss, a collapse in the win probability or a profit target. Record the cashout you take with **Record cashout taken**.

### Step 6 — Review the rupee ledger and the 30% TDS

On a win or a cashout above the stake, 30% TDS is withheld on the net winnings (Section 115BBJ's rate, deducted by the
bookmaker under Section 194BA), computed to the paisa:

```
Net winnings = max(0, payout − stake)
TDS          = 30% × net winnings
Credited     = payout − TDS
```

| Ticket | Stake | Payout | Net winnings | TDS | Credited |
| :--- | ---: | ---: | ---: | ---: | ---: |
| Win at 3.00 | ₹1,000 | ₹3,000 | ₹2,000 | ₹600.00 | ₹2,400.00 |
| Loss | ₹1,000 | ₹0 | ₹0 | ₹0.00 | ₹0.00 |
| Cashout at the stake | ₹1,000 | ₹1,000 | ₹0 | ₹0.00 | ₹1,000.00 |
| Cashout above it | ₹1,000 | ₹2,200 | ₹1,200 | ₹360.00 | ₹1,840.00 |

The Vault's TDS certificate is BetDoc's own summary of the year; the official certificate (Form 16A) comes from the
bookmaker, and the 4% cess is settled when the return is filed.

---

## PART III — OPERATOR VERIFICATION PROTOCOL

Tick each item only after verifying it on the running application (the `/manual` page keeps the ticks in your browser
and runs the first check live).

- [ ] **1. System liveness.** `http://localhost:8000/api/v1/health/` answers `postgres: ok` and `redis: ok`; the web app
  opens at `http://localhost:5173`.
- [ ] **2. Morning signal and `/today`.** Send `/today` to the BetDoc Telegram bot (its chat must be configured in Control
  Panel → Sentinel): it answers with today's score, the tier and the measured reasons.
- [ ] **3. Street-smart slip.** Synthesize toward a target: every leg shows its price, fair probability and EV; a target the
  board cannot reach is reported, not padded.
- [ ] **4. Three-ticket split.** Slip A shows "Capital shield on" only when its payout covers the whole session stake; the
  four outcomes add up to 100%.
- [ ] **5. Booking code locks the stake.** Record the tickets, place one at the book, enter its booking code: the Vault's
  liquid cash falls and locked exposure rises by the stake.
- [ ] **6. Cashout advice.** Type the book's offer on a placed ticket: HOLD, CASHOUT ADVISED or STOP-LOSS BREACH, with the
  fair value and the source of its probability.
- [ ] **7. 30% TDS on net winnings.** Record a cashout above the stake: TDS is 30% of (cashout − stake); at or below the
  stake it is ₹0.00. The Vault's ledger shows both rows.
- [ ] **8. `/status` on Telegram.** From a linked chat it lists the real vault (liquid, locked, SHIVAJI's state) beside the
  feeds and the kill switch.

---

## PART IV — WHAT BETDOC DOES NOT PROMISE

- No ticket, split or signal guarantees a win or the return of a stake; probabilities are the market consensus's, and
  legs of different matches are treated as independent.
- Every price comes from the live odds feed. When the feed is off, the board is empty and BetDoc builds nothing rather
  than inventing a slate.
- The morning signal, `/today` and the cashout pages reach your phone only through the Telegram and SMS channels
  configured in the Sentinel.
- BetDoc never logs in to, scrapes or acts on a bookmaker site; the booking code and the cashout amount are the ones you
  type.

---

<div style="border-top: 2px solid #C89B3C; padding-top: 12px;">

**Issued by authority of:** **Amit Ashok Kumar Patnaik**, Chief Quantitative Architect · BetDoc

*Developed for Amit Ashok Kumar Patnaik* · Gazette seal: **STANDARD OPERATING PROCEDURE** · Group 116 · 23 October 2026

</div>
