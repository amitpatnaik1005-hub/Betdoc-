import type { ReactNode } from "react";

/**
 * Minimal Markdown for research reports: headings, tables, bullet lists, paragraphs, **bold**,
 * _italic_ and `code`. Renders React nodes only (never raw HTML), so report text can't inject markup.
 */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|_[^_]+_|`[^`]+`)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    const token = m[0];
    if (token.startsWith("**")) out.push(<strong key={m.index} className="font-semibold text-slate-900 dark:text-slate-50">{token.slice(2, -2)}</strong>);
    else if (token.startsWith("`")) out.push(<code key={m.index} className="rounded bg-slate-100 px-1 font-mono text-[0.9em] dark:bg-white/10">{token.slice(1, -1)}</code>);
    else out.push(<em key={m.index}>{token.slice(1, -1)}</em>);
    last = m.index! + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (row: string): string[] => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function Markdown({ source }: { source: string }) {
  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("# ")) {
      blocks.push(<h2 key={i} className="text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">{inline(line.slice(2))}</h2>);
      i += 1;
    } else if (line.startsWith("## ")) {
      blocks.push(<h3 key={i} className="mt-2 text-sm font-bold uppercase tracking-[0.12em] text-[var(--accent)]">{inline(line.slice(3))}</h3>);
      i += 1;
    } else if (line.trim().startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
      const [head, , ...body] = rows;
      blocks.push(
        <div key={`t${i}`} className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-slate-900/10 dark:border-white/10">
                {cells(head).map((c, k) => <th key={k} className="px-2 py-1.5 font-semibold text-slate-500 dark:text-slate-400">{inline(c)}</th>)}
              </tr>
            </thead>
            <tbody>
              {body.map((r, k) => (
                <tr key={k} className="border-b border-slate-900/[0.05] dark:border-white/[0.05]">
                  {cells(r).map((c, j) => <td key={j} className="px-2 py-1.5 tabular-nums text-slate-700 dark:text-slate-200">{inline(c)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else if (line.trim().startsWith("- ")) {
      const items: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("- ")) items.push(lines[i++].trim().slice(2));
      blocks.push(
        <ul key={`u${i}`} className="list-disc space-y-1 pl-5 text-sm text-slate-600 dark:text-slate-300">
          {items.map((it, k) => <li key={k}>{inline(it)}</li>)}
        </ul>,
      );
    } else if (line.trim() === "") {
      i += 1;
    } else {
      blocks.push(<p key={i} className="text-sm leading-relaxed text-slate-600 dark:text-slate-300">{inline(line)}</p>);
      i += 1;
    }
  }
  return <div className="flex flex-col gap-3">{blocks}</div>;
}
