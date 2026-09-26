"""Lets a human take control by directly clicking or typing in the live
browser window, instead of requesting control through a side channel (a
second terminal, a CLI command). This is the more natural complement to the
file-based PAUSE_REQUESTED signal in agent/loop.py — both end up at the same
control-transfer bookkeeping in handoff/session.py; this one just detects
"the human is already here" instead of asking them to declare it elsewhere.

Why detection works the way it does: a real human's click and a Playwright-
dispatched click produce identical DOM events (`isTrusted: true` either way
— that's deliberate, it's what lets automation exercise the same code paths
real input would, and it means there is no reliable "was this a human"
signal on the event itself). What's reliable instead is knowing when *we*
are not currently mid-dispatch: a small `window.__pwActive` flag, toggled
around every action the executor issues (see agent/executor.py), is checked
by an injected listener before it reports a mousedown/keydown back to
Python. Anything that fires while the flag is false didn't come from us.

Scope, stated honestly: detection is installed on the main frame. Our mock
app's only nested frame (the balance panel) is read-only display, not a
plausible place for a human to intervene, so this doesn't chase that case.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from playwright.sync_api import Page

_INIT_SCRIPT = r"""
(() => {
  if (window.__pwGestureInstalled) return;
  window.__pwGestureInstalled = true;
  window.__pwActive = false;
  const signal = (e) => {
    if (window.__pwActive) return;
    // Interacting with our OWN UI (the pause banner's Resume button/note
    // field, or the confirm banner's Approve/Deny buttons) is the human
    // using the control mechanism, not taking over the underlying page —
    // without this check, clicking "Resume Automation" is itself a
    // mousedown that immediately re-triggers detection, and the pause never
    // actually ends. Found the hard way: a real run escalated 22 times in
    // under 25 seconds, each escalation's own resume click re-triggering
    // the next one. Every element we inject shares the __pw_ui class so one
    // check covers all of them, not just whichever banner existed first.
    if (e.target && e.target.closest && e.target.closest('.__pw_ui')) return;
    if (window.__pwHumanSignal) window.__pwHumanSignal();
  };
  document.addEventListener('mousedown', signal, true);
  document.addEventListener('keydown', signal, true);
})();
"""

_SHOW_BANNER_JS = r"""
(() => {
  if (document.getElementById('__pw_pause_banner')) return;
  const bar = document.createElement('div');
  bar.id = '__pw_pause_banner';
  bar.className = '__pw_ui';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
    'background:#b30000;color:#fff;font:14px sans-serif;padding:10px 14px;' +
    'display:flex;align-items:center;gap:10px;box-shadow:0 2px 6px rgba(0,0,0,.4);';
  bar.innerHTML = '<span>● Automation paused — you are in control.</span>' +
    '<input id="__pw_note" placeholder="What did you do? (optional)" ' +
    'style="flex:1;padding:4px 8px;border:none;border-radius:3px;">' +
    '<button id="__pw_resume" style="padding:5px 14px;border:none;border-radius:3px;' +
    'background:#fff;color:#b30000;font-weight:bold;cursor:pointer;">Resume Automation</button>';
  document.body.appendChild(bar);
  document.getElementById('__pw_resume').addEventListener('click', () => {
    const note = document.getElementById('__pw_note').value;
    bar.remove();
    if (window.__pwResumeSignal) window.__pwResumeSignal(note);
  });
})();
"""

_HIDE_BANNER_JS = "document.getElementById('__pw_pause_banner')?.remove();"

# Same idea as the pause banner, for the *other* escalation path: a risky/
# irreversible action needs a yes/no before it happens. Previously this only
# ever showed up as a terminal prompt — a human watching the browser (which
# is the whole point of running headed) had no on-page indication a decision
# was pending. Approve/Deny buttons carry the shared __pw_ui class, so the
# existing "ignore clicks on our own UI" guard in _INIT_SCRIPT covers this too.
# page.evaluate(script, arg) calls the function with arg as its one parameter
# (a plain string here) — not a destructured array, that was a bug caught
# before this ever ran against a real page.
_SHOW_CONFIRM_JS = r"""
((message) => {
  if (document.getElementById('__pw_confirm_banner')) return;
  const bar = document.createElement('div');
  bar.id = '__pw_confirm_banner';
  bar.className = '__pw_ui';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
    'background:#8a5a00;color:#fff;font:14px sans-serif;padding:10px 14px;' +
    'display:flex;align-items:center;gap:10px;box-shadow:0 2px 6px rgba(0,0,0,.4);';
  const span = document.createElement('span');
  span.textContent = '⚠ Approval needed: ' + message;
  span.style.flex = '1';
  const approve = document.createElement('button');
  approve.id = '__pw_approve';
  approve.textContent = 'Approve';
  approve.style.cssText = 'padding:5px 14px;border:none;border-radius:3px;' +
    'background:#fff;color:#1a7a1a;font-weight:bold;cursor:pointer;';
  const deny = document.createElement('button');
  deny.id = '__pw_deny';
  deny.textContent = 'Deny';
  deny.style.cssText = 'padding:5px 14px;border:none;border-radius:3px;' +
    'background:#fff;color:#b30000;font-weight:bold;cursor:pointer;';
  bar.append(span, approve, deny);
  document.body.appendChild(bar);
  approve.addEventListener('click', () => { bar.remove(); window.__pwConfirmSignal && window.__pwConfirmSignal(true); });
  deny.addEventListener('click', () => { bar.remove(); window.__pwConfirmSignal && window.__pwConfirmSignal(false); });
})
"""

_HIDE_CONFIRM_JS = "document.getElementById('__pw_confirm_banner')?.remove();"

# Structured capture of what a human actually does while they have control —
# not a free-text note, a replayable action (see agent/executor.py's
# record_human_action and artifact/schema.py's Step.source). Armed only
# during a human-control window (start_capturing/stop_capturing below).
#
# The accessibleName/roleOf/cssPath functions here duplicate
# agent/perception.py's _SNAPSHOT_JS almost verbatim. That's deliberate, not
# an oversight: a captured action's full descriptor (role, name, css path)
# must be computed SYNCHRONOUSLY inside the same click/change event-handler
# tick that might immediately trigger a navigation (e.g. a link). Reporting
# back to Python first — say, to re-run a page-wide snapshot and match the
# clicked element by selector — races that navigation and can lose the
# element before Python ever looks for it, since expose_function calls made
# from an event handler don't block the page's own subsequent event handling
# or default action. Computing the descriptor client-side, before anything
# else can run, avoids the race outright. See perception.py if the two ever
# need to diverge; for now keeping them in lockstep is a manual step, not an
# enforced one — a real cost of not sharing this properly, worth knowing.
_CAPTURE_INIT_JS = r"""
(() => {
  if (window.__pwCaptureInstalled) return;
  window.__pwCaptureInstalled = true;
  window.__pwCapturing = false;

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
    // Kept in lockstep with agent/perception.py's identical fix: a <select>'s
    // innerText is its concatenated option list, never distinguishing between
    // same-shaped selects -- skip it and fall through to the row-based label.
    if (el.tagName !== 'SELECT') {
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
    """One per live page. Exposes the JS<->Python bridge once; callbacks are
    re-armed per use so this survives many pause/resume cycles in one run.
    """

    def __init__(self, page: Page):
        self.page = page
        self._on_human_signal: Optional[Callable[[], None]] = None
        self._resume_note: Optional[str] = None
        self._confirm_result: Optional[bool] = None
        self._captured_actions: list[dict] = []
        page.add_init_script(_INIT_SCRIPT)
        page.add_init_script(_CAPTURE_INIT_JS)
        page.expose_function("__pwHumanSignal", self._handle_human_signal)
        page.expose_function("__pwResumeSignal", self._handle_resume)
        page.expose_function("__pwConfirmSignal", self._handle_confirm)
        page.expose_function("__pwActionCaptured", self._handle_action_captured)
        try:
            # cover the page already loaded before add_init_script applies to future navigations
            page.evaluate(_INIT_SCRIPT)
            page.evaluate(_CAPTURE_INIT_JS)
        except Exception:
            pass

    def _handle_human_signal(self) -> None:
        if self._on_human_signal:
            self._on_human_signal()

    def _handle_resume(self, note: str = "") -> None:
        self._resume_note = note or "(no note provided)"

    def _handle_confirm(self, approved: bool) -> None:
        self._confirm_result = approved

    def _handle_action_captured(self, action: str, descriptor: dict, value: Optional[str]) -> None:
        self._captured_actions.append({"action": action, "descriptor": descriptor, "value": value})

    def arm_human_detection(self, callback: Callable[[], None]) -> None:
        self._on_human_signal = callback

    def disarm_human_detection(self) -> None:
        self._on_human_signal = None

    def start_capturing(self) -> None:
        """Begins recording structured click/select/fill descriptors from the live
        page. Pair with stop_capturing() to bracket exactly the human-control window
        — nothing captured before this or after stop_capturing() is called.
        """
        self._captured_actions = []
        try:
            self.page.evaluate("window.__pwCapturing = true")
        except Exception:
            pass

    def stop_capturing(self) -> list[dict]:
        """Stops recording and returns everything captured since start_capturing(),
        in the order it happened. Each entry: {action, descriptor, value}.
        """
        try:
            self.page.evaluate("window.__pwCapturing = false")
        except Exception:
            pass
        actions, self._captured_actions = self._captured_actions, []
        return actions

    def mark_active(self, active: bool) -> None:
        """Call around every action the executor itself dispatches, so the
        injected listener can tell 'we did this' apart from 'a human did this'.

        Correction, left in deliberately: an earlier version of this comment
        claimed a ~20ms settle delay here was "confirmed" necessary to fix a
        Runtime.evaluate/Input.dispatchMouseEvent CDP ordering race. Chasing
        apparent flakiness in the smoke test that motivated it turned up a
        different, mundane cause — the test was clicking coordinates that
        happened to land on real links in the mock app's compact layout,
        triggering real navigations that raced the signal, independent of this
        delay. See tests/test_gesture.py for the corrected version (inert
        click targets). The small delay stays as cheap, plausible insurance —
        evaluate() and dispatched input do go through independently-latent CDP
        domains — but nothing here actually isolated it as load-bearing, and
        it shouldn't be cited as if something did.
        """
        try:
            self.page.evaluate(f"window.__pwActive = {'true' if active else 'false'}")
            self.page.wait_for_timeout(20)
        except Exception:
            pass  # mid-navigation; the init script reinstalls with __pwActive=false regardless

    def show_pause_banner(self) -> None:
        """Shows the Resume Automation banner without blocking — pairs with
        poll_resume_result() so a caller can race this against another channel
        (e.g. the CLI prompt in _CliOperator.take_control) instead of committing
        to wait here only.
        """
        self._resume_note = None
        self.page.evaluate(_SHOW_BANNER_JS)

    def poll_resume_result(self) -> Optional[str]:
        """None until Resume Automation is clicked; call repeatedly from a poll loop."""
        return self._resume_note

    def hide_pause_banner(self) -> None:
        try:
            self.page.evaluate(_HIDE_BANNER_JS)
        except Exception:
            pass

    def pause_and_wait_for_resume(self, poll_interval_s: float = 0.25, timeout_s: float = 3600) -> str:
        """Shows the on-page banner, blocks until the human clicks Resume (or
        timeout), and returns their optional note. For callers with no other
        channel to race against (see _CliOperator.take_control for the version
        that does).
        """
        self.show_pause_banner()
        deadline = time.monotonic() + timeout_s
        while self.poll_resume_result() is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(int(poll_interval_s * 1000))  # pumps Playwright's event loop
        self.hide_pause_banner()
        return self.poll_resume_result() or "(timed out waiting for resume)"

    def show_confirm_banner(self, message: str) -> None:
        """Shows the Approve/Deny banner without blocking — pairs with
        poll_confirm_result() so a caller can race this against another
        channel (e.g. the CLI prompt) instead of committing to wait here only.
        """
        self._confirm_result = None
        self.page.evaluate(_SHOW_CONFIRM_JS, message)

    def poll_confirm_result(self) -> Optional[bool]:
        """None until Approve/Deny is clicked; call repeatedly from a poll loop."""
        return self._confirm_result

    def hide_confirm_banner(self) -> None:
        try:
            self.page.evaluate(_HIDE_CONFIRM_JS)
        except Exception:
            pass

    def pump(self, duration_s: float) -> None:
        """Lets Playwright's event loop process incoming messages (expose_function
        callbacks included) for a short duration without dispatching anything
        ourselves. Used by callers polling poll_confirm_result() in a loop.
        """
        self.page.wait_for_timeout(int(duration_s * 1000))
