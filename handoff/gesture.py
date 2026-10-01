"""Lets a human take control by clicking or typing in the live browser. An injected listener reports
events that happen while we are not mid-dispatch. Main frame only."""

from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

from playwright.sync_api import Page

logger = logging.getLogger(__name__)

_INIT_SCRIPT = r"""
(() => {
  if (window.__pwGestureInstalled) return;
  window.__pwGestureInstalled = true;
  window.__pwActive = false;
  const signal = (e) => {
    if (window.__pwActive) return;
    // Clicking our own UI (Resume, Approve, Deny) is using the control mechanism, not taking
    // over the page. Without this check, clicking Resume would re-trigger detection.
    if (e.target && e.target.closest && e.target.closest('.__pw_ui')) return;
    if (window.__pwHumanSignal) window.__pwHumanSignal();
  };
  document.addEventListener('mousedown', signal, true);
  document.addEventListener('keydown', signal, true);
})();
"""

# Shared look for both the pause and confirm banners: a small, right-aligned
# card with real spacing between sections so a long human-readable message
# has room to actually be read.
_CARD_CSS = (
    "position:fixed;top:16px;right:16px;z-index:2147483647;width:340px;"
    "max-width:calc(100vw - 32px);background:#20232a;color:#f4f4f5;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "font-size:13px;line-height:1.5;border-radius:12px;"
    "box-shadow:0 12px 28px rgba(0,0,0,.45);padding:16px;"
    "display:flex;flex-direction:column;gap:12px;"
)

_SHOW_BANNER_JS = r"""
(({context, showCancel}) => {
  if (document.getElementById('__pw_pause_banner')) return;
  const bar = document.createElement('div');
  bar.id = '__pw_pause_banner';
  bar.className = '__pw_ui';
  bar.style.cssText = __PW_CARD_CSS__;

  const title = document.createElement('div');
  title.style.cssText = 'display:flex;align-items:center;gap:8px;font-weight:700;';
  const dot = document.createElement('span');
  dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#ff5c5c;flex:none;';
  const titleText = document.createElement('span');
  titleText.textContent = 'Automation Paused';
  title.append(dot, titleText);

  // A real end user watching the browser never sees a terminal. textContent,
  // never innerHTML, so an artifact's own description/goal text can never
  // inject markup here.
  const info = document.createElement('div');
  info.id = '__pw_pause_context';
  info.style.cssText = 'color:#c9cdd3;white-space:pre-wrap;max-height:220px;overflow-y:auto;';
  info.textContent = context || 'You are in control of the live session.';

  const note = document.createElement('input');
  note.id = '__pw_note';
  note.placeholder = 'What did you do? (optional)';
  note.style.cssText = 'padding:8px 10px;border-radius:8px;border:1px solid #3a3d44;' +
    'background:#2a2d34;color:#fff;font-size:12.5px;outline:none;';

  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';
  const resume = document.createElement('button');
  resume.id = '__pw_resume';
  resume.textContent = 'Resume Automation';
  resume.style.cssText = 'padding:8px 14px;border:none;border-radius:8px;' +
    'background:#3b82f6;color:#fff;font-weight:600;cursor:pointer;font-size:12.5px;';
  resume.addEventListener('click', () => {
    const val = note.value;
    bar.remove();
    if (window.__pwResumeSignal) window.__pwResumeSignal(val);
  });
  actions.appendChild(resume);

  if (showCancel) {
    const restart = document.createElement('button');
    restart.id = '__pw_restart';
    restart.textContent = 'Restart Automation';
    restart.style.cssText = 'padding:8px 14px;border:1px solid #3a3d44;border-radius:8px;' +
      'background:transparent;color:#c9cdd3;font-weight:600;cursor:pointer;font-size:12.5px;';
    restart.addEventListener('click', () => {
      const val = note.value;
      bar.remove();
      if (window.__pwRestartSignal) window.__pwRestartSignal(val);
    });
    actions.insertBefore(restart, resume);

    const cancel = document.createElement('button');
    cancel.id = '__pw_cancel';
    cancel.textContent = 'End Run';
    cancel.style.cssText = 'padding:8px 14px;border:1px solid #3a3d44;border-radius:8px;' +
      'background:transparent;color:#c9cdd3;font-weight:600;cursor:pointer;font-size:12.5px;';
    cancel.addEventListener('click', () => {
      const val = note.value;
      bar.remove();
      if (window.__pwCancelSignal) window.__pwCancelSignal(val);
    });
    actions.insertBefore(cancel, resume);
  }

  bar.append(title, info, note, actions);
  document.body.appendChild(bar);
})
""".replace("__PW_CARD_CSS__", json.dumps(_CARD_CSS))

_HIDE_BANNER_JS = "document.getElementById('__pw_pause_banner')?.remove();"

# Same idea as the pause banner, for risky actions that need a yes or no. The buttons share the
# __pw_ui class, so the click-ignoring guard covers them.
_SHOW_CONFIRM_JS = r"""
((message) => {
  if (document.getElementById('__pw_confirm_banner')) return;
  const bar = document.createElement('div');
  bar.id = '__pw_confirm_banner';
  bar.className = '__pw_ui';
  bar.style.cssText = __PW_CARD_CSS__;

  const title = document.createElement('div');
  title.style.cssText = 'display:flex;align-items:center;gap:8px;font-weight:700;';
  const dot = document.createElement('span');
  dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#f5a623;flex:none;';
  const titleText = document.createElement('span');
  titleText.textContent = 'Approval Needed';
  title.append(dot, titleText);

  const info = document.createElement('div');
  info.style.cssText = 'color:#c9cdd3;white-space:pre-wrap;max-height:220px;overflow-y:auto;';
  info.textContent = message;

  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';
  const deny = document.createElement('button');
  deny.id = '__pw_deny';
  deny.textContent = 'Deny';
  deny.style.cssText = 'padding:8px 14px;border:1px solid #3a3d44;border-radius:8px;' +
    'background:transparent;color:#c9cdd3;font-weight:600;cursor:pointer;font-size:12.5px;';
  const approve = document.createElement('button');
  approve.id = '__pw_approve';
  approve.textContent = 'Approve';
  approve.style.cssText = 'padding:8px 14px;border:none;border-radius:8px;' +
    'background:#22c55e;color:#fff;font-weight:600;cursor:pointer;font-size:12.5px;';
  actions.append(deny, approve);

  bar.append(title, info, actions);
  document.body.appendChild(bar);
  approve.addEventListener('click', () => { bar.remove(); window.__pwConfirmSignal && window.__pwConfirmSignal(true); });
  deny.addEventListener('click', () => { bar.remove(); window.__pwConfirmSignal && window.__pwConfirmSignal(false); });
})
""".replace("__PW_CARD_CSS__", json.dumps(_CARD_CSS))

_HIDE_CONFIRM_JS = "document.getElementById('__pw_confirm_banner')?.remove();"

# Captures a human's live actions as replayable steps. accessibleName, roleOf and cssPath copy
# agent/perception.py on purpose, so the descriptor is built before a navigation races it.
_CAPTURE_INIT_JS = r"""
(() => {
  if (window.__pwCaptureInstalled) return;
  window.__pwCaptureInstalled = true;
  // A navigation makes a new document, so read the state kept for this tab, not a fresh false.
  try { window.__pwCapturing = sessionStorage.getItem('__pw_capturing') === '1'; } catch (e) { window.__pwCapturing = false; }

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
    // Kept in lockstep with agent/perception.py: only a button-type input's value is its
    // name; a text field's value is user data, so it falls through to the row label.
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes((el.getAttribute('type') || '').toLowerCase())
        && el.value && el.value.trim()) {
      return { name: el.value.trim(), source: 'value' };
    }
    // Kept in lockstep with agent/perception.py: a <select>'s innerText is its
    // concatenated option list, never distinguishing same-shaped selects,
    // skip it and fall through to the row-based label.
    if (!['SELECT', 'INPUT', 'TEXTAREA'].includes(el.tagName)) {
      const text = (el.innerText || '').trim();
      if (text) return { name: text.slice(0, 80), source: 'own_text' };
    }
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
    return null;
  }

  function cssPath(el) {
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  }

  function describeElement(el) {
    const role = roleOf(el);
    if (!role) return null;
    const r = el.getBoundingClientRect();
    const { name, source } = accessibleName(el);
    return {
      role, name, name_source: source,
      css: cssPath(el),
      bbox: { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) },
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).map(o => ({ value: o.value, label: o.text.trim() }))
        : null,
    };
  }

  document.addEventListener('click', (e) => {
    if (!window.__pwCapturing) return;
    if (e.target && e.target.closest && e.target.closest('.__pw_ui')) return;
    const el = e.target.closest(
      'a[href], button, input[type=submit], input[type=button], input[type=checkbox], input[type=radio]'
    );
    if (!el) return;
    const desc = describeElement(el);
    if (desc && window.__pwActionCaptured) window.__pwActionCaptured('click', desc, null);
  }, true);

  document.addEventListener('change', (e) => {
    if (!window.__pwCapturing) return;
    const el = e.target;
    if (el.closest && el.closest('.__pw_ui')) return;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'select') {
      const desc = describeElement(el);
      if (desc && window.__pwActionCaptured) window.__pwActionCaptured('select', desc, el.value);
    } else if (tag === 'input' || tag === 'textarea') {
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'textarea' || ['text', 'password', 'email', 'number', ''].includes(type)) {
        const desc = describeElement(el);
        if (desc && window.__pwActionCaptured) window.__pwActionCaptured('fill', desc, el.value);
      }
    }
  }, true);
})();
"""


class GestureController:
    """One per live page. Exposes the JS/Python bridge once; callbacks are
    re-armed per use so this survives many pause/resume cycles in one run.
    """

    def __init__(self, page: Page):
        self.page = page
        self._on_human_signal: Optional[Callable[[], None]] = None
        self._resume_note: Optional[str] = None
        self._resume_decision: Optional[bool] = None
        """True if Resume was clicked, False if End Run was, None until either
        resolves. Distinct from _resume_note, which just carries the optional
        free-text note either button provides."""
        self._restart_requested: bool = False
        """True if Restart Automation was clicked. Checked before
        _resume_decision by callers, since a restart is neither a plain
        resume nor a plain end."""
        self._confirm_result: Optional[bool] = None
        self._captured_actions: list[dict] = []
        self._capturing = False
        # A navigation wipes the document, banner included. These flags and the load handler redraw
        # the right banner after each navigation.
        self._pause_active = False
        self._pause_context = ""
        self._pause_show_cancel = False
        self._confirm_active = False
        self._confirm_message = ""
        page.add_init_script(_INIT_SCRIPT)
        page.add_init_script(_CAPTURE_INIT_JS)
        page.expose_function("__pwHumanSignal", self._handle_human_signal)
        page.expose_function("__pwResumeSignal", self._handle_resume)
        page.expose_function("__pwCancelSignal", self._handle_cancel)
        page.expose_function("__pwRestartSignal", self._handle_restart)
        page.expose_function("__pwConfirmSignal", self._handle_confirm)
        page.expose_function("__pwActionCaptured", self._handle_action_captured)
        page.on("load", self._on_load)
        try:
            # cover the page already loaded before add_init_script applies to future navigations
            page.evaluate(_INIT_SCRIPT)
            page.evaluate(_CAPTURE_INIT_JS)
        except Exception:
            pass

    def _on_load(self, page: Page) -> None:
        """Fires on every real navigation of the main frame. Re-renders
        whichever banner is currently active; a no-op otherwise."""
        if self._capturing:
            self._set_page_capturing(True)  # covers a new origin, where session storage starts empty
        if self._pause_active:
            self._render_pause_banner()
        if self._confirm_active:
            self._render_confirm_banner()

    def _render_pause_banner(self) -> None:
        try:
            self.page.evaluate(_SHOW_BANNER_JS, {"context": self._pause_context, "showCancel": self._pause_show_cancel})
        except Exception:
            pass

    def _render_confirm_banner(self) -> None:
        try:
            self.page.evaluate(_SHOW_CONFIRM_JS, self._confirm_message)
        except Exception:
            pass

    def _handle_human_signal(self) -> None:
        if self._on_human_signal:
            self._on_human_signal()

    def _handle_resume(self, note: str = "") -> None:
        self._resume_note = note or "(no note provided)"
        self._resume_decision = True

    def _handle_cancel(self, note: str = "") -> None:
        self._resume_note = note or "(no note provided)"
        self._resume_decision = False

    def _handle_restart(self, note: str = "") -> None:
        self._resume_note = note or "(no note provided)"
        self._restart_requested = True

    def _handle_confirm(self, approved: bool) -> None:
        self._confirm_result = approved

    def _handle_action_captured(self, action: str, descriptor: dict, value: Optional[str]) -> None:
        self._captured_actions.append({"action": action, "descriptor": descriptor, "value": value})

    def arm_human_detection(self, callback: Callable[[], None]) -> None:
        self._on_human_signal = callback

    def disarm_human_detection(self) -> None:
        self._on_human_signal = None

    def start_capturing(self) -> None:
        """Begins recording structured click/select/fill descriptors. Pair
        with stop_capturing() to bracket exactly the human-control window."""
        self._captured_actions = []
        self._capturing = True
        self._set_page_capturing(True)

    def _set_page_capturing(self, on: bool) -> None:
        """Sets the flag in the current document and in the tab's session storage, which is what a
        later document reads, so actions after a navigation are still captured."""
        script = (
            "(on) => { window.__pwCapturing = on; "
            "try { on ? sessionStorage.setItem('__pw_capturing', '1') : sessionStorage.removeItem('__pw_capturing'); } catch (e) {} }"
        )
        try:
            self.page.evaluate(script, on)
        except Exception:
            pass  # mid-navigation; _on_load sets it again once the new document is up

    def stop_capturing(self) -> list[dict]:
        """Stops recording and returns everything captured since
        start_capturing(), in order. Each entry: {action, descriptor, value}."""
        self._capturing = False
        self._set_page_capturing(False)
        actions, self._captured_actions = self._captured_actions, []
        return actions

    def mark_active(self, active: bool) -> None:
        """Call around every action the executor dispatches, so the injected listener can tell our
        actions from a human's. The short settle delay is cheap insurance."""
        try:
            self.page.evaluate(f"window.__pwActive = {'true' if active else 'false'}")
            self.page.wait_for_timeout(20)
        except Exception:
            pass  # mid-navigation; the init script reinstalls with __pwActive=false regardless

    def show_pause_banner(self, context: str = "", show_cancel: bool = False) -> None:
        """Shows the pause banner without blocking, so a caller can race it against another channel.
        show_cancel adds Restart and End Run buttons next to Resume."""
        self._resume_note = None
        self._resume_decision = None
        self._restart_requested = False
        self._pause_active = True
        self._pause_context = context
        self._pause_show_cancel = show_cancel
        self._render_pause_banner()

    def poll_resume_result(self) -> Optional[str]:
        """None until Resume, Restart, or End Run is clicked; call repeatedly from a poll loop."""
        return self._resume_note

    def poll_resume_decision(self) -> Optional[bool]:
        """True if Resume was clicked, False if End Run was, None if neither
        yet (also None if Restart was clicked; check poll_restart_requested() first)."""
        return self._resume_decision

    def poll_restart_requested(self) -> bool:
        """True if Restart Automation was clicked. Check this before
        poll_resume_decision(): restart is a distinct third choice, not a
        flavor of resume or end."""
        return self._restart_requested

    def hide_pause_banner(self) -> None:
        self._pause_active = False
        try:
            self.page.evaluate(_HIDE_BANNER_JS)
        except Exception:
            pass

    def pause_and_wait_for_resume(
        self, context: str = "", show_cancel: bool = False, poll_interval_s: float = 0.25, timeout_s: float = 3600
    ) -> tuple[str, bool, bool]:
        """Shows the banner and blocks until Resume, Restart or End Run is clicked, or a timeout.
        Returns (note, resume, restart). Silence is never treated as consent."""
        self.show_pause_banner(context, show_cancel=show_cancel)
        deadline = time.monotonic() + timeout_s
        while self.poll_resume_result() is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(int(poll_interval_s * 1000))  # pumps Playwright's event loop
        self.hide_pause_banner()
        note = self.poll_resume_result() or "(timed out waiting for resume)"
        restart = self.poll_restart_requested()
        resume = False if restart else bool(self.poll_resume_decision())
        return note, resume, restart

    def show_confirm_banner(self, message: str) -> None:
        """Shows the Approve/Deny banner without blocking; pairs with
        poll_confirm_result() so a caller can race this against another
        channel. Also survives navigation (see _on_load)."""
        self._confirm_result = None
        self._confirm_active = True
        self._confirm_message = message
        self._render_confirm_banner()

    def poll_confirm_result(self) -> Optional[bool]:
        """None until Approve/Deny is clicked; call repeatedly from a poll loop."""
        return self._confirm_result

    def hide_confirm_banner(self) -> None:
        self._confirm_active = False
        try:
            self.page.evaluate(_HIDE_CONFIRM_JS)
        except Exception:
            pass

    def pump(self, duration_s: float) -> None:
        """Lets Playwright's event loop process incoming messages for a short
        duration without dispatching anything ourselves."""
        self.page.wait_for_timeout(int(duration_s * 1000))
