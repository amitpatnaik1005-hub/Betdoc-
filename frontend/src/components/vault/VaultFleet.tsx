/**
 * Vault & Fleet (Group 70): bulk-import a markdown credentials file, see every account and provider key
 * the Vault holds (masked; a secret is never sent to the browser), edit an account by hand, and watch
 * the active sports and the Odds API quota they burn.
 */
import { type DragEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { formatAgo, formatINR, formatInt, humanize } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import {
  type AccountPatch,
  type ImportPreview,
  type ImportSource,
  type VaultAccount,
  type VaultProvider,
  maskedIdentity,
  previewImport,
  saveImport,
  toggleAccount,
  toggleProvider,
  updateAccount,
  useVaultAccounts,
  useVaultProviders,
  useVaultStatus,
} from "../../lib/vault";
import {
  Async,
  Button,
  type Column,
  DataTable,
  EmptyState,
  Field,
  KeyValues,
  Meter,
  NumberInput,
  Panel,
  Pill,
  SPRING,
  Segmented,
  TextInput,
  Toggle,
  type Tone,
  num,
} from "../../ui/kit";

const DEFAULT_PATH = "D:\\confidential\\API Keys & TARGET URL (Read me).md";
const MAX_BYTES = 1_000_000;
const INVALIDATE = ["vault", "fleet"];
type Mode = "file" | "text" | "path";

const verificationTone = (status: string): Tone =>
  ({ OK: "good", FAILED: "critical", UNSUPPORTED: "info", UNVERIFIED: "neutral" } as Record<string, Tone>)[status] ?? "neutral";

const money = (value: string | null, currency: string): string => {
  if (value === null) return "—";
  const n = Number(value);
  return currency === "INR" ? formatINR(n) : `${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })} ${currency}`;
};

const CurrencyBadge = ({ code }: { code: string }) => (
  <span className="inline-flex items-center rounded-md bg-stone-100 px-1.5 py-0.5 font-mono text-[11px] font-semibold tracking-wide text-stone-600 dark:bg-stone-800 dark:text-stone-300">{code}</span>
);

const SecretIcons = ({ a }: { a: VaultAccount }) => {
  const items: [boolean, string, string][] = [
    [a.has_password, "password", "Password"],
    [a.has_api_key, "key", "API key"],
    [a.has_token, "token", "Token"],
    [a.has_2fa, "phonelink_lock", "2FA seed"],
    [Boolean(a.target_host), "link", a.target_host ?? "Target URL"],
  ];
  return (
    <span className="inline-flex items-center gap-1">
      {items.map(([on, icon, title]) => (
        <span key={icon} title={on ? title : `no ${title.toLowerCase()}`} className={`material-symbols-outlined text-[16px] ${on ? "text-stone-600 dark:text-stone-300" : "text-stone-300 dark:text-stone-700"}`}>
          {icon}
        </span>
      ))}
    </span>
  );
};

// --------------------------------------------------------------------------- bulk importer
const BulkImporter = ({ pathImports }: { pathImports: boolean }) => {
  const [mode, setMode] = useState<Mode>("file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [path, setPath] = useState(DEFAULT_PATH);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  const source = (): ImportSource | null => {
    if (mode === "file") return file ? { kind: "file", file } : null;
    if (mode === "text") return text.trim() ? { kind: "text", text } : null;
    return path.trim() ? { kind: "path", path: path.trim() } : null;
  };

  const accept = (picked: File | undefined) => {
    if (!picked) return;
    if (!/\.(md|markdown|txt)$/i.test(picked.name)) return void runMutation(() => Promise.reject(new Error("only .md, .markdown or .txt files")), { errorTitle: "Not a credentials file" });
    if (picked.size > MAX_BYTES) return void runMutation(() => Promise.reject(new Error("the file is over 1 MB")), { errorTitle: "File too large" });
    setFile(picked);
    setMode("file");
    setPreview(null);
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    accept(e.dataTransfer.files?.[0]);
  };

  const runPreview = async () => {
    const src = source();
    if (!src) return;
    setBusy("preview");
    const result = await runMutation(() => previewImport(src), { errorTitle: "Preview failed" });
    setBusy(null);
    if (result) setPreview(result);
  };

  const runSave = async (e?: FormEvent) => {
    e?.preventDefault();
    const src = source();
    if (!src) return;
    setBusy("save");
    const result = await runMutation(() => saveImport(src), {
      invalidate: INVALIDATE,
      errorTitle: "Import refused",
      success: (r) => `Encrypted: ${r.report.accounts_created + r.report.accounts_updated} account(s), ${r.report.providers_created + r.report.providers_updated} key(s) changed, ${r.report.accounts_unchanged + r.report.providers_unchanged} unchanged`,
    });
    setBusy(null);
    if (result) {
      setText(""); // pasted secrets leave the page as soon as they are sealed
      setPreview(null);
    }
  };

  const ready = source() !== null;
  return (
    <Panel title="Bulk credential import" icon="upload_file" className="lg:col-span-7" subtitle="AES-256-GCM, bound to each row and field">
      <form onSubmit={runSave} className="flex flex-col gap-4">
        <Segmented<Mode>
          size="sm"
          label="Import source"
          value={mode}
          onChange={(m) => { setMode(m); setPreview(null); }}
          options={[{ value: "file", label: "Drop a file", icon: "upload" }, { value: "text", label: "Paste", icon: "content_paste" }, { value: "path", label: "Local path", icon: "folder_open" }]}
        />
        {mode === "file" && (
          <div
            role="button"
            tabIndex={0}
            onClick={() => picker.current?.click()}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && picker.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-6 py-10 text-center transition-colors ${dragging ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]" : "border-stone-200 hover:border-stone-300 dark:border-stone-700 dark:hover:border-stone-600"}`}
          >
            <span className="material-symbols-outlined text-[32px] text-stone-400">{file ? "description" : "cloud_upload"}</span>
            <p className="text-sm font-medium text-stone-700 dark:text-stone-200">{file ? file.name : "Drop the credentials markdown here, or click to choose"}</p>
            <p className="text-[11px] text-stone-400">{file ? `${formatInt(file.size)} bytes · read on the server, never stored as a file` : ".md, .markdown or .txt, up to 1 MB"}</p>
            <input ref={picker} type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" className="hidden" onChange={(e) => accept(e.target.files?.[0])} />
          </div>
        )}
        {mode === "text" && (
          <Field label="Markdown" hint="Cleared from this page once it is encrypted.">
            <textarea
              value={text}
              onChange={(e) => { setText(e.target.value); setPreview(null); }}
              rows={9}
              spellCheck={false}
              autoComplete="off"
              placeholder={"**1.) Odds API Key :- …**\n**5.) Target URL (Parimatch) :- https://…**"}
              className="w-full rounded-2xl bg-stone-100/80 px-4 py-3 font-mono text-[12px] leading-relaxed text-stone-800 focus:bg-white focus:outline-none dark:bg-stone-800/70 dark:text-stone-100"
            />
          </Field>
        )}
        {mode === "path" && (
          <Field label="Server-side path" hint={pathImports ? "Only files under VAULT_IMPORT_ALLOWED_DIRS (D:\\confidential and the project root by default)." : "Path imports are off: VAULT_IMPORT_ALLOWED_DIRS is empty."}>
            <TextInput value={path} onChange={(e) => { setPath(e.target.value); setPreview(null); }} className="font-mono" disabled={!pathImports} />
          </Field>
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="button" icon="preview" busy={busy === "preview"} disabled={!ready || busy !== null} onClick={() => void runPreview()}>Preview parse</Button>
          <Button type="submit" variant="primary" icon="lock" busy={busy === "save"} disabled={!ready || busy !== null}>Encrypt &amp; save to Vault</Button>
        </div>
        {preview && <PreviewResult preview={preview} />}
      </form>
    </Panel>
  );
};

const PreviewResult = ({ preview }: { preview: ImportPreview }) => (
  <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={SPRING} className="flex flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
    <KeyValues
      items={[
        { label: "Accounts", value: preview.accounts_found },
        { label: "Provider keys", value: preview.providers_found },
        { label: "Sports", value: preview.sports_found },
        { label: "Open APIs (not stored)", value: preview.open_endpoints.length },
        ...(preview.changes
          ? [
              { label: "Would create", value: `${preview.changes.accounts_created} accounts · ${preview.changes.providers_created} keys` },
              { label: "Would update", value: `${preview.changes.accounts_updated} accounts · ${preview.changes.providers_updated} keys` },
            ]
          : []),
      ]}
    />
    {preview.accounts.length > 0 && (
      <ul className="flex flex-wrap gap-1.5">
        {preview.accounts.map((a) => (
          <li key={`${a.line}-${a.bookmaker}`}><Pill tone={a.url_only ? "info" : "accent"} icon={a.url_only ? "link" : "person"}>{a.bookmaker_name} · {a.username_hint ?? a.target_host ?? "key"} · {a.currency}</Pill></li>
        ))}
      </ul>
    )}
    {preview.providers.length > 0 && (
      <ul className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
        {preview.providers.map((p) => (
          <li key={`${p.line}-${p.provider}`}><Pill tone={p.generic ? "neutral" : "good"} icon="key">{p.provider_name} <span className="font-mono">{p.key_hint}</span></Pill></li>
        ))}
      </ul>
    )}
    {preview.syntax_warnings.length > 0 && (
      <details className="text-[12px] text-amber-700 dark:text-amber-300">
        <summary className="cursor-pointer">{preview.syntax_warnings.length} note(s): line numbers and fields only, never a value</summary>
        <ul className="mt-2 list-disc pl-5">{preview.syntax_warnings.map((w) => <li key={w}>{w}</li>)}</ul>
      </details>
    )}
  </motion.div>
);

// --------------------------------------------------------------------------- sports + quota
const SportsQuota = () => {
  const status = useVaultStatus();
  return (
    <Panel title="Active sports & Odds API quota" icon="sports_score" className="lg:col-span-5" updatedAt={status.updatedAt}>
      <Async resource={status} skeletonRows={4}>
        {(s) => {
          const q = s.quota;
          const used = q.limit ? (q.used ?? q.limit - (q.remaining ?? q.limit)) / q.limit : q.fraction !== null ? 1 - q.fraction : null;
          const tone = used === null ? "accent" : used > 0.9 ? "critical" : used > 0.7 ? "warning" : "good";
          return (
            <div className="flex flex-col gap-5">
              <div className="flex flex-wrap gap-1.5">
                {s.sports.length === 0 && <span className="text-sm text-stone-400">No sport is polled.</span>}
                {s.sports.map((sp) => (
                  <span key={sp.key} title={`${sp.markets} · ${sp.credits_per_call} credit(s)/call${sp.offered === false ? " · not offered by The Odds API" : ""}`}>
                    <Pill tone={!sp.polling ? "neutral" : sp.offered === false ? "warning" : "good"} icon={sp.source === "vault" ? "lock" : "settings"}>
                      <span className="font-mono">{sp.key}</span>
                    </Pill>
                  </span>
                ))}
              </div>
              <div className="flex flex-col gap-2">
                <div className="flex items-baseline justify-between text-sm">
                  <span className="text-stone-500 dark:text-stone-400">Quota consumed</span>
                  <span className="font-mono tabular-nums text-stone-800 dark:text-stone-200">
                    {q.used !== null ? formatInt(q.used) : "—"} / {q.limit !== null ? formatInt(q.limit) : "—"}
                  </span>
                </div>
                <Meter value={used ?? 0} tone={tone} label="Odds API quota consumed" />
                <KeyValues
                  items={[
                    { label: "Remaining", value: q.remaining !== null ? formatInt(q.remaining) : "unknown until the next poll" },
                    { label: "Burn", value: `${q.credits_per_hour} credits/h` },
                    { label: "Runway", value: q.hours_left !== null ? `${q.hours_left} h` : "—" },
                    { label: "Floor", value: formatInt(q.floor) },
                    { label: "Quiet hours", value: s.quiet_hours.start ? `${s.quiet_hours.start}–${s.quiet_hours.end}${s.quiet_hours.active ? " (now)" : ""}` : "off" },
                    { label: "Account routing", value: s.account_routing ? "on" : "off" },
                  ]}
                />
              </div>
            </div>
          );
        }}
      </Async>
    </Panel>
  );
};

// --------------------------------------------------------------------------- accounts matrix
const AccountsMatrix = ({ onEdit }: { onEdit: (a: VaultAccount) => void }) => {
  const accounts = useVaultAccounts();
  const columns: Column<VaultAccount>[] = [
    { key: "book", header: "Bookmaker", render: (a) => <div className="flex flex-col"><span className="font-medium text-stone-800 dark:text-stone-100">{a.bookmaker_name}</span><span className="text-[11px] text-stone-400">{a.label} · P{a.priority}</span></div> },
    { key: "identity", header: "Identity", render: (a) => <span className="font-mono text-[12px]">{maskedIdentity(a)}</span> },
    { key: "secrets", header: "Secrets", render: (a) => <SecretIcons a={a} /> },
    { key: "ccy", header: "Ccy", render: (a) => <CurrencyBadge code={a.currency} /> },
    { key: "balance", header: "Balance", align: "right", render: (a) => money(a.balance, a.currency) },
    { key: "free", header: "Free", align: "right", render: (a) => <span title={`reserved ${money(a.reserved, a.currency)} on ${a.open_holds} order(s)`}>{money(a.free_funds, a.currency)}</span> },
    { key: "status", header: "Status", render: (a) => <Pill tone={a.is_active ? "good" : "neutral"} icon={a.is_active ? "check_circle" : "pause_circle"}>{a.is_active ? "Active" : "Disabled"}</Pill> },
    { key: "verify", header: "Verified", render: (a) => <span title={a.verification_detail ?? ""}><Pill tone={verificationTone(a.verification_status)}>{humanize(a.verification_status)}</Pill></span> },
    {
      key: "actions", header: "", align: "center",
      render: (a) => (
        <span className="inline-flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <Toggle label={`${a.label} active`} checked={a.is_active} onChange={(v) => void runMutation(() => toggleAccount(a.id, v), { invalidate: INVALIDATE, success: `${a.label} ${v ? "enabled" : "disabled"}` })} />
          <Button size="sm" variant="ghost" icon="edit" onClick={() => onEdit(a)}>Edit</Button>
        </span>
      ),
    },
  ];
  return (
    <Panel title="Accounts" icon="account_balance_wallet" className="lg:col-span-12" updatedAt={accounts.updatedAt} subtitle="secrets stay sealed: hints only">
      <Async resource={accounts} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="account_balance_wallet" title="No bookmaker account yet" detail="Import the credentials file above, or add one by hand from the Vault API." />}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(a) => a.id} onRowClick={onEdit} rowTitle="Edit account" dense />}
      </Async>
    </Panel>
  );
};

const ProvidersMatrix = () => {
  const providers = useVaultProviders();
  const columns: Column<VaultProvider>[] = [
    { key: "provider", header: "Provider", render: (p) => <div className="flex flex-col"><span className="font-medium text-stone-800 dark:text-stone-100">{p.provider_name}</span><span className="font-mono text-[11px] text-stone-400">{p.provider_id}</span></div> },
    { key: "label", header: "Label", render: (p) => p.label },
    { key: "key", header: "Key", render: (p) => <span className="font-mono text-[12px]">{p.key_hint ?? "…"}{p.has_secret ? " + secret" : ""}</span> },
    { key: "kind", header: "Catalog", render: (p) => <Pill tone={p.generic ? "neutral" : "info"}>{p.generic ? "Generic" : "Known"}</Pill> },
    { key: "fleet", header: "Fleet", render: (p) => (p.linked_source_id ? <Pill tone="good" icon="hub">{p.linked_source_id}</Pill> : <span className="text-stone-400">—</span>) },
    { key: "status", header: "Status", render: (p) => <Pill tone={p.is_active ? "good" : "neutral"}>{p.is_active ? "Active" : "Disabled"}</Pill> },
    { key: "added", header: "Added", render: (p) => <span className="text-[12px] text-stone-500">{formatAgo(p.created_at)}</span> },
    { key: "toggle", header: "", align: "center", render: (p) => <Toggle label={`${p.label} active`} checked={p.is_active} onChange={(v) => void runMutation(() => toggleProvider(p.id, v), { invalidate: INVALIDATE, success: `${p.label} ${v ? "enabled" : "disabled"}` })} /> },
  ];
  return (
    <Panel title="Provider keys" icon="key" className="lg:col-span-12" updatedAt={providers.updatedAt}>
      <Async resource={providers} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="key" title="No provider key yet" />}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(p) => p.id} dense />}
      </Async>
    </Panel>
  );
};

// --------------------------------------------------------------------------- edit modal
const SECRET_FIELDS = [["username", "Login"], ["password", "Password"], ["api_key", "API key"], ["token", "Token"], ["totp_seed", "2FA seed"], ["url", "Target URL"]] as const;

const AccountModal = ({ account, onClose }: { account: VaultAccount | null; onClose: () => void }) => (
  <AnimatePresence>{account && <AccountDialog key={account.id} account={account} onClose={onClose} />}</AnimatePresence>
);

const AccountDialog = ({ account, onClose }: { account: VaultAccount; onClose: () => void }) => {
  const [d, setD] = useState({
    label: account.label, currency: account.currency, priority: String(account.priority),
    balance: account.balance ?? "", stake_cap: account.stake_cap ?? "", is_active: account.is_active,
  });
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const patch: AccountPatch = { label: d.label.trim(), currency: d.currency.trim().toUpperCase(), priority: Math.round(num(d.priority)), is_active: d.is_active };
    patch.balance = d.balance.trim() === "" ? null : num(d.balance);
    patch.stake_cap = d.stake_cap.trim() === "" ? null : num(d.stake_cap);
    const sent = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v.trim() !== ""));
    if (Object.keys(sent).length) patch.secrets = sent;
    setBusy(true);
    const ok = await runMutation(() => updateAccount(account.id, patch), { invalidate: INVALIDATE, success: `${patch.label} saved`, errorTitle: "Account not saved" });
    setBusy(false);
    setSecrets({});
    if (ok) onClose();
  };

  return (
    <motion.div className="fixed inset-0 z-[80] flex items-end justify-center bg-stone-900/30 backdrop-blur-sm sm:items-center sm:p-6 dark:bg-black/50" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
      <motion.form
        onSubmit={save}
        onClick={(e) => e.stopPropagation()}
        initial={{ y: 24, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        exit={{ y: 24, opacity: 0 }}
        transition={SPRING}
        className="flex max-h-[92vh] w-full max-w-3xl flex-col overflow-hidden rounded-t-[2rem] bg-[#FBFAF6] shadow-soft-lg sm:rounded-[2rem] dark:bg-stone-900 dark:ring-1 dark:ring-white/[0.06]"
      >
        <header className="flex items-start justify-between gap-4 px-6 pb-4 pt-7 sm:px-10">
          <div>
            <h2 className="font-display text-xl font-semibold text-stone-900 dark:text-stone-50">{account.bookmaker_name} · {account.label}</h2>
            <p className="mt-1 text-sm text-stone-500 dark:text-stone-400"><span className="font-mono">{maskedIdentity(account)}</span> · {account.adapter} · {account.target_host ?? "no target URL"}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="grid size-9 shrink-0 place-items-center rounded-full text-stone-400 transition-colors hover:bg-stone-200/60 hover:text-stone-700 dark:hover:bg-white/10">
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </header>
        <div className="grid flex-1 grid-cols-1 gap-x-6 gap-y-1 overflow-y-auto px-6 pb-4 sm:grid-cols-2 sm:px-10">
          <Field label="Label"><TextInput value={d.label} onChange={(e) => setD({ ...d, label: e.target.value })} maxLength={128} /></Field>
          <Field label="Currency"><TextInput value={d.currency} onChange={(e) => setD({ ...d, currency: e.target.value })} maxLength={8} className="font-mono uppercase" /></Field>
          <Field label="Priority" hint="1 = this bookmaker's primary account."><NumberInput min="1" max="1000" value={d.priority} onChange={(e) => setD({ ...d, priority: e.target.value })} /></Field>
          <Field label={`Balance (${d.currency || "—"})`} hint="As you last saw it at the book. Empty: not funds-limited.">
            <NumberInput min="0" step="0.01" value={d.balance} onChange={(e) => setD({ ...d, balance: e.target.value })} />
          </Field>
          <Field label={`Stake cap (${d.currency || "—"})`} hint="Your own ceiling per order on this account."><NumberInput min="0" step="0.01" value={d.stake_cap} onChange={(e) => setD({ ...d, stake_cap: e.target.value })} /></Field>
          <div className="flex items-center justify-between gap-3 self-center rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]">
            <span className="text-sm font-medium text-stone-700 dark:text-stone-200">Active</span>
            <Toggle label="Active" checked={d.is_active} onChange={(v) => setD({ ...d, is_active: v })} />
          </div>
          <p className="pt-3 text-xs font-semibold uppercase tracking-wide text-stone-400 sm:col-span-2">Replace a secret (write-only, never shown)</p>
          {SECRET_FIELDS.map(([key, label]) => (
            <Field key={key} label={label}>
              <TextInput type={key === "url" ? "url" : "password"} autoComplete="off" value={secrets[key] ?? ""} placeholder="unchanged" onChange={(e) => setSecrets({ ...secrets, [key]: e.target.value })} />
            </Field>
          ))}
        </div>
        <footer className="flex justify-end gap-2 px-6 pb-7 pt-2 sm:px-10">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" icon="lock" busy={busy}>Seal &amp; save</Button>
        </footer>
      </motion.form>
    </motion.div>
  );
};

// --------------------------------------------------------------------------- the tab
export const VaultFleet = () => {
  const status = useVaultStatus();
  const [editing, setEditing] = useState<VaultAccount | null>(null);
  return (
    <>
      <BulkImporter pathImports={status.data?.path_imports ?? true} />
      <SportsQuota />
      <AccountsMatrix onEdit={setEditing} />
      <ProvidersMatrix />
      <AccountModal account={editing} onClose={() => setEditing(null)} />
    </>
  );
};
