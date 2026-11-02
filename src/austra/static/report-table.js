import { A2uiLitElement } from "@a2ui/lit/v0_9";
import { css, html, nothing } from "lit";
import { z } from "zod";

const tableApi = {
  name: "AustraDataTable",
  schema: z.object({
    title: z.string(),
    headers: z.array(z.string()).min(2).max(30),
    rows: z.array(z.array(z.string())).max(200)
  })
};

const NUMBER = /^\s*[$₹]?\s*[\d,]+(?:\.\d+)?\s*(?:kg|t|%|usd)?\s*$/i;

class AustraDataTable extends A2uiLitElement {
  static styles = css`
    :host { display: block; margin: 14px 0 6px; color: var(--theme-ink); font-family: Inter, "Segoe UI", system-ui, sans-serif; white-space: normal; scrollbar-color: var(--color-scroll-thumb) transparent; }
    .heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 0 8px; }
    h4 { margin: 0; font-size: 14px; line-height: 1.35; }
    .count { flex: none; color: var(--theme-muted); font-size: 11px; }
    .scroll { position: relative; max-width: 100%; max-height: 440px; overflow: auto; border: 1px solid var(--theme-border); border-radius: 10px; background: var(--theme-surface); }
    .scroll { scrollbar-width: thin; scrollbar-color: var(--color-scroll-thumb) transparent; }
    .scroll::-webkit-scrollbar { width: 10px; height: 10px; }
    .scroll::-webkit-scrollbar-thumb { border: 2px solid transparent; border-radius: 10px; background: var(--color-scroll-thumb); background-clip: padding-box; }
    .scroll:focus-visible { outline: 2px solid var(--theme-accent); outline-offset: 2px; }
    table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12px; line-height: 1.4; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--theme-border); text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; z-index: 1; background: var(--color-accent-soft); color: var(--theme-ink); font-weight: 700; white-space: nowrap; }
    td { color: var(--theme-ink); overflow-wrap: anywhere; }
    td:first-child { min-width: 145px; font-weight: 650; }
    td.numeric, th.numeric { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    tbody tr:nth-child(even) { background: var(--color-raised); }
    tbody tr:hover { background: var(--color-accent-soft); }
    tbody tr:last-child td { border-bottom: 0; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (max-width: 540px) { table { min-width: max-content; } th, td { padding: 9px 10px; } }
  `;

  render() {
    const props = this.controller?.props;
    if (!props) return nothing;
    const numeric = props.headers.map((_, index) => index > 0 && props.rows.length > 0 &&
      props.rows.every(row => NUMBER.test(row[index] ?? "")));
    return html`<section aria-label=${props.title}>
      <div class="heading"><h4>${props.title}</h4><span class="count">${props.rows.length} rows</span></div>
      <div class="scroll" role="region" aria-label=${`${props.title} table`} tabindex="0">
        <table><caption class="sr-only">${props.title}</caption><thead><tr>${props.headers.map((header, index) => html`
          <th scope="col" class=${numeric[index] ? "numeric" : ""}>${header}</th>`)}</tr></thead>
          <tbody>${props.rows.map(row => html`<tr>${row.map((value, index) => html`
            <td class=${numeric[index] ? "numeric" : ""}>${value}</td>`)}</tr>`)}</tbody>
        </table>
      </div>
    </section>`;
  }
}

AustraDataTable.tagName = "austra-data-table";
customElements.define(AustraDataTable.tagName, AustraDataTable);

export const reportTableImplementation = { ...tableApi, tagName: AustraDataTable.tagName };
