import { A2uiLitElement } from "@a2ui/lit/v0_9";
import { css, html } from "lit";
import { z } from "zod";

class AustraWorkflowInput extends A2uiLitElement {
  static properties = { pending: { state: true } };
  static styles = css`
    :host { display: block; min-width: 0; }
    form, label { display: grid; gap: 8px; }
    form { gap: 13px; }
    label { color: var(--theme-ink); }
    input, select, textarea, button {
      box-sizing: border-box; min-width: 0; font: inherit;
      border: 1px solid var(--theme-border); border-radius: 7px;
      padding: 8px 11px; color: var(--theme-ink); background: var(--theme-surface);
    }
    input, select, textarea { width: 100%; }
    textarea { min-height: 96px; resize: vertical; }
    button { width: fit-content; cursor: pointer; background: var(--color-accent-soft); }
    :is(input, select, textarea, button):focus-visible {
      outline: 2px solid var(--theme-accent); outline-offset: 2px;
    }
  `;

  render() {
    const props = this.controller?.props;
    if (!props) return html``;
    return html`<form aria-label=${props.title} @submit=${event => this.submit(event)}>
      ${props.fields.map(field => html`<label>${field.label}
        ${field.options
          ? html`<select name=${field.name} required ?disabled=${this.pending}>
            <option value="">Choose a record</option>
            ${field.options.map(option => html`<option value=${option.value}
              ?selected=${option.value === field.value}>${option.label}</option>`)}
          </select>`
          : field.type === "textarea"
            ? html`<textarea name=${field.name} .value=${field.value} required ?disabled=${this.pending}></textarea>`
            : html`<input name=${field.name} type=${field.type} .value=${field.value}
                required ?disabled=${this.pending}>`}
      </label>`)}
      <button type="submit" ?disabled=${this.pending}>${props.label}</button>
    </form>`;
  }

  submit(event) {
    event.preventDefault();
    if (this.pending) return;
    const values = Object.fromEntries(new FormData(event.currentTarget));
    this.pending = true;
    this.dispatchEvent(new CustomEvent("austra-workflow-submit", {
      bubbles: true, composed: true, detail: { values, done: () => { this.pending = false; } }
    }));
  }
}

const tagName = "austra-workflow-input";
customElements.define(tagName, AustraWorkflowInput);

export const workflowInputImplementation = {
  name: "AustraWorkflowInput",
  tagName,
  schema: z.object({
    title: z.string(), label: z.string(),
    fields: z.array(z.object({
      name: z.string(), label: z.string(), type: z.enum(["text", "email", "textarea"]),
      value: z.string(), options: z.array(z.object({value: z.string(), label: z.string()})).optional()
    })).max(40)
  })
};
