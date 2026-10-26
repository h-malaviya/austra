function markdownCells(line) {
  const cells = [];
  let cell = "";
  for (let index = 0; index < line.length; index += 1) {
    if (line[index] === "\\" && line[index + 1] === "|") {
      cell += "|";
      index += 1;
    } else if (line[index] === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += line[index];
    }
  }
  cells.push(cell.trim());
  if (line.trim().startsWith("|")) cells.shift();
  if (line.trim().endsWith("|")) cells.pop();
  return cells;
}

function markdownTableAt(lines, index) {
  if (index + 1 >= lines.length || !lines[index].includes("|")) return false;
  const headings = markdownCells(lines[index]);
  const separators = markdownCells(lines[index + 1]);
  return headings.length > 1 && headings.length === separators.length
    && separators.every(cell => /^:?-{3,}:?$/.test(cell));
}

function markdownInline(parent, source) {
  const token = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|~~([^~]+)~~|\*([^*]+)\*|_([^_]+)_/g;
  let last = 0;
  for (const match of source.matchAll(token)) {
    parent.appendChild(document.createTextNode(source.slice(last, match.index)));
    let element;
    if (match[1]) {
      element = document.createElement("a");
      element.textContent = match[1];
      element.href = match[2];
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    } else {
      const tag = match[3] ? "code" : (match[4] || match[5]) ? "strong" : match[6] ? "del" : "em";
      element = document.createElement(tag);
      element.textContent = match[3] || match[4] || match[5] || match[6] || match[7] || match[8];
    }
    parent.appendChild(element);
    last = match.index + match[0].length;
  }
  parent.appendChild(document.createTextNode(source.slice(last)));
}

function renderMarkdown(parent, source) {
  const container = document.createElement("div");
  container.className = "markdown-content";
  parent.appendChild(container);
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (/^\s*```/.test(line)) {
      const code = document.createElement("code");
      const pre = document.createElement("pre");
      index += 1;
      const content = [];
      while (index < lines.length && !/^\s*```/.test(lines[index])) content.push(lines[index++]);
      if (index < lines.length) index += 1;
      code.textContent = content.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue;
    }
    if (markdownTableAt(lines, index)) {
      const scroll = document.createElement("div");
      scroll.className = "markdown-table-scroll";
      scroll.tabIndex = 0;
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", "Scrollable table");
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const header = document.createElement("tr");
      const headings = markdownCells(lines[index]);
      headings.forEach(value => {
        const cell = document.createElement("th");
        cell.scope = "col";
        markdownInline(cell, value);
        header.appendChild(cell);
      });
      head.appendChild(header);
      table.appendChild(head);
      const body = document.createElement("tbody");
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = document.createElement("tr");
        const values = markdownCells(lines[index]);
        headings.forEach((_, column) => {
          const cell = document.createElement("td");
          markdownInline(cell, values[column] || "");
          row.appendChild(cell);
        });
        body.appendChild(row);
        index += 1;
      }
      table.appendChild(body);
      scroll.appendChild(table);
      container.appendChild(scroll);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      markdownInline(element, heading[2]);
      container.appendChild(element);
      index += 1;
      continue;
    }
    const list = line.match(/^\s*(?:[-*+]\s+|\d+\.\s+)/);
    if (list) {
      const ordered = /^\s*\d+\./.test(line);
      const element = document.createElement(ordered ? "ol" : "ul");
      while (index < lines.length && /^\s*(?:[-*+]\s+|\d+\.\s+)/.test(lines[index])) {
        const item = document.createElement("li");
        markdownInline(item, lines[index].replace(/^\s*(?:[-*+]\s+|\d+\.\s+)/, ""));
        element.appendChild(item);
        index += 1;
      }
      container.appendChild(element);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        const paragraph = document.createElement("p");
        markdownInline(paragraph, lines[index].replace(/^>\s?/, ""));
        quote.appendChild(paragraph);
        index += 1;
      }
      container.appendChild(quote);
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()
      && !markdownTableAt(lines, index)
      && !/^(?:#{1,6}\s|\s*```|\s*[-*+]\s|\s*\d+\.\s|>\s?)/.test(lines[index])) {
      paragraph.push(lines[index++]);
    }
    if (paragraph.length) {
      const element = document.createElement("p");
      markdownInline(element, paragraph.join("\n"));
      container.appendChild(element);
    } else {
      index += 1;
    }
  }
  return container;
}
