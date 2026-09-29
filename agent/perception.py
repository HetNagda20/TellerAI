"""Turns a live Playwright page (main frame + nested iframes) into a compact,
LLM-readable snapshot of "things you can act on or read", and can resolve a
snapshot ref back into a real Playwright Locator.

This is deliberately not Playwright's built-in accessibility snapshot: we
need every perceived element to carry enough information to (a) act on it
now, during discovery, and (b) serialize into ranked LocatorCandidates that
can be re-resolved in a fresh browser session during replay. A raw
accessibility tree gives you (a) but not a clean path to (b).

Name/role computation intentionally mirrors what a sighted operator (or a
screen reader) would infer from an ugly, table-based, no-test-id page:
label associations, placeholders, button values, and finally trimmed inner
text. Never element ids or class names, because the target app does not
reliably have them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Frame, Locator, Page

from artifact.schema import LocatorCandidate, LocatorStrategy, Target

# Walks `document` (or a sub-frame's document) and returns a JSON-serializable
# list of perceived nodes: interactive controls, plus short standalone text
# leaves (useful as read/checkpoint targets). Role/name computation is a
# simplified accessible-name algorithm, not the full W3C spec.
_SNAPSHOT_JS = r"""
() => {
  // Returns {name, source}. `source` tells the caller whether `name` is
  // something the browser's own accessibility engine would compute for this
  // element (safe to search for via role+name, or by its own text) versus a
  // borrowed guess (a neighboring label cell) that happens to describe this
  // element but is NOT this element's text. Searching the page for that
  // text would find the label, not the input. Only 'inferred_label' needs
  // this distinction; every other source is part of the real accname chain.
  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return { name: aria.trim(), source: 'aria' };
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.innerText.trim()) return { name: lbl.innerText.trim(), source: 'label' };
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping.innerText.trim()) return { name: wrapping.innerText.trim(), source: 'label' };
    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return { name: placeholder.trim(), source: 'placeholder' };
    if ('value' in el && el.tagName !== 'SELECT' && el.value && el.value.trim()) {
      return { name: el.value.trim(), source: 'value' };
    }
    // A <select>'s own innerText is its concatenated option list (e.g.
    // "Checking\nSavings") -- identical for every same-shaped select on the
    // page, so it can never distinguish one control from another. Skip it
    // here the same way the value branch above already does, and fall
    // through to the row-based inferred_label branch below, which reads the
    // *distinct* neighboring label text instead. Generic to any legacy
    // table-layout form with more than one same-shaped dropdown, not
    // specific to any one app's field names.
    if (el.tagName !== 'SELECT') {
      const text = (el.innerText || '').trim();
      if (text) return { name: text.slice(0, 80), source: 'own_text' };
    }
    // Last resort: legacy table-layout forms often put the label in the
    // preceding <td> of the same row with no programmatic association at
    // all. A human operator reads it visually; we approximate that, but
    // flag it as borrowed, since it is NOT this element's own text.
    const row = el.closest('tr');
    if (row) {
      const cell = el.closest('td');
      const cells = Array.from(row.cells || []);
      const cellIdx = cell ? cells.indexOf(cell) : -1;
      if (cellIdx > 0) {
        const labelText = (cells[cellIdx - 1].innerText || '').trim();
        if (labelText) return { name: labelText.slice(0, 80), source: 'inferred_label' };
      }
    }
    return { name: '', source: 'none' };
  }

  function roleOf(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input' && (type === 'submit' || type === 'button')) return 'button';
    if (tag === 'input' && type === 'checkbox') return 'checkbox';
    if (tag === 'input' && type === 'radio') return 'radio';
    if (tag === 'input' && (type === '' || type === 'text' || type === 'password' || type === 'email' || type === 'number')) return 'textbox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    return null;
  }

  function cssPath(el) {
    // Short structural path: tag[:nth-of-type] chain, up to 5 ancestors.
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  }

  const nodes = [];
  const interactiveSel = 'a[href], button, input, textarea, select';
  document.querySelectorAll(interactiveSel).forEach(el => {
    if (!isVisible(el)) return;
    const role = roleOf(el);
    if (!role) return;
    const r = el.getBoundingClientRect();
    const { name, source } = accessibleName(el);
    nodes.push({
      role,
      name,
      name_source: source,
      tag: el.tagName.toLowerCase(),
      input_type: (el.getAttribute('type') || '').toLowerCase(),
      css: cssPath(el),
      bbox: { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) },
      interactive: true,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).map(o => ({ value: o.value, label: o.text.trim() }))
        : null,
    });
  });

  // Short standalone text leaves: useful as read_text / checkpoint targets
  // (e.g. a balance amount, a confirmation number, an error message).
  document.querySelectorAll('td, span, div, b, font, p, li, h1, h2, h3').forEach(el => {
    if (!isVisible(el)) return;
    const hasElementChildren = el.children.length > 0;
    if (hasElementChildren) return; // only true leaves, avoid duplicate nested text
    const text = (el.innerText || '').trim();
    if (!text || text.length > 150) return;
    const r = el.getBoundingClientRect();
    nodes.push({
      role: 'text',
      name: text,
      name_source: 'own_text',
      tag: el.tagName.toLowerCase(),
      input_type: '',
      css: cssPath(el),
      bbox: { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) },
      interactive: false,
      options: null,
    });
  });

  // Frames visible in this document (for building frame_chain candidates).
  const frames = Array.from(document.querySelectorAll('iframe')).map((f, i) => ({
    index: i,
    src: f.getAttribute('src') || '',
    css: cssPath(f),
  }));

  return { url: document.location.href, nodes, frames };
}
"""


# Sources the real browser accessible-name computation would also produce,
# safe to search for via get_by_role(name=...) or get_by_text(...). Anything
# else (currently just 'inferred_label') is a borrowed guess: useful to show
# a human/LLM, unsafe to use as a search key since it is another element's text.
_TRUSTWORTHY_NAME_SOURCES = {"aria", "label", "placeholder", "value", "own_text"}


@dataclass
class PerceivedElement:
    ref: str
    frame_index: int
    role: str
    name: str
    name_source: str
    tag: str
    input_type: str
    css: str
    bbox: dict
    interactive: bool
    options: list[dict] | None
    frame_chain_css: list[str] = field(default_factory=list)

    def candidates(self) -> list[LocatorCandidate]:
        out = []
        trustworthy = self.name_source in _TRUSTWORTHY_NAME_SOURCES
        if self.role and self.name and trustworthy:
            out.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.ROLE_NAME,
                    value={"role": self.role, "name": self.name},
                )
            )
        if self.name and trustworthy:
            out.append(LocatorCandidate(strategy=LocatorStrategy.TEXT, value={"text": self.name}))
        out.append(LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": self.css}))
        out.append(
            LocatorCandidate(
                strategy=LocatorStrategy.COORDINATES,
                value={"x": self.bbox["x"], "y": self.bbox["y"]},
            )
        )
        return out

    def to_target(self) -> Target:
        return Target(
            candidates=self.candidates(),
            frame_chain=[
                [LocatorCandidate(strategy=LocatorStrategy.CSS_PATH, value={"css": css})]
                for css in self.frame_chain_css
            ],
        )


@dataclass
class Snapshot:
    elements: list[PerceivedElement]
    main_url: str

    def to_prompt_text(self) -> str:
        lines = [f"Current page: {self.main_url}", "Visible elements:"]
        for el in self.elements:
            extra = ""
            if el.options:
                opts = ", ".join(o["label"] for o in el.options)
                extra = f" options=[{opts}]"
            frame_note = f" (in frame {el.frame_index})" if el.frame_index != 0 else ""
            lines.append(f"  [{el.ref}] {el.role} \"{el.name}\"{extra}{frame_note}")
        return "\n".join(lines)

    def get(self, ref: str) -> PerceivedElement:
        for el in self.elements:
            if el.ref == ref:
                return el
        raise KeyError(f"No perceived element with ref {ref!r}")


def snapshot(page: Page) -> Snapshot:
    """Builds a flat, ref-addressable snapshot of the page and every nested iframe."""
    frames = page.frames  # flat list, main frame first, Playwright resolves nesting order
    elements: list[PerceivedElement] = []

    # frame -> css path of the <iframe> tag that contains it, as seen by its parent
    frame_entry_css: dict[Frame, str] = {}
    for frame in frames:
        parent = frame.parent_frame
        if parent is None:
            continue
        try:
            data = parent.evaluate(_SNAPSHOT_JS)
        except Exception:
            continue
        for f in data["frames"]:
            # match this child frame to its <iframe> tag by src suffix
            if f["src"] and f["src"] in (frame.url or ""):
                frame_entry_css[frame] = f'iframe[src*="{f["src"]}"]'
                break
        else:
            frame_entry_css.setdefault(frame, "iframe")

    for frame_index, frame in enumerate(frames):
        try:
            data = frame.evaluate(_SNAPSHOT_JS)
        except Exception:
            continue

        # build the full chain of iframe CSS selectors from page down to this frame
        chain: list[str] = []
        cur = frame
        while cur.parent_frame is not None:
            chain.insert(0, frame_entry_css.get(cur, "iframe"))
            cur = cur.parent_frame

        # Reading order, not DOM-query order: the JS walker emits every
        # interactive element before any text leaf, which would otherwise put
        # e.g. both "Checking/Savings" dropdowns of a transfer form back to
        # back with their "From Account:"/"To Account:" labels many lines
        # away. Sorting by (row, x), bucketing y so same-row elements do not
        # get separated by sub-pixel jitter, reproduces how a human actually
        # scans a table-laid-out page, so a label always precedes its field.
        nodes_in_reading_order = sorted(
            data["nodes"], key=lambda n: (round(n["bbox"]["y"] / 12), n["bbox"]["x"])
        )

        for i, node in enumerate(nodes_in_reading_order):
            elements.append(
                PerceivedElement(
                    ref=f"f{frame_index}e{i}",
                    frame_index=frame_index,
                    role=node["role"],
                    name=node["name"],
                    name_source=node["name_source"],
                    tag=node["tag"],
                    input_type=node["input_type"],
                    css=node["css"],
                    bbox=node["bbox"],
                    interactive=node["interactive"],
                    options=node["options"],
                    frame_chain_css=chain,
                )
            )

    return Snapshot(elements=elements, main_url=page.url)


def resolve_locator(page: Page, el: PerceivedElement) -> Locator:
    """Resolves a perceived element back to a live Playwright Locator, for discovery-time action."""
    frame = page.frames[el.frame_index]
    if el.role and el.name and el.name_source in _TRUSTWORTHY_NAME_SOURCES:
        loc = frame.get_by_role(el.role, name=el.name, exact=False)
        if loc.count() >= 1:
            return loc.first
    return frame.locator(el.css).first
