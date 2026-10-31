"""Check floating controls in real, sandboxed plugin pages at natural iframe heights."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from file_tool_views_smoke import server_url
from tool_views_smoke import HOST, complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with server_url(None) as base, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for width in (1100, 620):
                page = browser.new_page(viewport={"width": width, "height": 850})
                page.set_content("<html><body></body></html>")
                # Description may still be loading when the operation menu is opened.
                # This is the shortest real page, where neither direction fits initially.
                page.evaluate(HOST.replace("holdDescriptions:false", "holdDescriptions:true"),
                              {"specs": [], "url": f"{base}/plugins/analysis/ui/dist/index.html", "dark": False})
                page.evaluate("document.body.style.paddingTop='96px'")
                tool = page.frame_locator("#plugin")
                expect(tool.locator("#operation")).to_be_visible()
                page.wait_for_function("bridge.requests.some(r=>r.method==='pymss.resize')")
                tool.locator("#operation").click()
                menu = tool.locator(".n-select-menu")
                expect(menu).to_be_visible()
                menu.evaluate("""async menu => {
                    const deadline = performance.now() + 2500;
                    while (performance.now() < deadline) {
                        const box = menu.getBoundingClientRect();
                        if (box.top >= 0 && box.bottom <= innerHeight) return;
                        await new Promise(resolve=>setTimeout(resolve, 30));
                    }
                    const box = menu.getBoundingClientRect();
                    throw new Error(`Menu clipped: top=${box.top}, bottom=${box.bottom}, viewport=${innerHeight}`);
                }""")
                expect(menu.locator(".n-base-select-option")).to_have_count(4)
                menu.evaluate("async menu => { await Promise.all(menu.getAnimations({subtree:true}).map(animation => animation.finished.catch(()=>{}))) }")
                page.screenshot(path=str(args.artifacts / f"menu-{width}.png"), full_page=True)
                menu.locator(".n-base-select-option").filter(has_text="RMS 电平").click()
                expect(menu).not_to_be_visible()
                tool.locator("body").evaluate("""async body => {
                    const deadline = performance.now() + 2500;
                    while (Math.abs(innerHeight - body.getBoundingClientRect().height) > 2) {
                        if (performance.now() > deadline) throw new Error('Popup space was not released');
                        await new Promise(resolve=>setTimeout(resolve, 30));
                    }
                }""")
                # Reopening and keyboard dismissal must not accumulate reserved height.
                tool.locator("#operation").click()
                expect(menu).to_be_visible()
                tool.locator("#operation").press("Escape")
                expect(menu).not_to_be_visible()
                # Opening a full-window log dialog must not retain temporary menu space.
                tool.locator("#operation").click()
                expect(menu).to_be_visible()
                tool.get_by_role("button", name="日志", exact=True).click()
                page.wait_for_function("bridge.overlay === true")
                expect(tool.locator(".tool-activity-dialog")).to_be_visible()
                tool.locator("body").press("Escape")
                page.wait_for_function("bridge.overlay === false")
                tool.locator("body").evaluate("""async body => {
                    const deadline = performance.now() + 2500;
                    while (Math.abs(innerHeight - body.getBoundingClientRect().height) > 2) {
                        if (performance.now() > deadline) throw new Error('Log dialog left stale popup space');
                        await new Promise(resolve=>setTimeout(resolve, 30));
                    }
                }""")
                # Once description loading completes, exercise the regular form as well.
                page.evaluate("""() => {
                    const request = bridge.held.at(-1);
                    reply(request.id, {id:request.params.operation, inputs:{audio:{type:'audio'}},
                        params:{type:'object',properties:{}}, outputs:{report:{type:'object'}}});
                }""")
                expect(tool.locator("#input-audio")).to_be_visible()
                tool.locator("#operation").click()
                expect(menu).to_be_visible()
                bounds = menu.evaluate("menu=>{const b=menu.getBoundingClientRect();return {top:b.top,bottom:b.bottom,viewport:innerHeight}}")
                assert bounds["top"] >= 0 and bounds["bottom"] <= bounds["viewport"], bounds
                tool.locator("#operation").press("Escape")
                tool.get_by_role("button", name="浏览", exact=True).click()
                tool.locator("#run").click()
                page.wait_for_function("bridge.active !== null")
                complete(page, {"report": {"kind": "object", "value": {"rms_dbfs": -30}}})
                expect(tool.locator("#results")).to_contain_text("rms_dbfs")
                page.close()
        finally:
            browser.close()
    print(json.dumps({"popupBounds": "passed", "widths": [1100, 620], "selection": "passed", "dismissal": "passed"}))


if __name__ == "__main__":
    main()
