import { Catalog } from "@a2ui/web_core/v0_9";
import { A2uiLitElement, basicCatalog } from "@a2ui/lit/v0_9";
import { css, html, nothing } from "lit";
import { z } from "zod";
import { reportTableImplementation } from "./report-table.js";

const chartRow = z.object({
  label: z.string().max(120),
  first: z.string(),
  second: z.string().nullable(),
  first_label: z.string(),
  second_label: z.string(),
  details: z.array(z.object({ label: z.string(), value: z.string() })).max(30).optional()
});

const chartApi = {
  name: "AustraBarChart",
  schema: z.object({
    title: z.string(),
    mode: z.enum(["stacked", "paired", "single"]),
    unit: z.string(),
    rows: z.array(chartRow).max(200)
  })
};

function amount(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function width(value, maximum) {
  return `${maximum > 0 ? Math.min(100, amount(value) / maximum * 100) : 0}%`;
}

function measure(value, unit) {
  return value === null ? "Unknown" : `${value}${unit ? ` ${unit}` : ""}`;
}

class AustraBarChart extends A2uiLitElement {
  static properties = {
    selectedIndex: { state: true },
    hoverIndex: { state: true },
    pinnedIndex: { state: true }
  };
  static styles = css`
    :host { display: block; margin: 12px 0 18px; color: var(--theme-ink); font-family: Inter, "Segoe UI", system-ui, sans-serif; white-space: normal; scrollbar-color: var(--color-scroll-thumb) transparent; }
    .chart { padding: 16px; border: 1px solid var(--theme-border); border-radius: 12px; background: var(--theme-surface); }
    h4 { margin: 0 0 5px; font-size: 14px; color: var(--theme-ink); }
    .legend { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 15px; color: var(--color-ink-soft); font-size: 12px; }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .swatch { width: 10px; height: 10px; border-radius: 3px; background: var(--theme-accent); }
    .swatch.second { background: var(--theme-warning); }
    .rows { display: grid; gap: 5px; }
    .chart-row { position: relative; }
    button { display: grid; grid-template-columns: minmax(110px, 31%) minmax(0, 1fr); gap: 10px; align-items: center; width: 100%; padding: 8px 10px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--theme-ink); font: inherit; text-align: left; cursor: pointer; }
    button:hover, button[aria-pressed="true"] { border-color: var(--theme-border); background: var(--color-accent-soft); }
    button:focus-visible { outline: 2px solid var(--theme-accent); outline-offset: 2px; }
    .label { overflow-wrap: anywhere; font-size: 12px; font-weight: 650; }
    .track { display: flex; height: 17px; border-radius: 4px; background: var(--color-accent-soft); overflow: hidden; }
    .value-track { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; }
    .value { min-width: 52px; color: var(--theme-accent); font-size: 12px; font-weight: 750; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
    .bar { display: block; height: 100%; background: var(--theme-accent); }
    .bar.second { background: var(--theme-warning); }
    .pair { display: grid; gap: 3px; }
    .pair .track { height: 8px; }
    .unknown { color: var(--theme-muted); font-size: 11px; }
    .detail { margin-top: 13px; padding: 11px 12px; border-radius: 8px; background: var(--color-accent-soft); color: var(--theme-ink); font-size: 12px; line-height: 1.5; }
    .detail strong { display: block; margin-bottom: 2px; }
    .tooltip { position: absolute; z-index: 10; top: calc(100% + 5px); right: 8px; width: min(300px, calc(100% - 16px)); padding: 12px 14px; border: 1px solid var(--theme-border); border-radius: 10px; background: var(--theme-surface); box-shadow: 0 12px 30px var(--color-shadow); color: var(--theme-ink); font-size: 12px; }
    .tooltip.above { top: auto; bottom: calc(100% + 5px); }
    .tooltip strong { display: block; margin-bottom: 8px; font-size: 13px; }
    .tooltip dl { display: grid; gap: 5px; margin: 0; }
    .tooltip dl > div { display: flex; justify-content: space-between; gap: 12px; }
    .tooltip dt { color: var(--theme-muted); }
    .tooltip dd { margin: 0; font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; }
    @media (max-width: 540px) { button { grid-template-columns: 1fr; gap: 5px; } }
  `;

  constructor() {
    super();
    this.selectedIndex = 0;
    this.hoverIndex = null;
    this.pinnedIndex = null;
  }

  dismissTooltip(event) {
    if (event.key === "Escape") {
      this.hoverIndex = null;
      this.pinnedIndex = null;
      event.stopPropagation();
    }
  }

  tooltip(row, unit, mode, above) {
    const details = row.details ?? [
      { label: row.first_label, value: measure(row.first, unit) },
      ...(mode === "single" ? [] : [{ label: row.second_label, value: measure(row.second, unit) }])
    ];
    return html`<div id="chart-tooltip" role="tooltip" class=${above ? "tooltip above" : "tooltip"}>
      <strong>${row.label}</strong>
      <dl>${details.map(item => html`<div><dt>${item.label}</dt><dd>${item.value}</dd></div>`)}</dl>
    </div>`;
  }

  render() {
    const props = this.controller?.props;
    if (!props) return nothing;
    const { rows, mode, unit } = props;
    const maximum = Math.max(0, ...rows.map(row => mode === "stacked"
      ? amount(row.first) + amount(row.second)
      : Math.max(amount(row.first), amount(row.second))));
    const selected = rows[Math.min(this.selectedIndex, rows.length - 1)];
    const activeIndex = this.hoverIndex ?? this.pinnedIndex;
    return html`<section class="chart" aria-label=${props.title}>
      <h4>${props.title}</h4>
      <div class="legend"><span><i class="swatch"></i>${rows[0]?.first_label}</span>
        ${mode === "single" ? nothing : html`<span><i class="swatch second"></i>${rows[0]?.second_label}</span>`}</div>
      <div class="rows">${rows.map((row, index) => html`<div class="chart-row"
        @mouseenter=${() => { this.hoverIndex = index; }}
        @mouseleave=${() => { if (this.hoverIndex === index) this.hoverIndex = null; }}>
        <button type="button" aria-pressed=${mode === "single" ? this.pinnedIndex === index : index === this.selectedIndex}
          aria-expanded=${activeIndex === index}
          aria-describedby=${activeIndex === index ? "chart-tooltip" : nothing}
          aria-label=${mode === "single" ? `${row.label}: ${row.first_label} ${row.first} ${unit}` : `${row.label}: ${row.first_label} ${row.first} ${unit}; ${row.second_label} ${row.second ?? "unknown"} ${row.second === null ? "" : unit}`}
          @focus=${() => { this.hoverIndex = index; }}
          @blur=${() => { if (this.pinnedIndex !== index) this.hoverIndex = null; }}
          @keydown=${event => this.dismissTooltip(event)}
          @click=${() => {
            this.selectedIndex = index;
            if (this.pinnedIndex === index) {
              this.pinnedIndex = null;
              this.hoverIndex = null;
            } else {
              this.pinnedIndex = index;
            }
          }}>
          <span class="label">${row.label}</span>
          ${mode === "single"
            ? html`<span class="value-track"><span class="track"><span class="bar" style=${`width:${width(row.first, maximum)}`}></span></span><span class="value">${measure(row.first, unit)}</span></span>`
            : mode === "stacked"
            ? html`<span class="track"><span class="bar" style=${`width:${width(row.first, maximum)}`}></span><span class="bar second" style=${`width:${width(row.second, maximum)}`}></span></span>`
            : html`<span class="pair"><span class="track"><span class="bar" style=${`width:${width(row.first, maximum)}`}></span></span>
                ${row.second === null ? html`<span class="unknown">Available stock unknown</span>` : html`<span class="track"><span class="bar second" style=${`width:${width(row.second, maximum)}`}></span></span>`}</span>`}
        </button>
        ${activeIndex === index ? this.tooltip(row, unit, mode, rows.length > 3 && index >= rows.length - 2) : nothing}
      </div>`)}</div>
      ${selected && mode !== "single" ? html`<div class="detail" role="status"><strong>${selected.label}</strong>
        ${selected.first_label}: ${selected.first} ${unit}${mode === "single" ? nothing : html` · ${selected.second_label}: ${selected.second ?? "Unknown"}${selected.second === null ? "" : ` ${unit}`}`}
      </div>` : nothing}
    </section>`;
  }
}

AustraBarChart.tagName = "austra-bar-chart";
customElements.define(AustraBarChart.tagName, AustraBarChart);

export const reportCatalog = new Catalog(
  "https://austra.local/catalogs/report.json", "0.9",
  [...basicCatalog.components.values(), { ...chartApi, tagName: AustraBarChart.tagName }, reportTableImplementation],
  [...basicCatalog.functions.values()]
);
