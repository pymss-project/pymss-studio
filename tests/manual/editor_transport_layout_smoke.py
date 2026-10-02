"""Check the real editor transport, injecting props for long sessions and recording states."""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true", help="Report the existing 1041px layout without asserting.")
args = parser.parse_args()
base_url = os.environ.get("PYMSS_SMOKE_URL", "http://127.0.0.1:1428")
widths = [1041] if args.baseline else [980, 1040, 1041, 1080, 1100, 1280]
measure = """() => {
  const header = document.querySelector('.editor-transport');
  const bounds = header.getBoundingClientRect();
  const time = header.querySelector('.transport-timecode code').getBoundingClientRect();
  const buttons = [...header.querySelectorAll('button')].filter(button => button.getBoundingClientRect().width > 0);
  const rect = element => element.getBoundingClientRect();
  const overlaps = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
  const failures = [];
  if (time.left < bounds.left - 0.5 || time.right > bounds.right + 0.5) failures.push('timecode out of bounds');
  for (const button of buttons) {
    const box = rect(button);
    if (box.left < bounds.left - 0.5 || box.right > bounds.right + 0.5 || box.top < bounds.top - 0.5 || box.bottom > bounds.bottom + 0.5) failures.push(`out of bounds: ${button.textContent.trim() || button.title}`);
    if (overlaps(time, box)) failures.push(`timecode overlap: ${button.textContent.trim() || button.title}`);
  }
  for (let i = 0; i < buttons.length; i++) {
    for (let j = i + 1; j < buttons.length; j++) {
      if (overlaps(rect(buttons[i]), rect(buttons[j]))) failures.push('buttons overlap');
    }
  }
  return { failures, width: bounds.width, height: bounds.height, timecode: header.querySelector('.transport-timecode code').textContent };
}"""

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    results = []
    for locale in ("en", "zh-CN"):
        page = browser.new_page(viewport={"width": 1041, "height": 900})
        page.add_init_script(
            "localStorage.setItem('pymss-studio:app-settings', JSON.stringify("
            + json.dumps({"startupOnboardingSeen": True, "locale": locale}) + "));"
        )
        page.goto(f"{base_url}/#/editor", wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        page.locator(".editor-transport").wait_for(state="visible", timeout=60_000)
        for width in widths:
            page.set_viewport_size({"width": width, "height": 900})
            for state in ("normal", "long", "recording", "preparing", "stopping", "saving", "offline"):
                page.evaluate("""state => {
                  const instance = document.querySelector('.editor-transport-wrap').__vueParentComponent;
                  Object.assign(instance.props, {
                    sessionName: 'Session with a long descriptive title', trackCount: 24,
                    currentTime: state === 'normal' ? 10 : 599999.9,
                    duration: state === 'normal' ? 120 : 600000,
                    disabled: false, transportCanToggle: true, masterVolume: 2,
                    recordingState: ['recording', 'preparing', 'stopping'].includes(state) ? state : 'idle',
                    recordingElapsed: 599999.9, saving: state === 'saving', exporting: state === 'saving',
                    missingAssetCount: state === 'offline' ? 12 : 0,
                  });
                }""", state)
                page.wait_for_timeout(80)
                result = {"locale": locale, "viewport": width, "state": state, **page.evaluate(measure)}
                results.append(result)
                if not args.baseline:
                    assert not result["failures"], result
            if width == 1041:
                output = Path("data/outputs")
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / f"editor-transport-{'baseline' if args.baseline else 'fixed'}-{locale}.png"))
        if not args.baseline:
            page.set_viewport_size({"width": 1280, "height": 900})
            page.locator(".editor-transport-wrap").evaluate("element => element.style.width = '700px'")
            page.wait_for_timeout(80)
            result = {"locale": locale, "viewport": 1280, "state": "narrow-container", **page.evaluate(measure)}
            results.append(result)
            assert not result["failures"], result
        page.close()
    browser.close()
    failures = [result for result in results if result["failures"]]
    report = json.dumps({"checks": len(results), "failures": failures}, ensure_ascii=False, indent=2)
    output = Path("data/outputs")
    output.mkdir(parents=True, exist_ok=True)
    (output / f"editor-transport-{'baseline' if args.baseline else 'fixed'}-geometry.json").write_text(report + "\n", encoding="utf-8")
    print(report)
