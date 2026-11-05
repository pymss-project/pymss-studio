"""Exercise model categories and ordering in Vue with substituted native IPC."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1495")
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    names = ["mvsep_mega_model_bs_roformer_53_stems_v1.ckpt", "bs_mega_53stem_bass_mvsep.ckpt",
             "model_scnet_ep_36_sdr_5.4596.ckpt", "gilliaan_drumsV1.ckpt"]
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))["models"]
    rows = []
    for item in catalog:
        rows.append({
            "name": item["name"], "aliases": item["aliases"], "modelType": item["model_type"],
            "architecture": item["architecture"], "supported": True, "unsupportedReason": "",
            "category": item["primary_category"] + "/" + item["secondary_category"],
            "categoryCn": item["primary_category_cn"] + " / " + item["secondary_category_cn"],
            "primaryCategory": item["primary_category"], "primaryCategoryCn": item["primary_category_cn"],
            "secondaryCategory": item["secondary_category"], "secondaryCategoryCn": item["secondary_category_cn"],
            "sortKey": item.get("sort_key", ""), "targetStem": item["target_stem"],
            "sizeBytes": item["size_bytes"], "downloaded": item["name"] in names, "missingPaths": [],
            "modelPath": "C:/models/" + item["relpath"], "configPath": "C:/models/" + item["config_relpath"],
            "auxiliaryPaths": [], "source": "catalog", "configInstruments": "", "configTargetInstrument": "",
        })
    output = Path(tempfile.mkdtemp(prefix="studio-model-categories-"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""localStorage.setItem('pymss-studio:app-settings',JSON.stringify({
          startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',
          themeMode:'light',animationsEnabled:false
        }))""")
        try:
            page.goto(args.url.rstrip("/") + "/#/settings", wait_until="networkidle", timeout=60000)
            page.evaluate("""async rows => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              const model=(await import('/src/stores/model.ts')).useModelStore();
              window.__model=model;window.__settings=globals.$pinia._s.get('settings');
              window.__app=globals.$pinia._s.get('app');window.__router=globals.$router;
              window.__rows=rows;
              const callbacks=new Map(),listeners=new Map();let sequence=0;
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='list_models')return {models:rows,categories:[],categoriesCn:[],modelDir:'C:/models'};
                  if(command==='get_model_info')return rows.find(row=>row.name===args?.payload?.model)||rows[0];
                  if(command==='list_nodes')return {nodes:[]};
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='list_orphaned_editor_projects')return [];
                  if(command==='get_build_info')return {version:'0.0.25',updateSupported:false};
                  if(command==='plugin_marketplace')return {installed:[],catalog:[],catalogError:null,pluginsDir:'C:/plugins',
                    runtime:{identity:'cpu',pythonPath:'C:/python.exe',managementSupported:false}};
                  return null;
                }};
              model.models=rows;model.modelsLoaded=true;model.isLoading=false;model.modelViewMode='list';
              model.category='';model.search='';model.modelSource='all';model.modelPreferences={};
              window.__app.setWorkerEventConnection('connected');
              await globals.$router.push('/models');
            }""", rows)
            cards = page.locator(".model-card")
            page.locator(".toolbar-actions .n-switch").click()
            expect(cards).to_have_count(4)
            expect(page.locator(".mc-meta-item--category").filter(has_text="人声 › 四部和声分离")).to_be_visible()
            page.locator(".category-select").click()
            menu = page.locator(".model-category-menu")
            expect(menu).to_be_visible()
            expect(menu.get_by_text("全部音乐多轨/分轨", exact=True)).to_have_count(0)
            menu.get_by_text("音乐多轨/分轨", exact=True).click()
            expect(cards).to_have_count(2)
            expect(cards.first.locator(".mc-name")).to_have_attribute("title", names[0])
            page.locator(".category-select").click()
            music = menu.locator(".n-tree-node").filter(has=page.get_by_text("音乐多轨/分轨", exact=True))
            music.locator(".n-tree-node-switcher").click()
            expect(menu.get_by_text("MVSep 53 轨分离", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "models-category-tree.png"), full_page=True, animations="disabled")
            menu.get_by_text("MVSep 53 轨分离", exact=True).click()
            expect(page.locator(".category-select")).to_contain_text("音乐多轨/分轨 › MVSep 53 轨分离")
            expect(cards).to_have_count(2)
            page.locator(".sort-select").click()
            page.get_by_text("名称：A 到 Z", exact=True).click()
            expect(cards.first.locator(".mc-name")).to_have_attribute("title", names[1])
            page.screenshot(path=str(output / "models-category.png"), full_page=True, animations="disabled")
            page.evaluate("async () => {window.__model.category='';await window.__router.push('/')}")
            picker = page.locator(".target-list--models .target-row")
            expect(picker).to_have_count(4)
            expect(picker.filter(has_text="四部和声分离")).to_be_visible()
            page.locator(".target-toolbar__filter").click()
            menu.get_by_text("音乐多轨/分轨", exact=True).click()
            expect(picker).to_have_count(2)
            expect(picker.first.locator(".target-row__name")).to_have_attribute("title", names[0])
            page.locator(".target-toolbar__filter").click()
            music.locator(".n-tree-node-switcher").click()
            menu.get_by_text("MVSep 53 轨分离", exact=True).click()
            expect(picker).to_have_count(2)
            expect(page.locator(".target-toolbar__filter")).to_contain_text("音乐多轨/分轨 › MVSep 53 轨分离")
            page.screenshot(path=str(output / "picker-category.png"), full_page=True, animations="disabled")
            page.evaluate("""async () => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              globals.$i18n.locale='en';window.__settings.locale='en';
              await window.__router.push('/models');window.__model.category='vocal/choir_vocal';
            }""")
            page.locator(".toolbar-actions .n-switch").click()
            expect(cards).to_have_count(1)
            expect(page.locator(".mc-meta-item--category")).to_contain_text("vocal › choir_vocal")
            page.set_viewport_size({"width": 840, "height": 900})
            page.screenshot(path=str(output / "models-en-narrow.png"), full_page=True, animations="disabled")
            page.locator(".category-select").click()
            expect(menu).to_be_visible()
            bounds = menu.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 841
            menu.get_by_text("All", exact=True).click()
            expect(menu).not_to_be_visible()
            expect(cards).to_have_count(4)
            page.evaluate("window.__settings.themeMode='dark';window.__settings.themeAccent='pink'")
            page.wait_for_function("getComputedStyle(document.documentElement).getPropertyValue('--primary').trim()==='#d084ad'")
            page.locator(".category-select").click()
            expect(menu).to_be_visible()
            expect(menu).to_have_css("opacity", "1")
            page.screenshot(path=str(output / "models-tree-dark-narrow.png"), full_page=True, animations="disabled")
            page.keyboard.press("Escape")
            expect(menu).not_to_be_visible()
            page.evaluate("""async () => {
              await window.__router.push('/settings');window.__model.category='removed/old-category';
              await window.__router.push('/models');
            }""")
            page.locator(".toolbar-actions .n-switch").click()
            expect(cards).to_have_count(4)
            assert page.evaluate("window.__model.category") == ""
            assert not errors, errors
            print(f"PASS: expandable parent/child filters, both model pages, catalog ordering, explicit ordering, locale fallback and dark narrow menu; screenshots: {output}")
        except Exception:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            print(f"Browser errors: {errors}; screenshots: {output}", flush=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
