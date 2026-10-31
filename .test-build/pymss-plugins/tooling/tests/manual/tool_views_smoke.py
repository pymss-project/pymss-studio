"""Exercise component-based plugin forms through the public Studio SDK."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from file_tool_views_smoke import server_url

ROOT = Path(__file__).resolve().parents[3]
HOST = """({specs,url,dark}) => {
 const frame=document.createElement('iframe');frame.id='plugin';frame.sandbox='allow-scripts allow-forms';frame.style.cssText='border:0;width:100%;height:640px';
 const state=window.bridge={requests:[],held:[],holdDescriptions:false,rejectDescription:false,rejectNext:false,active:null};
 const sdk={sdkVersion:1,capabilities:[
  'app.info','files.pickAudio','files.reveal','operations.describe',
  'operations.start','operations.cancel','settings.read','settings.write','ui.overlay',
 ]};
 window.reply=(id,result)=>frame.contentWindow.postMessage({jsonrpc:'2.0',id,result},'*');
 window.notify=(method,params)=>frame.contentWindow.postMessage({jsonrpc:'2.0',method,params},'*');
 window.addEventListener('message',event=>{
  if(event.source!==frame.contentWindow||event.data?.jsonrpc!=='2.0')return;
  const data=event.data;state.requests.push(data);
  if(data.method==='pymss.ready'){
   notify('host.theme',{colorMode:dark?'dark':'light',cssVariables:dark?{'--surface':'#151a21','--surface-1':'#202732','--surface-2':'#252f3a','--on-surface':'#e0e7ef','--on-surface-muted':'#aab8c8','--outline':'#3b4757','--primary':'#2c9c89'}:{}});notify('host.ready',{});
  }else if(data.method==='pymss.resize'){if(!state.overlay)frame.style.height=data.params.height+'px'}
  else if(data.method==='pymss.overlay'){
    state.overlay=data.params.open;
    if(state.overlay){state.previousStyle=frame.style.cssText;frame.style.cssText='position:fixed;inset:0;z-index:3000;border:0;width:100vw;height:100vh'}
    else if(state.previousStyle)frame.style.cssText=state.previousStyle;
  }
  else if(data.method==='app.info')reply(data.id,{...sdk,locale:'zh-CN',defaultOutputDir:'/output'});
  else if(data.method==='settings.read')reply(data.id,{});
  else if(data.method==='settings.write')reply(data.id,data.params.data);
  else if(data.method==='operations.describe'){
   if(state.rejectDescription){state.rejectDescription=false;frame.contentWindow.postMessage({jsonrpc:'2.0',id:data.id,error:{message:'Operation is unavailable'}},'*')}
   else if(state.holdDescriptions)state.held.push(data);
   else reply(data.id,specs.find(spec=>spec.id===data.params.operation));
  }else if(data.method==='files.pickAudio')reply(data.id,{path:'/audio/source.wav'});
  else if(data.method==='files.reveal')reply(data.id,{ok:true});
  else if(data.method==='operations.start'){
   state.active=data;
   if(state.rejectNext){state.rejectNext=false;frame.contentWindow.postMessage({jsonrpc:'2.0',id:data.id,error:{message:'Input audio is unavailable'}},'*')}
  }else if(data.method==='operations.cancel'){
   reply(data.id,true);frame.contentWindow.postMessage({jsonrpc:'2.0',id:state.active.id,error:{message:'Cancelled',data:{code:'TASK_CANCELLED'}}},'*');
  }
 });document.body.style.margin='0';document.body.append(frame);frame.src=url;
}"""


def open_tool(browser, base, name, specs, *, dark=False, width=1100):
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.set_content("<html><body></body></html>")
    page.evaluate(
        HOST,
        {"specs": specs, "url": f"{base}/plugins/{name}/ui/dist/index.html", "dark": dark},
    )
    tool = page.frame_locator("#plugin")
    expect(tool.locator("#input-audio")).to_be_visible()
    return page, tool


def select(tool, selector, label):
    tool.locator(selector).click()
    tool.locator(".n-base-select-option").filter(
        has_text=re.compile("^" + re.escape(label) + "$")
    ).click()


def complete(page, outputs=None):
    page.evaluate(
        "outputs=>reply(bridge.active.id,{operation:bridge.active.params.operation,outputs:outputs||{report:{kind:'object',value:{ok:true}}},outputDir:'/output'})",
        outputs,
    )


def fill_audio(tool):
    tool.get_by_role("button", name="浏览", exact=True).click()
    expect(tool.locator("#input-audio input")).to_have_value("/audio/source.wav")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "tooling/.tmp/tool-views")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    from pymss.operations import create_runtime

    runtime = create_runtime()
    with patch.dict(os.environ, {"PYMSS_PLUGINS_DIR": str(ROOT / "plugins")}):
        reports = runtime.load_plugins()
    assert all(row["loaded"] for row in reports), reports
    specs = runtime.list_operations()
    tools = {
        name: json.loads(
            (ROOT / "plugins" / name / "ui/src/tool.json").read_text(encoding="utf-8")
        )
        for name in ["analysis", "dynamics", "spectral", "timing"]
    }
    passed = []
    with server_url(args.url) as base, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            for name, config in tools.items():
                page, tool = open_tool(browser, base, name, specs)
                try:
                    for entry in config["operations"]:
                        select(tool, "#operation", entry["title"]["zh"])
                        fill_audio(tool)
                        tool.locator("#run").click()
                        expect(tool.locator("#run")).to_be_disabled()
                        page.wait_for_function("bridge.active!==null")
                        request = page.evaluate("bridge.active")
                        assert request["params"]["operation"] == entry["id"], request
                        assert request["params"]["inputs"]["audio"] == {
                            "kind": "file",
                            "path": "/audio/source.wav",
                        }
                        complete(
                            page,
                            {
                                "report": {
                                    "kind": "scalar",
                                    "value": '<img src=x onerror="alert(1)">',
                                }
                            },
                        )
                        expect(tool.locator("#results")).to_be_visible()
                        expect(tool.locator("#results img")).to_have_count(0)
                        expect(tool.locator("#run")).to_be_enabled()
                        page.evaluate("bridge.active=null")
                        passed.append(entry["id"])
                    page.screenshot(
                        path=str(args.artifacts / f"{name}-components.png"),
                        full_page=True,
                    )
                finally:
                    page.close()
            page, tool = open_tool(browser, base, "timing", specs, dark=True, width=420)
            try:
                fill_audio(tool)
                page.evaluate("bridge.holdDescriptions=true")
                select(
                    tool, "#operation", tools["timing"]["operations"][1]["title"]["zh"]
                )
                page.wait_for_function("bridge.held.length===1")
                select(
                    tool, "#operation", tools["timing"]["operations"][0]["title"]["zh"]
                )
                page.wait_for_function("bridge.held.length===2")
                page.evaluate(
                    "specs=>{const a=bridge.held[0],b=bridge.held[1];reply(b.id,specs.find(s=>s.id===b.params.operation));reply(a.id,specs.find(s=>s.id===a.params.operation));bridge.holdDescriptions=false}",
                    specs,
                )
                expect(tool.locator("#param-n_steps")).to_be_visible()
                expect(tool.locator("#param-rate")).to_have_count(0)
                tool.locator("#run").click()
                page.wait_for_function("bridge.active!==null")
                page.evaluate(
                    "notify('task.progress',{id:bridge.active.id+1,fraction:.9});notify('task.progress',{id:bridge.active.id,fraction:.6})"
                )
                tool.locator(".tool-log-button").click()
                expect(tool.locator("#progress")).to_have_attribute(
                    "aria-valuenow", "60"
                )
                page.evaluate(
                    "notify('task.progress',{id:bridge.active.id,fraction:.2});notify('task.progress',{id:bridge.active.id,fraction:NaN})"
                )
                expect(tool.locator("#progress")).to_have_attribute(
                    "aria-valuenow", "60"
                )
                complete(
                    page,
                    {"audio": {"kind": "audio_file", "path": "/output/processed.wav"}},
                )
                page.keyboard.press("Escape")
                expect(tool.locator("#results")).to_contain_text(
                    "/output/processed.wav"
                )
                expect(tool.get_by_role("button", name="打开输出位置")).to_be_visible()
                page.evaluate("bridge.rejectNext=true")
                tool.locator("#run").click()
                expect(tool.locator("#error")).to_contain_text(
                    "Input audio is unavailable"
                )
                expect(tool.locator("#results")).to_have_count(0)
                page.evaluate("bridge.rejectDescription=true")
                select(
                    tool, "#operation", tools["timing"]["operations"][1]["title"]["zh"]
                )
                expect(tool.locator("#error")).to_contain_text(
                    "Operation is unavailable"
                )
                expect(tool.locator("#run")).to_be_disabled()
                tool.locator("#retry").click()
                expect(tool.locator("#param-rate")).to_be_visible()
                expect(tool.locator("#run")).to_be_enabled()
                tool.locator("#run").click()
                tool.locator(".tool-log-button").click()
                tool.locator("#cancel").click()
                expect(tool.locator("#status")).to_contain_text("取消")
                page.keyboard.press("Escape")
                assert page.frames[1].evaluate(
                    "document.documentElement.scrollWidth<=innerWidth"
                )
                page.screenshot(
                    path=str(args.artifacts / "timing-dark-narrow.png"), full_page=True
                )
                passed.extend(
                    [
                        "stale-metadata",
                        "progress-identity",
                        "monotonic-progress",
                        "audio-file-output",
                        "failure-retry",
                        "metadata-retry",
                        "cancel-terminal",
                        "narrow-layout",
                    ]
                )
            finally:
                page.close()
            custom = {
                "id": "pymss.analysis.measure_lufs",
                "inputs": {"audio": {"type": "audio"}},
                "outputs": {"report": {"type": "object"}},
                "params": {
                    "type": "object",
                    "required": ["enabled"],
                    "properties": {
                        "enabled": {"type": "boolean", "default": False},
                        "mode": {"type": "integer", "enum": [1, 2], "default": 2},
                        "count": {
                            "type": "integer",
                            "default": 4,
                            "minimum": 1,
                            "maximum": 8,
                        },
                    },
                },
            }
            page, tool = open_tool(
                browser,
                base,
                "analysis",
                [spec for spec in specs if spec["id"] != custom["id"]] + [custom],
            )
            try:
                fill_audio(tool)
                tool.locator("#param-count input").fill("1.5")
                tool.locator("#param-count input").blur()
                expect(tool.locator("#run")).to_be_disabled()
                tool.locator("#param-count input").fill("4")
                tool.locator("#param-count input").blur()
                tool.locator("#run").click()
                page.wait_for_function("bridge.active!==null")
                assert page.evaluate("bridge.active.params.params") == {
                    "enabled": False,
                    "mode": 2,
                    "count": 4,
                }
                complete(page)
                passed.append("typed-parameters")
            finally:
                page.close()
        finally:
            browser.close()
    (args.artifacts / "results.json").write_text(
        json.dumps({"passed": passed}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Component tool view smoke passed: {len(passed)} checks")


if __name__ == "__main__":
    main()
