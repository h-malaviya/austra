import { A2uiLitElement } from "@a2ui/lit/v0_9";
import { css, html } from "lit";
import { z } from "zod";

class AustraWorkflowGraph extends A2uiLitElement {
  static properties = { selected: { state: true } };
  static styles = css`
    :host { display: block; min-width: 0; color: var(--theme-ink); }
    ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px; }
    li { display: flex; align-items: center; gap: 8px; min-width: 0; }
    button { white-space: normal; overflow-wrap: anywhere; border: 1px solid var(--theme-border);
      border-radius: 8px; padding: 8px 12px; font: inherit; cursor: pointer;
      color: var(--theme-ink); background: var(--theme-surface); text-align: left; }
    button[aria-pressed="true"] { border-color: var(--theme-accent); background: var(--color-accent-soft); }
    button:focus-visible { outline: 2px solid var(--theme-accent); outline-offset: 2px; }
    .detail { padding: 12px 0 0; overflow-wrap: anywhere; }
    .detail p { margin: 5px 0; }
    @media (max-width: 540px) {
      ul { display: grid; grid-template-columns: minmax(0, 1fr); }
      li { display: grid; gap: 4px; }
      li:last-child .edge { display: none; }
      .edge { width: 1px; height: 10px; margin-left: 18px; background: var(--theme-accent); font-size: 0; }
    }
  `;

  render() {
    const props = this.controller?.props;
    const steps = props?.steps;
    if (!steps?.length) return html``;
    const configured = Math.max(0, steps.findIndex(step => step.key === props.selectedStep));
    const selected = Math.min(this.selected ?? configured, steps.length - 1);
    const current = steps[selected];
    return html`<section aria-label="Workflow graph">
      <ul>${steps.map((step, index) => html`<li>
        <button type="button" aria-pressed=${index === selected} @click=${() => {
          this.selected = index;
          this.dispatchEvent(new CustomEvent("austra-workflow-step", {
            bubbles: true, composed: true, detail: step.key
          }));
        }}>${step.label}</button>
        ${index < steps.length - 1 ? html`<span class="edge" aria-hidden="true">→</span>` : ""}
      </li>`)}</ul>
      <div class="detail" role="status">
        <strong>${current.label}</strong>
        <p>${current.detail}</p>
        ${current.next ? html`<p><strong>Next</strong> ${current.next}</p>` : ""}
      </div>
    </section>`;
  }
}

const tagName = "austra-workflow-graph";
customElements.define(tagName, AustraWorkflowGraph);

export const workflowGraphImplementation = {
  name: "AustraWorkflowGraph",
  tagName,
  schema: z.object({steps: z.array(z.object({
    key: z.string(), label: z.string(), detail: z.string(), next: z.string()
  })).min(1).max(40), selectedStep: z.string().optional()})
};
