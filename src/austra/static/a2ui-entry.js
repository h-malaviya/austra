import { MessageProcessor } from "@a2ui/web_core/v0_9";
import { basicCatalog } from "@a2ui/lit/v0_9";
import { Context } from "@a2ui/lit";
import { ContextProvider } from "@lit/context";
import { LitElement, html } from "lit";
import { reportCatalog } from "./report-chart.js";

function renderPlainText(value) {
  const escaped = String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
  return Promise.resolve(escaped.replace(/\n/g, "<br>"));
}

class AustraSurfaceHost extends LitElement {
  static properties = { surface: { attribute: false } };

  constructor() {
    super();
    new ContextProvider(this, { context: Context.markdown, initialValue: renderPlainText });
  }

  createRenderRoot() { return this; }

  render() { return html`<a2ui-surface .surface=${this.surface}></a2ui-surface>`; }
}

customElements.define("austra-surface", AustraSurfaceHost);

window.AustraA2UI = {
  mount(container, description, onAction) {
    const processor = new MessageProcessor([basicCatalog, reportCatalog]);
    processor.processMessages(description.messages);
    const surface = processor.getSurfaces().get(description.id);
    if (!surface) throw new Error("Austra could not display this panel.");
    const subscription = surface.onAction.subscribe(onAction);
    const host = document.createElement("austra-surface");
    host.surface = surface;
    container.appendChild(host);
    return () => { subscription.unsubscribe(); host.remove(); };
  }
};
