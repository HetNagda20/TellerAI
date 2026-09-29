"""Verifies the gesture-based human-takeover mechanism (handoff/gesture.py):
a human clicking/typing directly in the live browser, independent of the
model asking for confirmation or calling give_up.

Click targets are deliberately inert (about:blank / a bare data: URL), not
the real mock app. An earlier version of this test clicked "empty-looking"
coordinates on the mock app's actual pages, which, given its compact
table layout, sometimes landed on a real link and triggered a genuine
navigation, racing the detection signal against context teardown. That was
a test-design bug, not a bug in the mechanism; seemed worth leaving this
note rather than silently swapping the target.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from handoff.gesture import GestureController


def test_own_dispatched_action_does_not_trigger_detection():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        signaled = {"count": 0}
        ctrl.arm_human_detection(lambda: signaled.__setitem__("count", signaled["count"] + 1))

        ctrl.mark_active(True)
        page.mouse.click(50, 50)
        ctrl.mark_active(False)
        page.wait_for_timeout(200)

        assert signaled["count"] == 0
        browser.close()


def test_untracked_click_triggers_detection():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        signaled = {"count": 0}
        ctrl.arm_human_detection(lambda: signaled.__setitem__("count", signaled["count"] + 1))

        page.mouse.click(60, 60)
        page.wait_for_timeout(200)

        assert signaled["count"] == 1
        browser.close()


def test_detection_survives_navigation():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        signaled = {"count": 0}
        ctrl.arm_human_detection(lambda: signaled.__setitem__("count", signaled["count"] + 1))

        page.mouse.click(10, 10)
        page.wait_for_timeout(200)
        page.goto("data:text/html,<html><body>blank</body></html>")
        page.mouse.click(20, 20)
        page.wait_for_timeout(200)

        assert signaled["count"] == 2
        browser.close()


def test_pause_and_wait_for_resume_captures_note():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)

        # Simulate the later resume-click from the page's own JS (a poller),
        # not a second Python thread, Playwright's sync API isn't thread-safe.
        page.evaluate(
            """
            (function poll() {
              const btn = document.getElementById('__pw_resume');
              if (btn) {
                document.getElementById('__pw_note').value = 'test note';
                btn.click();
              } else {
                setTimeout(poll, 50);
              }
            })();
            """
        )
        note, resume, restart = ctrl.pause_and_wait_for_resume(poll_interval_s=0.1, timeout_s=10)

        assert note == "test note"
        assert resume is True
        assert restart is False
        assert page.locator("#__pw_pause_banner").count() == 0
        browser.close()


def test_clicking_resume_button_does_not_retrigger_detection():
    # Regression: a real run escalated 22 times in under 25 seconds, because
    # clicking "Resume Automation" is itself a mousedown, and the original
    # listener didn't exclude clicks on its own banner, so every resume
    # immediately re-triggered another "human wants control" signal. The
    # earlier version of this test used a synthetic element.click() to
    # simulate the resume click, which, per the DOM spec, fires only a
    # `click` event, never `mousedown`, so it could never have caught this;
    # this one uses a real dispatched mouse click (mousedown + mouseup +
    # click, exactly what a physical click produces) at the button's actual
    # on-screen position.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        signaled = {"count": 0}
        ctrl.arm_human_detection(lambda: signaled.__setitem__("count", signaled["count"] + 1))

        page.evaluate(
            """
            (() => {
              const bar = document.createElement('div');
              bar.id = '__pw_pause_banner';
              bar.className = '__pw_ui';
              bar.innerHTML = '<input id="__pw_note"><button id="__pw_resume">Resume</button>';
              document.body.appendChild(bar);
              document.getElementById('__pw_resume').addEventListener('click', () => {
                if (window.__pwResumeSignal) window.__pwResumeSignal('resumed');
              });
            })();
            """
        )
        box = page.locator("#__pw_resume").bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(300)

        assert signaled["count"] == 0, "clicking Resume re-triggered human-takeover detection"
        browser.close()


def test_confirm_banner_approve_and_deny():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)

        assert ctrl.poll_confirm_result() is None
        ctrl.show_confirm_banner("click on button \"Confirm & Open Account\"")
        assert page.locator("#__pw_confirm_banner").count() == 1
        assert "Confirm & Open Account" in page.locator("#__pw_confirm_banner").inner_text()

        page.click("#__pw_deny")
        page.wait_for_timeout(100)
        assert ctrl.poll_confirm_result() is False

        # a fresh banner for the approve half of this check
        ctrl._confirm_result = None
        ctrl.show_confirm_banner("click on button \"Confirm Transfer\"")
        page.click("#__pw_approve")
        page.wait_for_timeout(100)
        assert ctrl.poll_confirm_result() is True

        ctrl.hide_confirm_banner()
        assert page.locator("#__pw_confirm_banner").count() == 0
        browser.close()


def test_clicking_confirm_banner_does_not_retrigger_detection():
    # Same lesson as the resume-button regression, applied to the other banner:
    # Approve/Deny live inside the same __pw_ui-classed container, so clicking
    # them must not register as the human taking over the underlying page.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        signaled = {"count": 0}
        ctrl.arm_human_detection(lambda: signaled.__setitem__("count", signaled["count"] + 1))

        ctrl.show_confirm_banner("a risky action")
        box = page.locator("#__pw_approve").bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(300)

        assert ctrl.poll_confirm_result() is True
        assert signaled["count"] == 0, "clicking Approve re-triggered human-takeover detection"
        browser.close()


def test_cli_operator_confirm_resolves_via_gesture_banner():
    # End-to-end: _CliOperator.confirm() races the terminal prompt against the
    # on-page banner. This exercises the banner side without needing real
    # stdin, the banner resolves first, so the stdin-reading daemon thread is
    # left abandoned (a documented, accepted limitation; see handoff/session.py).
    from handoff.session import InterventionRequest, _CliOperator

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("about:blank")
        ctrl = GestureController(page)
        operator = _CliOperator(gesture=ctrl)

        page.evaluate(
            """
            (() => {
              (function poll() {
                const btn = document.getElementById('__pw_approve');
                if (btn) { btn.click(); } else { setTimeout(poll, 50); }
              })();
            })();
            """
        )
        approved, note = operator.confirm(
            InterventionRequest(
                reason="risky_action_confirm",
                goal_or_capability="test",
                message="click on button \"Confirm & Open Account\"",
            )
        )

        assert approved is True
        assert "banner" in note
        browser.close()
