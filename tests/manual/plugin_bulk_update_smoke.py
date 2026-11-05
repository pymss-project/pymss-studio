"""Exercise bulk plugin updates in the real Vue page with substituted native IPC."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1491")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-bulk-updates-"))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error"
                    and re.search(r"TypeError|ReferenceError|Unhandled error.*render", message.text) else None)
            page.add_init_script("""localStorage.setItem('pymss-studio:app-settings',JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
              themeMode:'light',animationsEnabled:false,pluginDefaultsPolicy:'review'
            }))""")
            page.goto(args.url.rstrip("/") + "/#/settings", wait_until="networkidle", timeout=60000)
            page.evaluate("""async () => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              window.__settings=globals.$pinia._s.get('settings');
              window.__app=globals.$pinia._s.get('app');
              const names=['pymss.analysis','pymss.timing','pymss.spectral','readonly.analysis'];
              const titles=['音频分析','音高与速度','均衡与滤波','目录插件'];
              window.__inventory={installed:names.map((name,index)=>({name,version:'1.0.0',
                managed:index!==3,loaded:true,enabled:true,prepared:true,source:'url',url:'https://github.com/example/plugins',
                path:'C:/plugins/'+name,contentDigest:name+'-before',toolCategory:'edit',
                locales:{'zh-CN':{title:titles[index]},en:{title:name}},operations:[],contributions:{views:[]}})),
                pluginsDir:'C:/plugins',runtime:{identity:'cpu',pythonPath:'C:/python.exe',bindingDir:'C:/plugins/cpu',
                updateSourceIdentity:'source',managementSupported:true},catalog:[],catalogError:null,
                catalogSource:{supported:true,provider:'github',gitAvailable:false,httpProviders:['github','gitcode']}};
              Object.assign(window.__inventory.installed[2],{loaded:false,error:'Plugin files are incomplete',errorKind:'load'});
              const callbacks=new Map(),listeners=new Map();let sequence=0,check=1;
              const emit=(type,payload,id)=>{for(const callback of listeners.values())callbacks.get(callback)?.({
                event:'pymss://worker-event',payload:{type,taskId:id,payload}})};
              window.__starts=[];
              window.__inventoryCalls=0;
              window.__finishUpdate=failure=>{
                const task=window.__heldUpdate;window.__heldUpdate=null;
                const inventory=window.__heldInventory;window.__heldInventory=null;
                if(failure)emit('error',{message:'Dependency is unavailable'},task.taskId);
                else {
                  const row=inventory.installed.find(row=>row.name===task.name);
                  row.version=task.expectedUpdate.remote_version;row.contentDigest+='-updated';
                  emit('plugin_manage_result',{...structuredClone(inventory),updates:{}},task.taskId);
                }
              };
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},
                unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin_marketplace'){window.__inventoryCalls++;return structuredClone(window.__inventory)}
                  if(command==='runtime_info')return window.__runtimeInfo?structuredClone(window.__runtimeInfo):null;
                  if(command==='list_nodes')return {nodes:[]};
                  if(command==='list_orphaned_editor_projects')return [];
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='get_build_info')return {version:'0.0.25',updateSupported:false};
                  if(command==='manage_plugin'){
                    const payload=JSON.parse(JSON.stringify(args.payload));
                    if(payload.action==='check_updates'){
                      const updates=Object.fromEntries(names.map((name,index)=>[name,{status:index===2?'source_changed':'update',
                        remote_version:(check+1)+'.0.0',target:{schemaVersion:1,candidate:{commit:'a'.repeat(40)}}}]));
                      check++;
                      setTimeout(()=>emit('plugin_manage_result',{...structuredClone(window.__inventory),updates},payload.taskId),30);
                    }else{
                      window.__starts.push(payload);window.__heldUpdate=payload;window.__heldInventory=window.__inventory;
                    }
                    return {taskId:payload.taskId};
                  }
                  return null;
                }};
              window.__market=(await import('/src/features/plugin-marketplace/composable.ts')).usePluginMarketplace();
              window.__market.snapshot.value=structuredClone(window.__inventory);window.__market.error.value='';
              window.__app.setWorkerEventConnection('connected');
              await globals.$router.push('/plugins');
            }""")
            broken = page.get_by_role("listitem", name="均衡与滤波", exact=True)
            expect(broken).to_contain_text("需要修复")
            summary = page.locator(".plugin-load-error > .plugin-error")
            details = page.locator(".plugin-error-details")
            expect(summary).to_have_text("插件无法正常加载，请查看错误详情。")
            expect(details.locator("pre")).not_to_be_visible()
            details.locator("summary").click()
            expect(details.locator("pre")).to_have_text("Plugin files are incomplete")
            expect(details.locator("pre")).to_be_visible()
            page.evaluate("window.__settings.locale='en'")
            expect(summary).to_have_text("The plugin could not be loaded. View the error details for more information.")
            expect(details.locator("summary")).to_have_text("Error details")
            expect(details.locator("pre")).to_have_text("Plugin files are incomplete")
            page.evaluate("window.__settings.locale='zh-CN'")
            expect(details.locator("summary")).to_have_text("错误详情")
            page.screenshot(path=str(output / "localized-plugin-error.png"), full_page=True, animations="disabled")
            details.locator("summary").click()
            page.evaluate("Object.assign(window.__market.snapshot.value.installed[2],{errorKind:'dependency',error:'Required package is missing'})")
            expect(broken.get_by_role("button", name="修复依赖", exact=True)).to_be_enabled()
            expect(broken.get_by_role("button", name="重新安装", exact=True)).to_have_count(0)
            page.evaluate("Object.assign(window.__market.snapshot.value.installed[2],{loaded:true,error:null,errorKind:null,viewError:'Requires another SDK',viewErrorKind:'compatibility'})")
            expect(broken).to_contain_text("已加载")
            expect(broken.get_by_role("button", name="重新安装", exact=True)).to_have_count(0)
            expect(broken.get_by_text("需要修复", exact=True)).to_have_count(0)
            page.evaluate("Object.assign(window.__market.snapshot.value.installed[2],{viewError:null,viewErrorKind:null},window.__inventory.installed[2])")
            broken.get_by_role("button", name="重新安装", exact=True).click()
            reinstall = page.locator(".plugin-update-modal")
            expect(reinstall).to_be_visible()
            page.screenshot(path=str(output / "reinstall-confirmation.png"), full_page=True, animations="disabled")
            reinstall.get_by_role("button", name="取消", exact=True).click()
            expect(reinstall).not_to_be_visible()
            assert page.evaluate("window.__starts.length") == 0
            page.get_by_role("button", name="运行环境与插件源", exact=True).click()
            page.locator(".n-popover .n-select").click()
            gitcode_option = page.locator(".n-base-select-option").filter(has_text="GitCode")
            expect(gitcode_option).to_have_count(1)
            expect(gitcode_option).not_to_have_class(re.compile("disabled"))
            page.get_by_role("heading", name="插件管理", exact=True).click()
            check = page.get_by_role("button", name="检查更新", exact=True)
            expect(check).to_be_enabled()
            check.click()
            update_all = page.get_by_role("button", name=re.compile(r"^全部更新"))
            expect(update_all).to_have_text("全部更新 (2)")
            page.get_by_placeholder("搜索名称、描述和标签").fill("音频分析")
            update_all.click()
            confirmation = page.locator(".plugin-update-modal").filter(has_text="将依次更新")
            expect(confirmation).to_contain_text("音高与速度")
            confirmation.locator("summary").click()
            expect(confirmation.get_by_role("checkbox")).not_to_be_checked()
            page.screenshot(path=str(output / "confirmation.png"), full_page=True)
            confirmation.get_by_role("button", name="全部更新", exact=True).click()
            expect(confirmation).not_to_be_visible()
            page.wait_for_function("window.__starts.length===1")
            page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings') }")
            page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/plugins') }")
            expect(page.locator(".plugins-batch-status")).to_contain_text("0/2")
            page.evaluate("window.__finishUpdate(true)")
            page.wait_for_function("window.__starts.length===2")
            page.evaluate("window.__finishUpdate(false)")
            page.wait_for_function("!window.__market.batchBusy.value")
            expect(page.locator(".plugins-batch-status")).to_contain_text("成功 1 · 失败 1")
            page.get_by_role("button", name="更新结果", exact=True).click()
            results = page.locator(".plugin-source-modal").filter(has=page.locator(".plugin-batch-results"))
            expect(results).to_contain_text("Dependency is unavailable")
            expect(results.locator(".plugin-batch-results > li")).to_have_count(2)
            for mode, width in [("light", 1280), ("dark", 840), ("dark", 760)]:
                page.evaluate("mode=>window.__settings.themeMode=mode", mode)
                page.set_viewport_size({"width": width, "height": 900})
                expect(results).to_be_visible()
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                page.screenshot(path=str(output / f"results-{mode}-{width}.png"), full_page=True, animations="disabled")
            page.set_viewport_size({"width": 1280, "height": 900})
            results.get_by_role("button", name="关闭", exact=True).click()
            expect(results).not_to_be_visible()
            check.click()
            expect(update_all).to_be_enabled()
            update_all.click()
            confirmation.get_by_role("button", name="全部更新", exact=True).click()
            page.wait_for_function("window.__starts.length===3")
            page.get_by_role("button", name="停止剩余更新", exact=True).click()
            expect(page.get_by_role("button", name="停止剩余更新", exact=True)).to_be_disabled()
            page.evaluate("window.__finishUpdate(false)")
            page.wait_for_function("!window.__market.batchBusy.value")
            assert page.evaluate("window.__starts.length") == 3
            expect(page.locator(".plugins-batch-status")).to_contain_text("已停止 1")
            check.click()
            expect(update_all).to_be_enabled()
            update_all.click()
            confirmation.get_by_role("button", name="全部更新", exact=True).click()
            page.wait_for_function("window.__starts.length===4")
            page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings') }")
            page.evaluate("""() => {
              window.__inventory={...window.__inventory,installed:[],pluginsDir:'C:/plugins/cuda',
                runtime:{identity:'cuda',pythonPath:'C:/cuda/python.exe',bindingDir:'C:/plugins/cuda',managementSupported:true}};
              const active={backend:'cuda',pythonPath:'C:/cuda/python.exe',health:'ready'};
              window.__runtimeInfo={platform:'win32',installedBackend:'cuda',installState:active,installedEnvironments:[active]};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("window.__market.snapshot.value===null&&window.__market.updateBatch.value.phase==='stopping'")
            page.evaluate("window.__finishUpdate(false)")
            page.wait_for_function("window.__market.snapshot.value?.runtime.identity==='cuda'&&!window.__market.loading.value")
            assert page.evaluate("window.__starts.length") == 4
            assert page.evaluate("window.__market.updateBatch.value.items[1].status") == "cancelled"
            page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/plugins') }")
            expect(page.get_by_role("listitem")).to_have_count(0)
            page.screenshot(path=str(output / "runtime-switch.png"), full_page=True, animations="disabled")
            before_core_update = page.evaluate("window.__inventoryCalls")
            page.evaluate("""() => {
              window.__runtimeInfo={...window.__runtimeInfo,pymssVersion:'2.3.5',
                installState:{...window.__runtimeInfo.installState,pymssVersion:'2.3.5'},
                installedEnvironments:window.__runtimeInfo.installedEnvironments.map(env=>({...env,pymssVersion:'2.3.5'}))};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("before=>window.__inventoryCalls>before&&!window.__market.loading.value", arg=before_core_update)
            assert page.evaluate("window.__market.snapshot.value.runtime.identity") == "cuda"
            page.evaluate("""() => {
              const invoke=window.__TAURI_INTERNALS__.invoke;
              window.__replacementQueries=0;window.__failInventory=true;
              window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
                if(command==='plugin_marketplace'){
                  window.__replacementQueries++;
                  if(window.__failInventory)throw new Error('Replacement runtime unavailable');
                }
                return invoke(command,args);
              };
              const active={backend:'cpu',pythonPath:'C:/unavailable/python.exe',health:'ready'};
              window.__inventory={...window.__inventory,runtime:{identity:'unavailable',pythonPath:active.pythonPath},installed:[]};
              window.__runtimeInfo={platform:'win32',installedBackend:'cpu',installState:active,installedEnvironments:[active]};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("window.__market.snapshot.value===null&&!window.__market.loading.value&&window.__market.error.value.includes('Replacement runtime unavailable')")
            before_recovery = page.evaluate("window.__replacementQueries")
            page.evaluate("""() => {
              window.__failInventory=false;
              const active={backend:'cuda',pythonPath:'C:/available/python.exe',health:'ready'};
              window.__inventory={...window.__inventory,runtime:{identity:'available',pythonPath:active.pythonPath},installed:[]};
              window.__runtimeInfo={platform:'win32',installedBackend:'cuda',installState:active,installedEnvironments:[active]};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("before=>window.__replacementQueries>before&&window.__market.snapshot.value?.runtime.identity==='available'&&!window.__market.loading.value", arg=before_recovery, timeout=5000)
            assert page.evaluate("window.__market.error.value") == ""
            page.evaluate("""async () => {
              const invoke=window.__TAURI_INTERNALS__.invoke;
              window.__catalogQueries=0;
              window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
                const result=await invoke(command,args);
                if(command!=='plugin_marketplace')return result;
                if(args?.payload?.catalogOnly){
                  window.__catalogQueries++;
                  return {...result,catalog:[{name:'available.example',version:'1.0.0',
                    url:'https://github.com/example/new-plugin',description:'Available plugin',tags:[],
                    locales:{'zh-CN':{title:'可安装插件'}}}],catalogError:null};
                }
                return {...result,catalog:[],catalogError:null};
              };
              await window.__market.openMarketplace(true);
            }""")
            expect(page.get_by_role("listitem", name="可安装插件", exact=True)).to_be_visible()
            before_catalog_refresh = page.evaluate("window.__catalogQueries")
            page.evaluate("""() => {
              window.__runtimeInfo={...window.__runtimeInfo,pymssVersion:'2.3.6',
                installState:{...window.__runtimeInfo.installState,pymssVersion:'2.3.6'},
                installedEnvironments:window.__runtimeInfo.installedEnvironments.map(env=>({...env,pymssVersion:'2.3.6'}))};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("before=>window.__catalogQueries>before&&!window.__market.loading.value&&!window.__market.catalogLoading.value", arg=before_catalog_refresh)
            expect(page.get_by_role("listitem", name="可安装插件", exact=True)).to_be_visible()
            before_background_refresh = page.evaluate("window.__inventoryCalls")
            background_catalog_queries = page.evaluate("window.__catalogQueries")
            page.evaluate("""async () => {
              await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/settings');
              const active={backend:'cpu',pythonPath:'C:/background/python.exe',health:'ready'};
              window.__inventory={...window.__inventory,runtime:{identity:'background',pythonPath:active.pythonPath},installed:[]};
              window.__runtimeInfo={platform:'win32',installedBackend:'cpu',installState:active,installedEnvironments:[active]};
              window.__app.runtimeInfo=structuredClone(window.__runtimeInfo);
            }""")
            page.wait_for_function("before=>window.__inventoryCalls>before&&window.__market.snapshot.value?.runtime.identity==='background'&&!window.__market.loading.value", arg=before_background_refresh)
            assert page.evaluate("window.__catalogQueries") == background_catalog_queries
            page.evaluate("async () => { await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/plugins') }")
            expect(page.get_by_role("listitem", name="可安装插件", exact=True)).to_be_visible()
            starts = page.evaluate("window.__starts")
            assert all(item["installDependencies"] is False for item in starts)
            assert all(item["expectedUpdate"]["target"]["schemaVersion"] == 1 for item in starts)
            assert not errors, errors
            print(f"PASS: GitCode without Git, manual reinstall, bulk updates, stop, runtime switching, failed-query recovery and visible catalog refresh; screenshots: {output}")
        except Exception:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            print(json.dumps(page.evaluate("""() => ({starts:window.__starts,
              batch:window.__market?.updateBatch.value,error:window.__market?.error.value,
              loading:window.__market?.loading.value,catalogLoading:window.__market?.catalogLoading.value,
              activities:window.__market?.activities.value})""")), flush=True)
            print(f"Browser errors: {errors}; screenshots: {output}", flush=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
