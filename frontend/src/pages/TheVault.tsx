import { motion, AnimatePresence } from 'framer-motion';

// ---------------------------------------------------------------------------
// PHYSICS
// ---------------------------------------------------------------------------
const SPRING = { type: 'spring', stiffness: 350, damping: 30 } as const;

const CARD_VARIANTS = {
  hidden: { opacity: 0, y: 18 },
  show:   { opacity: 1, y: 0, transition: SPRING },
};

const GRID_VARIANTS = {
  hidden: { opacity: 0 },
  show:   { opacity: 1, transition: { staggerChildren: 0.1 } },
};

const ROW_VARIANTS = {
  hidden: { opacity: 0, x: -12 },
  show:   { opacity: 1, x: 0, transition: SPRING },
};

// ---------------------------------------------------------------------------
// INTERFACES
// ---------------------------------------------------------------------------
interface LedgerEntry {
  id: string;
  timestamp: string;
  hash: string;
  description: string;
  type: 'CREDIT' | 'DEBIT' | 'FEE';
  amount: string;
  status: 'cleared' | 'pending';
}

interface TreasuryAsset {
  id: string;
  name: string;
  allocation: number;
  value: string;
  colorHex: string;
}

// ---------------------------------------------------------------------------
// STATIC DATA
// ---------------------------------------------------------------------------
const LEDGER_ENTRIES: LedgerEntry[] = [
  {
    id: 'led-001',
    timestamp: '24 Sep · 14:32:07',
    hash: '0x4F9A...B21C',
    description: 'Betfair Winnings — CSK ML',
    type: 'CREDIT',
    amount: '+₹24,125',
    status: 'cleared',
  },
  {
    id: 'led-002',
    timestamp: '24 Sep · 13:18:44',
    hash: '0xA3D1...F08E',
    description: 'Stake Debit — RCB Lay Hedge',
    type: 'DEBIT',
    amount: '-₹8,000',
    status: 'cleared',
  },
  {
    id: 'led-003',
    timestamp: '24 Sep · 12:05:31',
    hash: '0x7C2B...3A9F',
    description: 'Exchange Commission Fee',
    type: 'FEE',
    amount: '-₹482',
    status: 'cleared',
  },
  {
    id: 'led-004',
    timestamp: '23 Sep · 19:47:12',
    hash: '0xE81F...C44D',
    description: 'Betfair Winnings — IND vs AUS',
    type: 'CREDIT',
    amount: '+₹34,400',
    status: 'cleared',
  },
  {
    id: 'led-005',
    timestamp: '23 Sep · 18:22:09',
    hash: '0x29BC...7E1A',
    description: 'Stake Debit — SRH Arb Leg A',
    type: 'DEBIT',
    amount: '-₹5,000',
    status: 'pending',
  },
  {
    id: 'led-006',
    timestamp: '22 Sep · 21:03:55',
    hash: '0x6D4E...A302',
    description: 'Wallet Rebalance — Cold Storage',
    type: 'CREDIT',
    amount: '+₹1,00,000',
    status: 'cleared',
  },
];

const TREASURY_ASSETS: TreasuryAsset[] = [
  {
    id: 'asset-001',
    name: 'Cold Storage',
    allocation: 58,
    value: '₹2,90,000',
    colorHex: '#C89B3C',
  },
  {
    id: 'asset-002',
    name: 'Exchange Hot Wallets',
    allocation: 30,
    value: '₹1,50,000',
    colorHex: '#10B981',
  },
  {
    id: 'asset-003',
    name: 'Liquidity Pool',
    allocation: 12,
    value: '₹60,000',
    colorHex: '#3B82F6',
  },
];

// ---------------------------------------------------------------------------
// STYLE MAPS
// ---------------------------------------------------------------------------
const ENTRY_TYPE_CONFIG: Record<
  LedgerEntry['type'],
  { color: string; icon: string }
> = {
  CREDIT: { color: '#10B981', icon: 'arrow_downward' },
  DEBIT:  { color: '#EF4444', icon: 'arrow_upward'   },
  FEE:    { color: '#EF4444', icon: 'percent'         },
};

const STATUS_CONFIG: Record<
  LedgerEntry['status'],
  { color: string; label: string }
> = {
  cleared: { color: '#10B981', label: 'CLEARED' },
  pending: { color: '#F59E0B', label: 'PENDING' },
};

// ---------------------------------------------------------------------------
// UTILITY: SECTION LABEL
// ---------------------------------------------------------------------------
const SectionLabel = ({ icon, label }: { icon: string; label: string }) => (
  <div className="flex items-center gap-2">
    <span
      className="material-symbols-outlined text-base"
      style={{ color: '#C89B3C' }}
    >
      {icon}
    </span>
    <span
      className="text-xs font-bold uppercase tracking-[0.15em]"
      style={{ color: '#C89B3C' }}
    >
      {label}
    </span>
  </div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TYPEWRITER LINE
// ---------------------------------------------------------------------------
const TypewriterLine = ({ text }: { text: string }) => {
  const words = text.split(' ');
  return (
    <h2 className="max-w-2xl text-xl font-bold tracking-tight text-slate-100 lg:text-2xl">
      {words.map((word, i) => (
        <motion.span
          key={i}
          className="inline-block mr-[0.3em]"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ ...SPRING, delay: 0.04 * i }}
        >
          {word}
        </motion.span>
      ))}
    </h2>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TODAR MAL CONSOLE
// ---------------------------------------------------------------------------
const TodarMalConsole = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-12 relative overflow-hidden rounded-2xl p-8"
    style={{
      background: 'rgba(255,255,255,0.04)',
      backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)',
    }}
  >
    {/* Cryptographic Vault Lock SVG Background */}
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute -right-16 -top-16 h-[400px] w-[400px]"
      viewBox="0 0 400 400"
      fill="none"
      style={{ opacity: 0.15 }}
    >
      {/* Outer gear ring — clockwise */}
      <motion.circle
        cx="200" cy="200" r="180"
        stroke="#C89B3C"
        strokeWidth="2"
        strokeDasharray="8 12"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 30, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />
      {/* Mid gear ring — counter-clockwise */}
      <motion.circle
        cx="200" cy="200" r="130"
        stroke="#10B981"
        strokeWidth="1.5"
        strokeDasharray="6 10"
        fill="none"
        animate={{ rotate: -360 }}
        transition={{ duration: 20, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />
      {/* Inner ring — clockwise, faster */}
      <motion.circle
        cx="200" cy="200" r="80"
        stroke="#C89B3C"
        strokeWidth="1"
        strokeDasharray="4 8"
        fill="none"
        animate={{ rotate: 360 }}
        transition={{ duration: 12, repeat: Infinity, ease: 'linear' }}
        style={{ transformOrigin: '200px 200px' }}
      />
      {/* Bolt markers on outer ring */}
      {[0, 45, 90, 135, 180, 225, 270, 315].map((deg, i) => {
        const rad = (deg * Math.PI) / 180;
        const cx = 200 + 165 * Math.cos(rad);
        const cy = 200 + 165 * Math.sin(rad);
        return (
          <motion.circle
            key={i}
            cx={cx} cy={cy} r={5}
            fill="#C89B3C"
            fillOpacity="0.5"
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 2.5, repeat: Infinity, delay: i * 0.3 }}
          />
        );
      })}
      {/* Center lock core */}
      <circle
        cx="200" cy="200" r="28"
        fill="#C89B3C"
        fillOpacity="0.1"
        stroke="#C89B3C"
        strokeWidth="1.5"
      />
      <motion.circle
        cx="200" cy="200" r="10"
        fill="#C89B3C"
        fillOpacity="0.6"
        animate={{ opacity: [0.6, 1, 0.6], r: [10, 13, 10] }}
        transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
      />
    </svg>

    {/* Content */}
    <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex flex-col gap-3">
        {/* Identity badge */}
        <div className="flex items-center gap-2">
          <motion.span
            className="h-2 w-2 rounded-full"
            style={{ background: '#C89B3C' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          <span
            className="text-xs font-bold uppercase tracking-[0.2em]"
            style={{ color: '#C89B3C' }}
          >
            Treasury General · TODAR MAL
          </span>
        </div>

        <TypewriterLine text="TODAR MAL ACTIVE. Ledger synchronized. All funds cryptographically secured." />

        <p className="max-w-xl text-sm text-slate-400">
          6 ledger entries on-chain. Treasury integrity verified. Cold storage
          holding 58% of mandate. All wallets within compliance thresholds.
        </p>
      </div>

      {/* Action row */}
      <div className="flex flex-wrap gap-3 shrink-0">
        {[
          { label: 'Export Ledger',      icon: 'download',       accent: '#C89B3C' },
          { label: 'Rebalance Wallets',  icon: 'account_balance',accent: '#10B981' },
          { label: 'Audit Trail',        icon: 'policy',         accent: '#3B82F6' },
        ].map(({ label, icon, accent }) => (
          <motion.button
            key={label}
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.97 }}
            transition={SPRING}
            className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold outline-none transition-colors"
            style={{
              background: `${accent}10`,
              border: `1px solid ${accent}30`,
              color: accent,
            }}
          >
            <span className="material-symbols-outlined text-base">{icon}</span>
            {label}
          </motion.button>
        ))}
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: LEDGER ROW
// ---------------------------------------------------------------------------
const LedgerRow = ({ entry }: { entry: LedgerEntry }) => {
  const typeCfg   = ENTRY_TYPE_CONFIG[entry.type];
  const statusCfg = STATUS_CONFIG[entry.status];

  return (
    <motion.div
      variants={ROW_VARIANTS}
      className="grid grid-cols-12 items-center gap-3 py-3.5 px-4 transition-colors"
      style={{ cursor: 'default' }}
      whileHover={{ backgroundColor: 'rgba(255,255,255,0.02)' }}
    >
      {/* Type icon + Description — 4 cols */}
      <div className="col-span-12 sm:col-span-4 flex items-center gap-3">
        <div
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg"
          style={{
            background: `${typeCfg.color}15`,
            border: `1px solid ${typeCfg.color}30`,
          }}
        >
          <span
            className="material-symbols-outlined text-sm"
            style={{ color: typeCfg.color }}
          >
            {typeCfg.icon}
          </span>
        </div>
        <div className="flex flex-col gap-0.5 min-w-0">
          <span className="text-sm font-semibold text-slate-100 leading-tight truncate">
            {entry.description}
          </span>
          <span className="text-[10px] text-slate-600">{entry.timestamp}</span>
        </div>
      </div>

      {/* Hash — 3 cols */}
      <div className="col-span-6 sm:col-span-3">
        <span className="font-mono tabular-nums text-[11px] text-slate-500">
          {entry.hash}
        </span>
      </div>

      {/* Type badge — 2 cols */}
      <div className="col-span-3 sm:col-span-2">
        <span
          className="rounded-md px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider"
          style={{
            background: `${typeCfg.color}15`,
            color: typeCfg.color,
            border: `1px solid ${typeCfg.color}30`,
          }}
        >
          {entry.type}
        </span>
      </div>

      {/* Amount — 2 cols */}
      <div className="col-span-6 sm:col-span-2 text-right sm:text-left">
        <span
          className="font-mono tabular-nums text-sm font-bold"
          style={{ color: typeCfg.color }}
        >
          {entry.amount}
        </span>
      </div>

      {/* Status — 1 col */}
      <div className="col-span-3 sm:col-span-1 flex justify-end sm:justify-start">
        <span
          className="rounded-full px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest"
          style={{
            background: `${statusCfg.color}15`,
            color: statusCfg.color,
            border: `1px solid ${statusCfg.color}30`,
          }}
        >
          {statusCfg.label}
        </span>
      </div>
    </motion.div>
  );
};

// ---------------------------------------------------------------------------
// SUB-COMPONENT: LEDGER MATRIX
// ---------------------------------------------------------------------------
const LedgerMatrix = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-8 flex flex-col gap-4"
  >
    <SectionLabel icon="receipt_long" label="Immutable Ledger" />

    <div
      className="rounded-2xl overflow-hidden"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Column headers */}
      <div className="grid grid-cols-12 gap-3 border-b border-white/5 px-4 py-2.5">
        {[
          { label: 'Description', span: 'col-span-4' },
          { label: 'Tx Hash',     span: 'col-span-3' },
          { label: 'Type',        span: 'col-span-2' },
          { label: 'Amount',      span: 'col-span-2' },
          { label: 'Status',      span: 'col-span-1' },
        ].map(({ label, span }) => (
          <span
            key={label}
            className={`${span} text-[10px] font-bold uppercase tracking-widest text-slate-500`}
          >
            {label}
          </span>
        ))}
      </div>

      {/* Rows with stagger */}
      <motion.div
        className="divide-y divide-white/5"
        variants={GRID_VARIANTS}
        initial="hidden"
        animate="show"
      >
        <AnimatePresence>
          {LEDGER_ENTRIES.map((entry) => (
            <LedgerRow key={entry.id} entry={entry} />
          ))}
        </AnimatePresence>
      </motion.div>

      {/* Footer */}
      <div
        className="flex items-center justify-between border-t border-white/5 px-4 py-3"
        style={{ background: 'rgba(16,185,129,0.04)' }}
      >
        <span className="font-mono text-[11px] text-slate-500">
          6 entries · chain verified
        </span>
        <span
          className="font-mono text-sm font-bold"
          style={{ color: '#10B981' }}
        >
          Net +₹1,46,043
        </span>
      </div>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: ASSET CARD
// ---------------------------------------------------------------------------
const AssetCard = ({
  asset,
  index,
}: {
  asset: TreasuryAsset;
  index: number;
}) => (
  <motion.div
    variants={CARD_VARIANTS}
    className="flex flex-col gap-3 rounded-xl p-4"
    style={{
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.06)',
    }}
    whileHover={{ backgroundColor: 'rgba(255,255,255,0.05)' }}
    transition={SPRING}
  >
    {/* Header */}
    <div className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        <div
          className="h-2.5 w-2.5 rounded-full"
          style={{ background: asset.colorHex }}
        />
        <span className="text-sm font-semibold text-slate-200">{asset.name}</span>
      </div>
      <span
        className="font-mono text-xs font-bold tabular-nums"
        style={{ color: asset.colorHex }}
      >
        {asset.allocation}%
      </span>
    </div>

    {/* Progress bar */}
    <div className="h-1.5 w-full rounded-full overflow-hidden bg-white/5">
      <motion.div
        className="h-full rounded-full"
        style={{ background: asset.colorHex }}
        initial={{ width: '0%' }}
        animate={{ width: `${asset.allocation}%` }}
        transition={{ ...SPRING, delay: 0.2 + index * 0.1 }}
      />
    </div>

    {/* Value */}
    <div className="flex items-center justify-between">
      <span className="text-xs text-slate-500">Fiat Value</span>
      <span className="font-mono tabular-nums text-sm font-bold text-slate-200">
        {asset.value}
      </span>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// SUB-COMPONENT: TREASURY ALLOCATIONS
// ---------------------------------------------------------------------------
const TreasuryAllocations = () => (
  <motion.div
    variants={CARD_VARIANTS}
    className="lg:col-span-4 flex flex-col gap-4"
  >
    <SectionLabel icon="account_balance" label="Treasury Allocations" />

    <div
      className="flex flex-col gap-4 rounded-2xl p-5"
      style={{
        background: 'rgba(255,255,255,0.04)',
        backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)',
      }}
    >
      {/* Panel header */}
      <div className="flex items-center justify-between border-b border-white/5 pb-3">
        <span className="text-xs font-bold text-slate-400">Wallet Distribution</span>
        <span
          className="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest"
          style={{
            background: 'rgba(16,185,129,0.12)',
            color: '#10B981',
            border: '1px solid rgba(16,185,129,0.3)',
          }}
        >
          <motion.span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: '#10B981' }}
            animate={{ opacity: [1, 0.3, 1] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          />
          SECURED
        </span>
      </div>

      {/* Total */}
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-bold tabular-nums text-slate-100">
          ₹5,00,000
        </span>
        <span className="text-xs text-slate-500">total treasury</span>
      </div>

      {/* Asset cards */}
      <div className="flex flex-col gap-3">
        {TREASURY_ASSETS.map((asset, i) => (
          <AssetCard key={asset.id} asset={asset} index={i} />
        ))}
      </div>

      {/* Donut legend */}
      <div className="flex items-center justify-center gap-4 border-t border-white/5 pt-3">
        {TREASURY_ASSETS.map((asset) => (
          <div key={asset.id} className="flex items-center gap-1.5">
            <div
              className="h-2 w-2 rounded-full"
              style={{ background: asset.colorHex }}
            />
            <span className="text-[10px] text-slate-500">{asset.name.split(' ')[0]}</span>
          </div>
        ))}
      </div>

      {/* Footer action */}
      <motion.button
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.97 }}
        transition={SPRING}
        className="flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-bold"
        style={{
          background: 'rgba(200,155,60,0.08)',
          border: '1px solid rgba(200,155,60,0.25)',
          color: '#C89B3C',
        }}
      >
        <span className="material-symbols-outlined text-base">edit</span>
        Rebalance Allocations
      </motion.button>
    </div>
  </motion.div>
);

// ---------------------------------------------------------------------------
// NAMED EXPORT: THE VAULT
// ---------------------------------------------------------------------------
export const TheVault = () => (
  <motion.div
    variants={GRID_VARIANTS}
    initial="hidden"
    animate="show"
    className="grid grid-cols-1 lg:grid-cols-12 gap-6 w-full max-w-7xl mx-auto py-6"
  >
    <TodarMalConsole />
    <LedgerMatrix />
    <TreasuryAllocations />
  </motion.div>
);
