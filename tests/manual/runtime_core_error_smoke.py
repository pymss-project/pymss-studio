"""Verify localized core-update errors and retained diagnostics in Settings."""

from __future__ import annotations

import argparse
import re
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1564")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-runtime-error-ui-"))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1420, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
              themeMode:'light',animationsEnabled:false
            }))""")
            page.goto(args.url.rstrip("/") + "/#/tools", wait_until="networkidle", timeout=60000)
            page.evaluate(r"""async () => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              window.__app=globals.$pinia._s.get('app');
              window.__settings=globals.$pinia._s.get('settings');
              window.__runtime={platform:'win32',machine:'AMD64',ready:true,installedBackend:'cuda',
                torchBackend:'cuda',torchVersion:'2.7.1+cu128',pymssVersion:'2.1.12',
                manifestVersion:'2026.10.2',installedEnvironments:[{
                  backend:'cuda',source:'managed',health:'ready',pythonPath:'C:/runtime/python.exe',
                  torchVersion:'2.7.1+cu128',pymssVersion:'2.1.12',pymssCoreVersion:'0.1.14',
                  manifestVersion:'2026.10.2',acceleratorAvailable:true,pymssGraphAvailable:true,
                  coreUpdateSupported:true,
                }]};
              // The update worker can discover changed metadata after the UI's cached probe.
              window.__nextError={code:'RUNTIME_MANIFEST_INCOMPATIBLE',recoverable:true,
                message:'The runtime manifest is newer than this application or has an unknown version.',
                manifestStatus:'newer',runtimeManifestVersion:'2026.10.4',appManifestVersion:'2026.10.2'};
              let sequence=0;
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                transformCallback:()=>++sequence,unregisterCallback(){},
                async invoke(command,args){
                  if(command==='plugin:event|listen')return ++sequence;
                  if(command==='load_app_store')return null;
                  if(command==='runtime_info')return structuredClone(window.__runtime);
                  if(command==='runtime_env_sizes')return {sizes:{},incompleteBackends:[]};
                  if(command==='runtime_core_versions')return {packages:{
                    pymss:{currentVersion:'2.1.12',latestVersion:'2.2.0',updateAvailable:true},
                    'pymss-core':{currentVersion:'0.1.14',latestVersion:'0.1.14',updateAvailable:false}}};
                  if(command==='get_env_info')return {platform:'win32',pymssAvailable:true,
                    torchAvailable:true,torchVersion:'2.7.1+cu128',torchBackend:'cuda',pymssVersion:'2.1.12'};
                  if(command==='get_build_info')return {version:'0.0.23',updateSupported:false};
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='start_runtime_core_update'){
                    const taskId=args.payload.taskId;
                    setTimeout(()=>window.__app.handleRuntimeEvent({type:'error',taskId,
                      payload:structuredClone(window.__nextError)}),20);
                    return {taskId,started:true};
                  }
                  return null;
                }};
              window.__app.runtimeInfo=structuredClone(window.__runtime);
              window.__app.setWorkerEventConnection('connected');
              await globals.$router.push('/settings?section=runtime');
              await window.__app.loadRuntimeCoreVersions();
            }""")
            page.get_by_role("button", name="稍后处理", exact=True).click()
            expect(page.locator(".n-modal-mask")).to_have_count(0)
            update = page.get_by_role("button", name="更新内核", exact=True)
            expect(update).to_be_enabled()
            update.click()
            message = page.locator(".runtime-install-error__message")
            details = page.locator("details.runtime-error-details")
            expect(message).to_have_count(1)
            expect(message).to_contain_text("请先更新 Studio")
            expect(message).not_to_contain_text("The runtime manifest")
            expect(details.locator("pre")).not_to_be_visible()
            message.scroll_into_view_if_needed()
            page.screenshot(path=str(output / "runtime-error-zh.png"), full_page=True)
            details.locator("summary").click()
            expect(details.locator("pre")).to_contain_text("RUNTIME_MANIFEST_INCOMPATIBLE")
            expect(details.locator("pre")).to_contain_text("The runtime manifest")
            expect(details.locator("pre")).to_contain_text("2026.10.4")
            details.scroll_into_view_if_needed()
            page.screenshot(path=str(output / "runtime-error-details.png"), full_page=True)

            page.evaluate("window.__settings.locale='en'")
            expect(message).to_contain_text("Update Studio first")
            expect(details.locator("summary")).to_have_text("Technical details")
            page.screenshot(path=str(output / "runtime-error-en.png"), full_page=True)
            page.get_by_role("button", name="Go to software updates", exact=True).click()
            expect(page).to_have_url(re.compile(r"section=about"))

            page.evaluate("""async () => {
              window.__settings.locale='zh-CN';
              window.__nextError={code:'UNRECOGNIZED_CORE_ERROR',message:'Original diagnostic reason',details:'Traceback retained'};
              await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings?section=runtime');
            }""")
            page.get_by_role("button", name="更新内核", exact=True).click()
            expect(message).to_contain_text("运行环境更新未完成")
            if not details.evaluate("element => element.open"):
                details.locator("summary").click()
            expect(details.locator("pre")).to_contain_text("Original diagnostic reason")
            expect(details.locator("pre")).to_contain_text("Traceback retained")
            assert not errors, errors
            print(f"Localized core errors, diagnostics and update navigation verified: {output}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
