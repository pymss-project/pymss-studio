"""Check real Studio plugin frames, log overlays and analysis presentation.

Only native IPC is substituted. Plugin HTML comes from the sibling checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import tomllib
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check_log_overlay(page, tool, output, name):
    tool.locator(".tool-log-button").scroll_into_view_if_needed()
    page.evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
    before = tool.locator(".audio-tool-layout").bounding_box()
    tool.locator("html").evaluate("""(_element, before) => {
      window.__overlayFrames=[];window.__captureOverlay=true;
      const normalWidth=innerWidth;
      const sample=()=>{
        if(!window.__captureOverlay)return;
        const layout=document.querySelector('.audio-tool-layout'), box=layout.getBoundingClientRect();
        const inline=Math.abs(innerWidth-normalWidth)<2;
        window.__overlayFrames.push({visibility:getComputedStyle(layout).visibility,
          x:box.x+(inline?before.x:0),y:box.y+(inline?before.y:0),width:box.width,height:box.height});
        requestAnimationFrame(sample);
      };sample();
    }""", before)
    tool.locator(".tool-log-button").click()
    expect(tool.locator("html")).to_have_class(re.compile("tool-overlay-anchored"))
    expect(tool.locator(".tool-activity-dialog")).to_be_visible()
    expect(tool.locator(".audio-tool-layout")).to_be_visible()
    after = tool.locator(".audio-tool-layout").bounding_box()
    for key in ["x", "y", "width", "height"]:
        assert abs(before[key] - after[key]) < 2, (name, before, after)
    tool.locator(".tool-activity-dialog").evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
    page.screenshot(path=str(output / f"logs-{name}.png"), full_page=True)
    tool.locator("body").press("Escape")
    expect(tool.locator(".tool-activity-dialog")).not_to_be_visible()
    expect(page.locator(".plugin-view-frame--overlay")).to_have_count(0)
    expect(tool.locator(".tool-log-button")).to_be_enabled()
    frames = tool.locator("html").evaluate("""async () => {
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      window.__captureOverlay=false;return window.__overlayFrames;
    }""")
    (output / f"log-layout-{name}.json").write_text(json.dumps({"before": before, "frames": frames}, indent=2), encoding="utf-8")
    assert frames and all(frame["visibility"] == "visible" for frame in frames), (name, frames)
    for frame in frames:
        for key in ["x", "y", "width", "height"]:
            assert abs(before[key] - frame[key]) < 2, (name, before, frame)


def check_management_confirmation(page, output):
    page.evaluate("""async () => {
      window.__holdPluginManagement=true;
      await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/plugins');
    }""")
    row = page.get_by_role("listitem", name="音频分析", exact=True)
    for outcome in ["failed", "cancelled", "succeeded"]:
        row.scroll_into_view_if_needed()
        page.evaluate("async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame) }")
        row.locator("h2").click(button="right")
        page.get_by_role("menuitem", name="修复依赖", exact=True).click()
        confirm = page.locator(".n-dialog")
        expect(confirm).to_contain_text("是否继续")
        assert page.evaluate("!window.__heldPluginManagement")
        confirm.get_by_role("button", name="修复依赖", exact=True).click()
        expect(confirm).not_to_be_visible()
        status = page.get_by_role("region", name="插件任务状态", exact=True)
        expect(status).to_be_visible()
        expect(status).to_contain_text("修复依赖")
        expect(status.get_by_role("button", name="取消", exact=True)).to_be_enabled()
        assert page.evaluate("window.__heldPluginManagement?.action==='repair'")
        page.keyboard.press("Escape")
        expect(status).not_to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^日志.*1 项运行中"))).to_be_visible()
        running = row.locator(".plugin-running-task")
        expect(running).to_be_enabled()
        if outcome == "failed":
            for theme, width in [("light", 1280), ("dark", 840)]:
                page.evaluate("theme => window.__settings.themeMode=theme", theme)
                page.set_viewport_size({"width": width, "height": 900})
                page.evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                page.screenshot(path=str(output / f"task-running-{theme}.png"), full_page=True)
            page.evaluate("window.__settings.themeMode='light'")
            page.set_viewport_size({"width": 1280, "height": 900})
        running.click()
        expect(status).to_be_visible()
        if outcome == "cancelled":
            status.get_by_role("button", name="取消", exact=True).click()
            expect(status).to_contain_text("已取消")
        else:
            page.evaluate("failure => window.__finishPluginManagement(failure)", outcome == "failed")
            expect(status).to_contain_text("操作失败，请查看日志" if outcome == "failed" else "已完成")
        page.evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
        page.screenshot(path=str(output / f"repair-{outcome}.png"), full_page=True)
        page.keyboard.press("Escape")
        expect(status).not_to_be_visible()
        expect(page.get_by_role("button", name="从来源安装", exact=True)).to_be_enabled()
    page.evaluate("window.__holdPluginManagement=false")
    page.get_by_role("button", name="日志", exact=True).click()
    page.locator(".plugin-log-body .n-select").click()
    failed = page.locator(".n-base-select-option").filter(has_text="操作失败，请查看日志")
    expect(failed).to_have_count(1)
    failed.click()
    expect(page.get_by_role("region", name="插件任务状态", exact=True)).to_contain_text("操作失败，请查看日志")
    expect(page.locator(".plugin-log-body .log-console")).to_contain_text("Dependency repair failed")
    page.keyboard.press("Escape")


def check_source_preferences(page, output):
    page.evaluate("""() => {
      window.__customCatalog=true;window.__catalogReads=0;
      const invoke=window.__TAURI_INTERNALS__.invoke;
      window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
        if(command!=='plugin_marketplace')return invoke(command,args);
        window.__catalogReads++;
        if(window.__catalogSourceOffline&&args?.payload?.catalogOnly)throw new Error('Catalog offline');
        return {...await invoke(command,args),catalogSource:{supported:true,provider:window.__customCatalog?'custom':window.__settings.pluginSource==='gitcode'?'gitcode':'github',gitAvailable:true}};
      };
      window.__market.snapshot.value={...window.__market.snapshot.value,catalogSource:{supported:true,provider:'custom',gitAvailable:true}};
      window.__market.updates.value={analysis:{status:'latest'}};
    }""")
    page.get_by_role("button", name="运行环境与插件源", exact=True).click()
    preferences = page.locator(".plugins-runtime-details")
    expect(preferences.locator(".plugins-effective-source")).to_have_text("当前插件源：自定义源")
    preferences.locator(".n-select").click()
    page.locator(".n-base-select-option").filter(has_text=re.compile("^GitHub$")).click()
    page.wait_for_function("window.__settings.pluginSource==='github'")
    expect(preferences.locator(".n-select")).not_to_have_class(re.compile("n-select--disabled"))
    assert page.evaluate("window.__catalogReads===0&&window.__market.updates.value.analysis.status==='latest'")
    expect(preferences).to_contain_text("退出自定义源后")
    page.evaluate("window.__customCatalog=false;window.__market.snapshot.value.catalogSource.provider='github'")
    preferences.locator(".n-select").click()
    page.locator(".n-base-select-option").filter(has_text=re.compile("^GitCode$")).click()
    expect(preferences.locator(".plugins-effective-source")).to_have_text("当前插件源：GitCode")
    assert page.evaluate("window.__catalogReads>0&&!window.__market.updates.value.analysis")
    page.get_by_role("heading", name="插件管理", exact=True).click()
    page.evaluate("window.__catalogSourceOffline=true")
    page.get_by_role("button", name="更多市场操作", exact=True).click()
    page.get_by_text("刷新目录", exact=True).click()
    expect(page.locator(".plugins-catalog-error")).to_be_visible()
    page.get_by_role("button", name="运行环境与插件源", exact=True).click()
    expect(preferences.locator(".plugins-effective-source")).to_have_text("上次确认来源：GitCode")
    expect(preferences.locator(".n-select")).to_contain_text("GitCode")
    expect(preferences).not_to_contain_text("自动比较")
    page.evaluate("async () => { await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(()=>{}))) }")
    page.screenshot(path=str(output / "source-preference.png"), full_page=True)
    page.get_by_role("heading", name="插件管理", exact=True).click()


def check_tool_transitions(page, tool, output, restore_animations):
    nav = page.locator(".audio-tools-nav")
    checks = []
    for enabled, reduced in [(True, False), (False, False), (True, True)]:
        page.evaluate("value => window.__settings.animationsEnabled=value", enabled)
        page.emulate_media(reduced_motion="reduce" if reduced else "no-preference")
        page.evaluate("""() => {
          window.__toolFrames=[];window.__captureTools=true;
          const sample=()=>{
            if(!window.__captureTools)return;
            const frames=[...document.querySelectorAll('.plugin-view-frame')];
            const panels=[...document.querySelectorAll('.audio-tool-panel')];
            window.__toolFrames.push({frames:frames.length,
              fading:[...frames.filter(f=>f.classList.contains('plugin-view-frame--loaded')),...panels].some(el=>{const opacity=Number(getComputedStyle(el).opacity);return opacity>0&&opacity<1}),
              leaving:panels.some(el=>el.classList.contains('tool-panel-leave-active')&&el.inert)});
            requestAnimationFrame(sample);
          };sample();
        }""")
        nav.get_by_role("button", name="音频分析", exact=True).click()
        expect(tool.get_by_role("heading", name="音频分析", exact=True)).to_be_visible()
        expect(page.locator(".plugin-view-frame")).to_have_class(re.compile("plugin-view-frame--loaded"))
        page.evaluate("async () => { await Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{}))); await new Promise(requestAnimationFrame);window.__captureTools=false }")
        frames = page.evaluate("window.__toolFrames")
        assert frames and max(frame["frames"] for frame in frames) <= 1
        checks.append({"animations": enabled, "reducedMotion": reduced, "fading": any(frame["fading"] for frame in frames), "maxFrames": max(frame["frames"] for frame in frames)})
        if enabled and not reduced:
            assert any(frame["fading"] for frame in frames), frames
            assert any(frame["leaving"] for frame in frames), frames
        else:
            assert not any(frame["fading"] for frame in frames), frames
        nav.get_by_role("button", name="音频格式转换", exact=True).click()
        expect(tool.get_by_role("heading", name="音频格式转换", exact=True)).to_be_visible()
        page.evaluate("async () => { await Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{}))) }")
    page.emulate_media(reduced_motion="no-preference")
    page.evaluate("value => window.__settings.animationsEnabled=value", restore_animations)
    page.evaluate("""async () => {
      const select=title=>[...document.querySelectorAll('.audio-tools-nav button')].find(button=>button.textContent.trim()===title).click();
      select('音频分析');await new Promise(requestAnimationFrame);
      select('音频格式转换');await new Promise(requestAnimationFrame);
      select('频谱分析');
    }""")
    expect(tool.get_by_role("heading", name="频谱分析", exact=True)).to_be_visible()
    expect(page.locator(".plugin-view-frame")).to_have_count(1)
    nav.get_by_role("button", name="音频分析", exact=True).click()
    tool.get_by_role("button", name="浏览", exact=True).click()
    expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
    page.wait_for_function("JSON.stringify(window.__pluginSettings.get('analysis/tool')).includes('C:/audio/source.wav')")
    nav.get_by_role("button", name="音频格式转换", exact=True).click()
    expect(tool.get_by_role("heading", name="音频格式转换", exact=True)).to_be_visible()
    nav.get_by_role("button", name="音频分析", exact=True).click()
    expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
    page.evaluate("window.__holdOperation=true;window.__busyFrame=document.querySelector('.plugin-view-frame')")
    tool.locator("#run").click()
    expect(nav.get_by_role("button", name="音频格式转换", exact=True)).to_be_disabled()
    page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/plugins');await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/tools?pluginView=analysis/tool') }")
    expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
    assert page.evaluate("window.__busyFrame===document.querySelector('.plugin-view-frame')")
    page.evaluate("window.__holdOperation=false;window.__finishHeldOperation()")
    expect(tool.locator("#run")).to_be_enabled()
    nav.get_by_role("button", name="音频格式转换", exact=True).click()
    expect(tool.get_by_role("heading", name="音频格式转换", exact=True)).to_be_visible()
    (output / "tool-transitions.json").write_text(json.dumps({"checks": checks, "rapidSwitch": True, "cachedInputs": True, "runningFrameRetained": True}), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1564")
    parser.add_argument("--plugins", type=Path, default=Path(__file__).resolve().parents[3] / "pymss-plugins")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--animations", action="store_true")
    parser.add_argument("--stress-routing", action="store_true")
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
            def console_error(message):
                if message.type == "error" and re.search(r"TypeError|ReferenceError|Unhandled error.*render", message.text):
                    errors.append(message.text)
            page.on("console", console_error)
            page.add_init_script("""if(window===window.top)localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
              themeMode:'light',animationsEnabled:false
            }))""".replace("animationsEnabled:false", f"animationsEnabled:{str(args.animations).lower()}"))
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
              const managementEvent=(type,payload,taskId)=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:{type,taskId,payload}})};
              window.__finishPluginManagement=failure=>{
                const task=window.__heldPluginManagement;window.__heldPluginManagement=null;
                managementEvent(failure?'error':'plugin_manage_result',failure?{message:'Dependency repair failed'}:structuredClone(window.__inventory),task.taskId);
              };
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__descriptions=[];window.__descriptionRequests=[];window.__heldDescriptions=[];window.__operationStarts=[];window.__definitionVersion=1;window.__pluginSettings=new Map();
              const describe=operation=>({id:operation,specDigest:(window.__definitionVersion===1?'a':'b').repeat(64),inputs:{audio:{type:'audio'}},
                params:{type:'object',properties:operation.endsWith('.spectrum')?{n_fft:{type:'integer',default:window.__definitionVersion===1?2048:4096,minimum:64,maximum:32768}}:{}},outputs:{report:{type:'object'}}});
              window.__releaseDescriptions=()=>{window.__holdDescriptions=false;window.__heldDescriptions.splice(0).forEach(({resolve,operation})=>resolve(describe(operation)))};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                convertFileSrc:(path,protocol)=>`http://${protocol}.localhost/${path}`,
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},
                unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin_marketplace')return structuredClone(window.__inventory);
                  if(command==='list_nodes')return {nodes:[]};
                  if(command==='list_orphaned_editor_projects')return [];
                  if(command==='manage_plugin'&&window.__holdPluginManagement){
                    window.__heldPluginManagement=structuredClone(args.payload);
                    return {taskId:args.payload.taskId};
                  }
                  if(command==='cancel_task'&&window.__heldPluginManagement?.taskId===args.taskId){
                    window.__heldPluginManagement=null;
                    managementEvent('task_cancelled',{},args.taskId);return true;
                  }
                  if(command==='manage_plugin'){
                    window.__pluginInstalls=(window.__pluginInstalls||0)+1;
                    window.__inventory.installed.push({name:'local-'+window.__pluginInstalls,managed:true,loaded:true,enabled:true,contributions:{views:[]}});
                    setTimeout(()=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:{type:'plugin_manage_result',taskId:args.payload.taskId,payload:structuredClone(window.__inventory)}})},40);
                    return {taskId:args.payload.taskId};
                  }
                  if(command==='get_build_info')return {version:'0.0.23',updateSupported:false};
                  if(command==='load_plugin_tool_settings')return structuredClone(window.__pluginSettings.get(args.pluginName+'/'+args.viewId)||{});
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='load_app_store')return null;
                  if(command==='save_plugin_tool_settings'){window.__pluginSettings.set(args.pluginName+'/'+args.viewId,structuredClone(args.data));return args.data;}
                  if(command==='describe_operation'){
                    window.__descriptionRequests.push(structuredClone(args.payload));
                    const operation=args.payload.operation;window.__descriptions.push(operation);
                    if(window.__holdDescriptions)return new Promise(resolve=>window.__heldDescriptions.push({resolve,operation}));
                    return describe(operation);
                  }
                  if(command==='plugin:dialog|open')return 'C:/audio/source.wav';
                  if(command==='start_operation_task'){
                    window.__operationStarts.push(structuredClone(args.payload));
                    if(window.__changeDefinitionOnRun){
                      window.__changeDefinitionOnRun=false;window.__definitionVersion=2;
                      setTimeout(()=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:{type:'error',requestId:args.payload.requestId,taskId:args.payload.requestId,payload:{code:'OPERATION_SPEC_CHANGED',message:'The operation definition changed.',recoverable:true}}})},40);
                      return {taskId:args.payload.requestId,started:true};
                    }
                    const operation=args.payload.operation;
                    const value=operation.endsWith('.spectrum')?{freqs:[0,1000,2000,3000,4000],magnitudes_db:[-80,-12,-50,-60,-80]}:
                      operation.endsWith('.measure_lufs')?{lufs:'-Infinity'}:{sample_peak_dbfs:-27.467469,true_peak_dbtp:-27.461275};
                    const payload={operation,outputs:{report:{kind:'object',value}}};
                    const complete=()=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:{type:'operation_result',requestId:args.payload.requestId,taskId:args.payload.requestId,payload}})};
                    if(window.__holdOperation)window.__finishHeldOperation=complete;else setTimeout(complete,40);
                    return {taskId:args.payload.requestId,started:true};
                  }
                  return null;
                }};
              const market=(await import('/src/features/plugin-marketplace/composable.ts')).usePluginMarketplace();
              window.__market=market;
              market.snapshot.value=structuredClone(window.__inventory);
              market.error.value='';
              window.__app.setWorkerEventConnection('connected');
              await globals.$router.push('/tools?pluginView=convert/tool');
            }""", {"rows": rows})
            tool = page.frame_locator(".plugin-view-frame")
            expect(tool.locator(".tool-log-button")).to_be_visible()
            try:
                check_tool_transitions(page, tool, output, args.animations)
            except Exception:
                print("BROWSER_ERRORS", errors)
                print("TOOL_STATE", page.evaluate("({content:document.querySelector('.audio-tools-content')?.innerHTML.slice(0,2500),frames:window.__toolFrames?.slice(0,12)})"))
                page.screenshot(path=str(output / "transition-failure.png"), full_page=True)
                raise
            if args.stress_routing:
                route = "async(path)=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(path)"
                page.evaluate(route, '/plugins')
                for index in range(2):
                    page.get_by_role('button', name='从来源安装', exact=True).click()
                    source = page.locator('.plugin-source-modal')
                    expect(source).to_be_visible()
                    source.locator('input').fill('/test/plugin-' + str(index))
                    source.get_by_role('button', name='安装', exact=True).click()
                    page.locator('.n-dialog').get_by_role('button', name='安装', exact=True).click()
                    expect(source).not_to_be_visible()
                    page.wait_for_function('!window.__market.busy.value')
                page.get_by_role('button', name='从来源安装', exact=True).click()
                expect(page.locator('.plugin-source-modal')).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('.plugin-source-modal')).not_to_be_visible()
                for animations in [True, False]:
                    page.evaluate('value=>window.__settings.animationsEnabled=value', animations)
                    for _ in range(5):
                        for mode in ['dark', 'light']:
                            page.evaluate(route, '/settings')
                            page.locator('.theme-switcher').get_by_role('button', name='深色' if mode == 'dark' else '浅色', exact=True).click()
                            page.evaluate(route, '/workflows')
                            expect(page.locator('.workflows-page')).to_be_visible()
                            page.evaluate(route, '/tools?pluginView=convert/tool')
                            expect(page.locator('.workflows-page')).to_have_count(0)
                            expect(tool.locator('html')).to_have_attribute('data-mode', mode)
                            expect(tool.locator('.n-input').first).to_have_css('--n-color', '#23272e' if mode == 'dark' else '#eef2f7')
                            tool.locator('.tool-log-button').click()
                            expect(tool.locator('.tool-activity-dialog')).to_be_visible()
                            tool.locator('body').press('Escape')
                            expect(tool.locator('.tool-activity-dialog')).not_to_be_visible()
                page.screenshot(path=str(output / 'repeated-route-theme-dialogs.png'), full_page=True)
            for mode in ["light", "dark"]:
                if args.animations:
                    page.evaluate("async()=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings')")
                    button = page.locator('.theme-switcher').get_by_role('button', name='深色' if mode == 'dark' else '浅色', exact=True)
                    expect(button).to_be_visible()
                    button.click()
                    page.evaluate("async()=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/tools?pluginView=convert/tool')")
                else:
                    page.evaluate("mode=>window.__settings.themeMode=mode", mode)
                expect(tool.locator("html")).to_have_attribute("data-mode", mode)
                expect(page.locator(".plugin-view-frame")).to_have_css("color-scheme", mode)
                expect(tool.locator("html")).to_have_css("color-scheme", mode)
                expect(tool.locator('.n-input').first).to_have_css('--n-color', '#23272e' if mode == 'dark' else '#eef2f7')
                check_log_overlay(page, tool, output, mode)
                viewport = page.viewport_size
                page.set_viewport_size({"width": 1100, "height": viewport["height"]})
                page.evaluate("async () => { await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))) }")
                check_log_overlay(page, tool, output, mode + "-medium")
                page.set_viewport_size(viewport)
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
            check_log_overlay(page, tool, output, "spectrum")
            viewport = page.viewport_size
            tool.locator(".tool-log-button").click()
            expect(tool.locator(".tool-activity-dialog")).to_be_visible()
            page.set_viewport_size({"width": 1100, "height": 800})
            tool.locator("body").press("Escape")
            expect(page.locator(".plugin-view-frame--overlay")).to_have_count(0)
            expect(tool.locator("html")).not_to_have_class(re.compile("tool-overlay-anchored"))
            tool.locator(".tool-log-button").click()
            expect(tool.locator(".tool-activity-dialog")).to_be_visible()
            tool.locator("body").press("Escape")
            expect(page.locator(".plugin-view-frame--overlay")).to_have_count(0)
            page.set_viewport_size(viewport)
            tool.get_by_role("button", name="浏览", exact=True).click()
            field = tool.locator("#param-n_fft input")
            field.fill("1536")
            field.press("Tab")
            page.evaluate("window.__changeDefinitionOnRun=true")
            count = page.evaluate("window.__operationStarts.length")
            tool.locator("#run").click()
            expect(tool.locator("#error")).to_contain_text("请核对刷新后的参数")
            expect(field).to_have_value("4096")
            expect(tool.locator("#input-audio input")).to_have_value("C:/audio/source.wav")
            assert page.evaluate("window.__operationStarts.length") == count + 1
            assert page.evaluate("window.__operationStarts.at(-1).expectedSpecDigest") == "a" * 64
            assert page.evaluate("window.__descriptionRequests.at(-1).refresh") is True
            tool.locator("#run").click()
            expect(tool.locator(".analysis-spectrum polyline")).to_have_attribute("points", re.compile(r"\d"))
            assert page.evaluate("window.__operationStarts.at(-1).expectedSpecDigest") == "b" * 64
            expect(tool.locator(".analysis-hint")).to_contain_text("FFT")
            spectrum = tool.locator(".analysis-spectrum")
            spectrum.scroll_into_view_if_needed()
            for fraction, frequency, magnitude in [(0.25, "1,000 Hz", "-12 dB"), (0.75, "3,000 Hz", "-60 dB")]:
                bounds = spectrum.bounding_box()
                spectrum.hover(position={"x": bounds["width"] * (50 + 700 * fraction) / 780, "y": bounds["height"] * 100 / 270})
                tooltip = tool.locator(".analysis-spectrum-tooltip")
                expect(tooltip.locator("dd").nth(0)).to_have_text(frequency)
                expect(tooltip.locator("dd").nth(1)).to_have_text(magnitude)
                expect(tool.locator(".analysis-spectrum-cursor circle")).to_be_visible()
            page.screenshot(path=str(output / "spectrum.png"), full_page=True)
            spectrum.hover(position={"x": 5, "y": 5})
            expect(tool.locator(".analysis-spectrum-tooltip")).to_have_count(0)
            check_management_confirmation(page, output)
            check_source_preferences(page, output)
            assert not errors, errors
            print(f"Studio log overlays, cached forms, measurement results and spectrum verified: {output}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
