"""Turns a live page (and its iframes) into a compact snapshot of what can be acted on or read, and
resolves refs back to locators for replay."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Frame, Locator, Page

from artifact.schema import LocatorCandidate, LocatorStrategy, Target

# Walks a document and returns a JSON list of interactive controls plus short text leaves. Names use
# a simplified accessible-name algorithm, not the full W3C spec.
_SNAPSHOT_JS = r"""
() => {
  // Returns {name, source}. `source` says whether the browser would compute this name itself,
  // or whether it is a borrowed guess (a neighboring label) that isn't searchable text.
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
    // Only a button-type input's value is its label. A text field's value is user data
    // (an old address), so it falls through to the row label below.
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes((el.getAttribute('type') || '').toLowerCase())
        && el.value && el.value.trim()) {
      return { name: el.value.trim(), source: 'value' };
    }
    // A <select>'s innerText is its whole option list, identical for every same-shaped select,
    // so skip it and use the row label instead.
    if (!['SELECT', 'INPUT', 'TEXTAREA'].includes(el.tagName)) {
      const text = (el.innerText || '').trim();
      if (text) return { name: text.slice(0, 80), source: 'own_text' };
    }
    // Last resort: legacy forms put the label in the preceding <td> of the same row.
    // Flagged as borrowed, since it is not this element's own text.
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


# Name sources a real browser would also produce, so they are safe to search by. Anything else (like
# 'inferred_label') is a guess borrowed from another element.
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

        # Sort into reading order, not DOM order. Bucketing y avoids sub-pixel jitter, so a label
        # always comes right before its field, like a person scanning the page.
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
