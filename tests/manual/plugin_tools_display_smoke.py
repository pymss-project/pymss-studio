"""Check real Studio plugin frames, log overlays and analysis presentation.

Only native IPC is substituted. Plugin HTML comes from the sibling checkout.
"""

from __future__ import annotations

import argparse
import re
import tempfile
import tomllib
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1564")
    parser.add_argument("--plugins", type=Path, default=Path(__file__).resolve().parents[3] / "pymss-plugins")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-plugin-display-"))
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    pages = {}
    for name in ["convert", "analysis"]:
        package = args.plugins / "plugins" / name
        project = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
        studio = project["tool"]["pymss"]["studio"]
        views = []
        for view in studio["views"]:
            views.append({**view, "sdkVersion": studio["sdk_version"], "requiredCapabilities": studio["required_capabilities"]})
            pages[f"/{name}/{view['id']}/index.html"] = package / view["entry"]
        rows.append({"name": name, "managed": True, "loaded": True, "enabled": True,
                     "toolCategory": studio["category"], "locales": studio["locales"], "contributions": {"views": views}})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""if(window===window.top)localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
              themeMode:'light',animationsEnabled:false
            }))""")
            def plugin_page(route):
                from urllib.parse import urlparse
                path = urlparse(route.request.url).path.removeprefix("/view")
                if path == "/sdk/pymss-sdk.js":
                    route.fulfill(path=str(Path(__file__).resolve().parents[2] / "src-tauri/resources/plugin-sdk/pymss-sdk.js"), content_type="text/javascript")
                elif path in pages:
                    route.fulfill(path=str(pages[path]), content_type="text/html")
                else:
                    route.fulfill(status=404, body="Not found")
            page.route("http://pymss-plugin.localhost/**", plugin_page)
            page.goto(args.url.rstrip("/") + "/#/tools", wait_until="networkidle", timeout=60000)
            page.evaluate(r"""async ({rows}) => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              window.__app=globals.$pinia._s.get('app');
              window.__settings=globals.$pinia._s.get('settings');
              window.__inventory={installed:rows,pluginsDir:'C:/plugins',runtime:{identity:'cpu',pythonPath:'C:/python.exe'},catalog:[],catalogError:null};
              const callbacks=new Map(), listeners=new Map();let sequence=0;
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__descriptions=[];window.__heldDescriptions=[];
              const describe=operation=>({id:operation,inputs:{audio:{type:'audio'}},
                params:{type:'object',properties:operation.endsWith('.spectrum')?{n_fft:{type:'integer',default:2048,minimum:64,maximum:32768}}:{}},outputs:{report:{type:'object'}}});
              window.__releaseDescriptions=()=>{window.__holdDescriptions=false;window.__heldDescriptions.splice(0).forEach(({resolve,operation})=>resolve(describe(operation)))};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                convertFileSrc:(path,protocol)=>`http://${protocol}.localhost/${path}`,
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},
                unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin_marketplace')return structuredClone(window.__inventory);
                  if(command==='get_build_info')return {version:'0.0.23',updateSupported:false};
                  if(command==='load_plugin_tool_settings')return {};
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='load_app_store')return null;
                  if(command==='save_plugin_tool_settings')return args.data;
                  if(command==='describe_operation'){
                    const operation=args.payload.operation;window.__descriptions.push(operation);
                    if(window.__holdDescriptions)return new Promise(resolve=>window.__heldDescriptions.push({resolve,operation}));
                    return describe(operation);
                  }
                  if(command==='plugin:dialog|open')return 'C:/audio/source.wav';
                  if(command==='start_operation_task'){
                    const operation=args.payload.operation;
                    const value=operation.endsWith('.spectrum')?{freqs:[0,1000,2000,3000,4000],magnitudes_db:[-80,-12,-50,-60,-80]}:
                      operation.endsWith('.measure_lufs')?{lufs:'-Infinity'}:{sample_peak_dbfs:-27.467469,true_peak_dbtp:-27.461275};
                    const payload={operation,outputs:{report:{kind:'object',value}}};
                    setTimeout(()=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:{type:'operation_result',requestId:args.payload.requestId,taskId:args.payload.requestId,payload}})},40);
                    return {taskId:args.payload.requestId,started:true};
                  }
                  return null;
                }};
              const market=(await import('/src/features/plugin-marketplace/composable.ts')).usePluginMarketplace();
              market.snapshot.value=structuredClone(window.__inventory);
              market.error.value='';
              window.__app.setWorkerEventConnection('connected');
              await globals.$router.push('/tools?pluginView=convert/tool');
            }""", {"rows": rows})
            tool = page.frame_locator(".plugin-view-frame")
            expect(tool.locator(".tool-log-button")).to_be_visible()
            for mode in ["light", "dark"]:
                page.evaluate("mode=>window.__settings.themeMode=mode", mode)
                expect(tool.locator("html")).to_have_attribute("data-mode", mode)
                expect(page.locator(".plugin-view-frame")).to_have_css("color-scheme", mode)
                expect(tool.locator("html")).to_have_css("color-scheme", mode)
                before = tool.locator(".audio-tool-layout").bounding_box()
                tool.locator(".tool-log-button").click()
                expect(tool.locator("html")).to_have_class(re.compile("tool-overlay-anchored"))
                expect(tool.locator(".tool-activity-dialog")).to_be_visible()
                expect(tool.locator(".audio-tool-layout")).to_be_visible()
                after = tool.locator(".audio-tool-layout").bounding_box()
                for key in ["x", "y", "width"]:
                    assert abs(before[key] - after[key]) < 2, (mode, before, after)
                tool.locator(".tool-activity-dialog").evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
                page.screenshot(path=str(output / f"logs-{mode}.png"), full_page=True)
                tool.locator("body").press("Escape")
                expect(tool.locator(".tool-activity-dialog")).not_to_be_visible()
                expect(page.locator(".plugin-view-frame--overlay")).to_have_count(0)
                expect(tool.locator(".tool-log-button")).to_be_enabled()
            page.evaluate("""async()=>{
              window.__settings.themeMode='light';window.__holdDescriptions=true;
              await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/tools?pluginView=analysis/tool');
            }""")
            # Inputs must be usable even before a delayed worker description returns.
            expect(tool.locator("#input-audio")).to_be_visible()
            tool.get_by_role("button", name="浏览", exact=True).click()
            expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
            expect(tool.locator("#run")).to_be_disabled()
            page.wait_for_function("window.__heldDescriptions.length===1")
            page.evaluate("window.__releaseDescriptions()")
            expect(tool.locator("#run")).to_be_enabled()

            def select_operation(label):
                tool.locator("#operation").click()
                tool.locator(".n-base-select-option").filter(has_text=re.compile("^" + re.escape(label) + "$" )).click()

            page.evaluate("window.__holdDescriptions=true")
            select_operation("真峰值")
            expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
            expect(tool.locator("#run")).to_be_disabled()
            page.wait_for_function("window.__heldDescriptions.length===1")
            page.evaluate("window.__releaseDescriptions()")
            expect(tool.locator("#run")).to_be_enabled()
            count = page.evaluate("window.__descriptions.length")
            select_operation("综合响度")
            expect(tool.locator("#run")).to_be_enabled()
            assert page.evaluate("window.__descriptions.length") == count
            tool.locator("#run").click()
            expect(tool.locator(".analysis-measurements")).to_contain_text("−∞")
            expect(tool.locator(".analysis-measurements")).to_contain_text("LUFS")
            select_operation("真峰值")
            tool.locator("#run").click()
            expect(tool.locator(".analysis-measurements")).to_contain_text("-27.46")
            expect(tool.locator(".analysis-measurements")).to_contain_text("dBTP")
            expect(tool.locator(".analysis-measurements")).to_contain_text("采样峰值")
            expect(tool.locator(".analysis-raw pre")).not_to_be_visible()
            tool.locator("#results").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "analysis-results.png"), full_page=True)
            await_route = "document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/tools?pluginView=analysis/spectrum')"
            page.evaluate(await_route)
            expect(tool.get_by_role("heading", name="频谱分析", exact=True)).to_be_visible()
            tool.get_by_role("button", name="浏览", exact=True).click()
            tool.locator("#run").click()
            expect(tool.locator(".analysis-spectrum polyline")).to_have_attribute("points", re.compile(r"\d"))
            expect(tool.locator(".analysis-hint")).to_contain_text("FFT")
            page.screenshot(path=str(output / "spectrum.png"), full_page=True)
            assert not errors, errors
            print(f"Studio log overlays, cached forms, measurement results and spectrum verified: {output}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
