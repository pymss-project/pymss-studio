"""Exercise overlay resizing and slicer validation against built plugin pages.

The existing SDK host mock holds each operation until a terminal result is
injected. No Python worker, model download, or audio conversion is performed.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from file_tool_views_smoke import HOST, ROOT, open_tool, server_url


def settle_content(page) -> float:
    return page.frames[1].evaluate("""() => new Promise(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() =>
        resolve(document.body.getBoundingClientRect().height)));
    })""")


def complete_operation(page, result):
    page.evaluate("""result => document.querySelector('#plugin').contentWindow.postMessage({
      jsonrpc: '2.0', id: bridge.active.id, result,
    }, '*')""", result)


def check_restored_height(page, initial_height: float, name: str, artifacts: Path):
    page.keyboard.press("Escape")
    page.wait_for_function("bridge.overlay === false")
    body_height = settle_content(page)
    page.wait_for_function("""bodyHeight => {
      const height = document.querySelector('#plugin').getBoundingClientRect().height;
      return height >= bodyHeight && height - bodyHeight <= 2;
    }""", arg=body_height)
    iframe_height = page.locator("#plugin").bounding_box()["height"]
    assert body_height > initial_height + 20, (name, initial_height, body_height)
    page.screenshot(path=str(artifacts / f"{name}-restored.png"), full_page=True)
    return {"initialHeight": initial_height, "bodyHeight": body_height, "iframeHeight": iframe_height}


def check_convert(browser, base_url: str, artifacts: Path):
    page, frame = open_tool(browser, base_url, "convert", width=1100)
    try:
        page.wait_for_load_state("networkidle")
        frame.get_by_role("button", name="Choose audio files", exact=True).click()
        initial_height = settle_content(page)
        page.evaluate("bridge.cancelNext = true")
        frame.locator(".audio-tool-actions button").click()
        page.wait_for_function("bridge.active?.method === 'operations.start'")
        frame.locator(".tool-log-button").click()
        page.wait_for_function("bridge.overlay === true")
        complete_operation(page, {
            "operation": "pymss.convert.batch",
            "outputs": {"report": {"kind": "object", "value": {
                "operation": "convert", "outputDir": "/output", "outputPaths": ["/output/source.ogg"],
                "succeeded": 1, "failed": [],
            }}},
        })
        expect(frame.locator(".audio-tool-card--side .result-summary")).to_have_count(1)
        settle_content(page)
        return check_restored_height(page, initial_height, "convert", artifacts)
    finally:
        page.close()


def check_example(browser, base_url: str, artifacts: Path, sdk: Path):
    page = browser.new_page(viewport={"width": 1100, "height": 900}, locale="en-US")
    try:
        page.route("**/sdk/pymss-sdk.js", lambda route: route.fulfill(path=str(sdk), content_type="text/javascript"))
        page.set_content("<!doctype html><html><body></body></html>")
        # The shipped SDK also reads its version and capability list from the handshake.
        host = HOST.replace("method:'host.ready',params:{}", "method:'host.ready',params:sdk")
        page.evaluate(host, {
            "url": f"{base_url}/examples/audio_gain/ui/dist/index.html",
            "dark": False, "platform": "win32", "machine": "AMD64",
        })
        frame = page.frame_locator("#plugin")
        page.wait_for_function("bridge.requests.some(request => request.method === 'settings.read')")
        page.wait_for_load_state("networkidle")
        frame.get_by_role("button", name="Browse", exact=True).nth(0).click()
        initial_height = settle_content(page)
        page.evaluate("bridge.cancelNext = true")
        frame.get_by_role("button", name="Apply gain", exact=True).click()
        page.wait_for_function("bridge.active?.method === 'operations.start'")
        frame.get_by_role("button", name="Logs", exact=True).click()
        page.wait_for_function("bridge.overlay === true")
        complete_operation(page, {
            "operation": "example.audio_gain.apply",
            "outputs": {
                "audio": {"kind": "audio_file", "path": "/output/source-gain.wav"},
                "report": {"kind": "object", "value": {"peak_before": 0.2, "peak_after": 0.2, "peak_limited": False}},
            },
        })
        expect(frame.locator(".gain-results")).to_have_count(1)
        settle_content(page)
        return check_restored_height(page, initial_height, "audio-gain", artifacts)
    finally:
        page.close()


def check_slicer(browser, base_url: str, artifacts: Path):
    page, frame = open_tool(browser, base_url, "slicer", width=1100)
    try:
        page.wait_for_load_state("networkidle")
        frame.get_by_role("button", name="Choose audio files", exact=True).click()
        action = frame.get_by_role("button", name="Slice audio", exact=True)
        expect(action).to_be_enabled()
        controls = frame.locator(".n-input-number input")
        expect(controls).to_have_count(5)
        checked = []
        for index, name in enumerate(("threshold", "minLength", "minInterval", "maxSilKept", "hopSize")):
            control = controls.nth(index)
            original = control.input_value()
            control.fill("")
            control.press("Tab")
            expect(action).to_be_disabled()
            assert page.evaluate("bridge.requests.filter(request => request.method === 'operations.start').length") == 0
            checked.append(name)
            control.fill(original)
            control.press("Tab")
            expect(action).to_be_enabled()
        action.click()
        page.wait_for_function("bridge.requests.some(request => request.method === 'operations.start')")
        request = page.evaluate("bridge.requests.find(request => request.method === 'operations.start').params.inputs.request")
        assert all(isinstance(request[name], (int, float)) for name in checked), request
        expect(frame.get_by_text("clip.wav", exact=True)).to_be_visible()
        page.screenshot(path=str(artifacts / "slicer-restored.png"), full_page=True)
        return {"clearedControlsDisabled": checked, "restoredRequest": request}
    finally:
        page.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Use an existing loopback server")
    parser.add_argument("--studio-root", type=Path, default=ROOT.parent / "pymss-studio", help="Studio checkout supplying the standalone example's public SDK")
    parser.add_argument("--artifacts", type=Path, help="Screenshot/report directory (defaults to a system temporary directory)")
    args = parser.parse_args()
    sdk = args.studio_root / "src-tauri/resources/plugin-sdk/pymss-sdk.js"
    if not sdk.is_file():
        parser.error(f"Studio SDK was not found: {sdk}")
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix="pymss-plugin-ui-regressions-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    with server_url(args.url) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results = {
                "convertOverlay": check_convert(browser, base_url, artifacts),
                "exampleOverlay": check_example(browser, base_url, artifacts, sdk),
                "slicerValidation": check_slicer(browser, base_url, artifacts),
            }
        finally:
            browser.close()
    report = {"results": results, "artifacts": str(artifacts)}
    (artifacts / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
