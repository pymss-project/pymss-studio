"""Exercise runtime preparation and default tool onboarding in the real Vue app."""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import tomllib
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1564")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    studio = Path(__file__).resolve().parents[2]
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-inventory-browser-"))
    output.mkdir(parents=True, exist_ok=True)
    package = studio / "python/preinstalled_plugins/pymss.convert"
    metadata = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pymss"]["studio"]
    views = [{**view, "entryPath": f"C:/plugins/pymss.convert/{view['entry']}",
              "sdkVersion": metadata["sdk_version"], "requiredCapabilities": metadata.get("required_capabilities", [])}
             for view in metadata["views"]]
    ready = {"name": "pymss.convert", "version": "0.2.0", "managed": True, "enabled": True,
             "loaded": True, "prepared": True, "availability": "ready", "toolCategory": "convert",
             "locales": metadata["locales"], "operations": [], "contributions": {"views": views}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""if(window===window.top)localStorage.setItem('pymss-studio:app-settings',JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
              themeMode:'light',animationsEnabled:false,pluginDefaultsPolicy:'review'
            }))""")

            def plugin_page(route):
                path = urlparse(route.request.url).path.removeprefix("/view")
                if path == "/sdk/pymss-sdk.js":
                    route.fulfill(path=str(studio / "src-tauri/resources/plugin-sdk/pymss-sdk.js"), content_type="text/javascript")
                elif path == "/pymss.convert/tool/index.html":
                    route.fulfill(path=str(package / views[0]["entry"]), content_type="text/html")
                else:
                    route.fulfill(status=404, body="Not found")

            page.route("http://pymss-plugin.localhost/**", plugin_page)
            page.goto(args.url.rstrip("/") + "/#/tools", wait_until="networkidle", timeout=60000)
            page.evaluate(r"""async ready => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              window.__settings=globals.$pinia._s.get('settings');
              window.__ready=ready;window.__calls=[];window.__failPrepare=true;
              window.__inventory={installed:[{...ready,loaded:false,prepared:false,availability:'not_prepared',
                sourceSelectionRequired:true,availableBindings:[
                  {runtime:'cpu-py312',version:'0.1.0',content:'a'.repeat(64)},
                  {runtime:'cuda-py312',version:'0.2.0',content:'b'.repeat(64)}]}],
                pluginsDir:'C:/plugins',runtime:{identity:'rocm-py312',pythonPath:'C:/python.exe'},catalog:[],catalogError:null};
              const callbacks=new Map(),listeners=new Map();let sequence=0;
              const terminal=(type,payload,id)=>{for(const callback of listeners.values())callbacks.get(callback)?.({
                event:'pymss://worker-event',payload:{type,taskId:id,payload}})};
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                convertFileSrc:(path,protocol)=>`http://${protocol}.localhost/${path}`,
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},
                unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  window.__calls.push({command,...structuredClone(args||{})});
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin_marketplace')return structuredClone(window.__inventory);
                  if(command==='manage_plugin'){
                    const id=args.payload.taskId;
                    if(window.__failPrepare){window.__failPrepare=false;
                      setTimeout(()=>terminal('error',{code:'PLUGIN_MANAGE_FAILED',message:'Dependency preparation failed',recoverable:true},id),30);
                    }else{window.__inventory={...window.__inventory,installed:[window.__ready],preinstalledPending:[],preinstalledAvailable:[]};
                      setTimeout(()=>terminal('plugin_manage_result',structuredClone(window.__inventory),id),30);}
                    return {taskId:id};
                  }
                  if(command==='get_build_info')return {version:'0.0.23',updateSupported:false};
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='load_plugin_tool_settings')return {};
                  if(command==='save_plugin_tool_settings')return args.data;
                  if(command==='load_app_store')return null;
                  return null;
                }};
              window.__market=(await import('/src/features/plugin-marketplace/composable.ts')).usePluginMarketplace();
              await window.__market.refreshInventory();
              await globals.$router.push('/tools?pluginView=pymss.convert/tool');
            }""", ready)
            panel = page.locator(".plugin-tool-unavailable")
            expect(panel).to_contain_text("安装记录已保留")
            expect(page.locator("iframe.plugin-view-frame")).to_have_count(0)
            prepare = panel.get_by_role("button", name="在当前环境准备", exact=True)
            expect(prepare).to_be_disabled()
            panel.locator(".n-select").click()
            page.get_by_text("cuda-py312 · 0.2.0", exact=True).click()
            expect(prepare).to_be_enabled()
            expect(page.locator(".n-base-select-menu")).not_to_be_visible()
            page.screenshot(path=str(output / "source-choice.png"), full_page=True)
            prepare.click()
            expect(page.get_by_text("Dependency preparation failed", exact=True)).to_be_visible()
            expect(page.locator("iframe.plugin-view-frame")).to_have_count(0)
            expect(prepare).to_be_enabled()
            prepare.click()
            try:
                expect(page.locator("iframe.plugin-view-frame")).to_have_count(1)
            except AssertionError:
                page.screenshot(path=str(output / "prepare-failure.png"), full_page=True)
                print(page.locator("body").inner_text())
                print(page.evaluate("({snapshot:window.__market.snapshot.value,calls:window.__calls})"))
                print(errors)
                raise
            expect(page.frame_locator("iframe.plugin-view-frame").get_by_role("heading", name="音频格式转换", exact=True)).to_be_visible()
            starts = page.evaluate("window.__calls.filter(item=>item.command==='manage_plugin')")
            assert len(starts) == 2, starts
            assert all(item["payload"]["sourceRuntime"] == "cuda-py312" for item in starts)
            assert all(item["payload"]["sourceContent"] == "b" * 64 for item in starts)
            assert all(item["payload"]["expectedRuntime"] == "rocm-py312" for item in starts)

            page.evaluate("""async () => {
              window.__calls=[];window.__inventory={...window.__inventory,installed:[],preinstalledAvailable:['pymss.convert'],preinstalledPending:[]};
              await window.__market.refreshInventory();
              await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/tools');
            }""")
            enable = page.get_by_role("button", name="启用基础工具", exact=True)
            expect(enable).to_be_visible()
            assert not page.evaluate("window.__calls.some(item=>item.command==='manage_plugin')")
            page.screenshot(path=str(output / "existing-profile.png"), full_page=True)
            enable.click()
            expect(page.locator("iframe.plugin-view-frame")).to_have_count(1)
            assert page.evaluate("window.__settings.pluginDefaultsPolicy") == "auto"
            assert page.evaluate("window.__calls.find(item=>item.command==='manage_plugin').payload.action") == "prepare_preinstalled"

            page.evaluate("""async () => {
              window.__calls=[];window.__inventory={...window.__inventory,installed:[],preinstalledAvailable:['pymss.convert'],preinstalledPending:['pymss.convert']};
              await window.__market.refreshInventory();
            }""")
            expect(page.locator("iframe.plugin-view-frame")).to_have_count(1)
            assert len(page.evaluate("window.__calls.filter(item=>item.command==='manage_plugin')")) == 1
            assert not errors, errors
            print(f"Source selection, failed preparation retry, iframe gating and default tool onboarding passed: {output}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
