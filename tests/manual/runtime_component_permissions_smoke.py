"""Check macOS runtime storage notices and core-update confirmation."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:1564")
parser.add_argument("--output-dir", type=Path, default=Path(".tmp/runtime-component-permissions"))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',themeMode:'light',animationsEnabled:false
    }))""")
    try:
        page.goto(args.url.rstrip("/") + "/#/tools", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        page.evaluate("""() => {
          const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
          window.__app=stores.get('app');window.__settings=stores.get('settings');
          window.__componentStatus={package:'funasr',requirement:'funasr==1.3.26',installed:true,present:true,
            version:'1.3.26',source:'environment',canUninstall:true,runtimeIdentity:'cpu-py312'};
          window.__runtime={platform:'win32',machine:'AMD64',ready:true,installedBackend:'cpu',
            torchBackend:'cpu',torchVersion:'2.7.1',pymssVersion:'2.1.8',manifestVersion:'2026.10.2',
            installedEnvironments:[]};
          window.__callbacks=new Map();window.__listeners=new Map();window.__calls=[];window.__sequence=0;
          window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
            transformCallback: callback => {const id=++window.__sequence;window.__callbacks.set(id,callback);return id;},
            unregisterCallback: id => window.__callbacks.delete(id),
            invoke:async(command,args)=>{
              window.__calls.push(command);
              if(command==='optional_runtime_package_status')return structuredClone(window.__componentStatus);
              if(command==='manage_optional_runtime_package'){
                window.__componentTask=args.payload.taskId;return {taskId:args.payload.taskId,started:true};
              }
              if(command==='plugin:event|listen'){
                const id=++window.__sequence;window.__listeners.set(id,args);return id;
              }
              if(command==='plugin:event|unlisten'){window.__listeners.delete(args.eventId);return;}
              if(command==='load_app_store')return null;
              if(command==='runtime_info')return structuredClone(window.__runtime);
              if(command==='runtime_env_sizes')return {sizes:{},incompleteBackends:[]};
              if(command==='runtime_core_versions')return {packages:{
                pymss:{currentVersion:'2.1.8',latestVersion:'2.1.8',updateAvailable:false},
                'pymss-core':{currentVersion:'0.1.10',latestVersion:'0.1.11',updateAvailable:true}}};
              if(command==='get_env_info')return {platform:window.__runtime.platform,pymssAvailable:true,
                torchAvailable:true,torchVersion:'2.7.1',torchBackend:'cpu',pymssVersion:'2.1.8'};
              if(command==='get_build_info')return {version:'0.0.12',updateSupported:false};
              if(command==='get_workflow_editors')return {revision:0,editors:[]};
              if(command==='cancel_task')return true;
              return null;
            }};
          window.__emitComponentStatus=()=>{
            for(const [id,listener] of window.__listeners){
              if(listener.event==='pymss://worker-event')window.__callbacks.get(listener.handler)?.({
                event:listener.event,id,payload:{type:'optional_package_status',taskId:window.__componentTask,
                  payload:structuredClone(window.__componentStatus)}});
            }
          };
          window.__app.runtimeInfo=structuredClone(window.__runtime);
          window.__app.setWorkerEventConnection('connected');
        }""")
        # Component controls belong to the independent ASR plugin view and are
        # exercised by pymss-plugins/tests/manual/migrated_tool_views_smoke.py.
        dialog = page.get_by_role("dialog")
        page.evaluate("""async () => {
          window.__runtime.platform='darwin';window.__runtime.machine='arm64';
          window.__runtime.installedBackend='mlx';
          window.__runtime.installedEnvironments=[{backend:'mlx',source:'bundled',health:'ready',
            pythonPath:'/Applications/Pymss Studio.app/Contents/Resources/python-runtime/bin/python3',
            torchVersion:'2.7.1',pymssVersion:'2.1.8',pymssCoreVersion:'0.1.10',
            manifestVersion:'2026.10.2',acceleratorAvailable:true,pymssGraphAvailable:true,
            coreUpdateSupported:true}];
          window.__corePlan={id:'runtime-permissions-plan',backend:'mlx',mirror:'pypi',mode:'update',
            pythonPath:window.__runtime.installedEnvironments[0].pythonPath,channel:'stable',
            currentVersions:{pymss:'2.1.8','pymss-core':'0.1.10'},
            targetVersions:{pymss:'2.1.8','pymss-core':'0.1.11'},dependencyCount:1};
          window.__app.prepareRuntimeCoreUpdate=async()=>structuredClone(window.__corePlan);
          window.__app.discardRuntimeCorePlan=id=>{window.__discardedPlan=id;};
          window.__app.updateRuntimeCore=async(...args)=>{window.__appliedPlan=args;};
          window.__app.runtimeInfo=structuredClone(window.__runtime);
          await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings?section=runtime');
        }""")
        expect(page.locator(".runtime-section")).to_contain_text("内核更新独立保存在用户数据目录中，不会修改基础运行环境。")
        expect(page.locator(".runtime-env-card--active")).to_contain_text("Apple MLX")
        page.get_by_role("button", name="稍后处理", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.locator(".n-message")).to_have_count(0)
        expect(page.get_by_role("button", name="更新内核", exact=True)).to_be_enabled()
        page.screenshot(path=str(args.output_dir / "macos-runtime-storage.png"))
        page.get_by_role("button", name="更新内核", exact=True).click()
        expect(dialog).to_contain_text("基础环境保持不变")
        expect(dialog).to_contain_text("0.1.11")
        page.screenshot(path=str(args.output_dir / "macos-core-confirmation.png"), animations="disabled")
        dialog.get_by_role("button", name="取消", exact=True).click()
        page.wait_for_function("window.__discardedPlan===window.__corePlan.id")
        expect(dialog).to_have_count(0)
        assert page.evaluate("window.__appliedPlan===undefined")
        page.evaluate("window.__settings.locale='en'")
        expect(page.locator(".runtime-section")).to_contain_text("Core updates are stored separately in your user data directory")
        page.screenshot(path=str(args.output_dir / "macos-runtime-storage-en.png"))
        page.get_by_role("button", name="Update Core", exact=True).click()
        expect(dialog).to_contain_text("Base runtime unchanged")
        page.screenshot(path=str(args.output_dir / "macos-core-confirmation-en.png"), animations="disabled")
        dialog.get_by_role("button", name="Update Core", exact=True).click()
        page.wait_for_function("window.__appliedPlan?.[3]?.planId===window.__corePlan.id")
        expect(dialog).to_have_count(0)
        assert not errors, errors
        print("Bilingual macOS runtime notices and core update confirmation verified")
    finally:
        browser.close()
