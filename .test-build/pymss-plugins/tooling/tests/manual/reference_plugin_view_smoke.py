"""Exercise the standalone reference page against the shipped Studio SDK."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def serve(directory):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ui/dist/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


HOST = """url => {
 const frame=document.createElement('iframe');frame.id='tool';frame.sandbox='allow-scripts allow-forms';
 frame.style.cssText='width:100%;height:640px;border:0;display:block';
 window.bridge={requests:[],active:null,locale:'zh-CN',mode:'light',rejectSave:false,rejectCancel:false,preferences:{gain_db:-3,protect_peak:false}};
 const send=data=>frame.contentWindow.postMessage(data,'*');
 window.reply=(id,result)=>send({jsonrpc:'2.0',id,result});
 window.fail=(id,message,code)=>send({jsonrpc:'2.0',id,error:{code:-32000,message,data:code?{code}:{}}});
 window.progress=(fraction,message='processing')=>send({jsonrpc:'2.0',method:'task.progress',params:{id:bridge.active.id,fraction,message}});
 window.pushTheme=()=>send({jsonrpc:'2.0',method:'host.theme',params:{locale:bridge.locale,colorMode:bridge.mode,fontFamily:'Arial, sans-serif',componentTheme:{common:{primaryColor:'#258c7b'}},cssVariables:{'--font-sans':'Arial, sans-serif','--surface-1':bridge.mode==='dark'?'#191d25':'#fafbff','--outline':bridge.mode==='dark'?'#333b49':'#dcdfe8','--on-surface':bridge.mode==='dark'?'#e2e8f3':'#1d283b','--on-surface-muted':bridge.mode==='dark'?'#a1aec3':'#66738b'}}});
 window.complete=()=>reply(bridge.active.id,{operation:'example.audio_gain.apply',outputs:{audio:{kind:'audio_file',path:'/output/audio.wav'},report:{kind:'object',value:{gain_db:6,peak_before:.1,peak_after:.2,peak_limited:false}}}});
 window.addEventListener('message',event=>{
  if(event.source!==frame.contentWindow||event.data?.jsonrpc!=='2.0')return;
  const data=event.data;bridge.requests.push(data);
  if(data.method==='pymss.ready'){send({jsonrpc:'2.0',method:'host.ready'});pushTheme()}
  else if(data.method==='pymss.resize'){if(!bridge.overlay)frame.style.height=data.params.height+'px'}
  else if(data.method==='pymss.overlay'){bridge.overlay=data.params.open;frame.style.position=bridge.overlay?'fixed':'';frame.style.inset=bridge.overlay?'0':'';frame.style.height=bridge.overlay?'100vh':frame.style.height;frame.style.zIndex=bridge.overlay?'100':''}
  else if(data.method==='app.info')reply(data.id,{locale:bridge.locale,defaultOutputDir:'/output'});
  else if(data.method==='settings.read')reply(data.id,bridge.preferences);
  else if(data.method==='settings.write'){if(bridge.rejectSave)fail(data.id,'Settings store unavailable');else{bridge.preferences=data.params.data;reply(data.id,data.params.data)}}
  else if(data.method==='files.pickAudio')reply(data.id,{path:'/audio/source.wav'});
  else if(data.method==='files.pickDirectory')reply(data.id,{path:'/output'});
  else if(data.method==='files.reveal')reply(data.id,{ok:true});
  else if(data.method==='operations.start')bridge.active=data;
  else if(data.method==='operations.cancel'){if(bridge.rejectCancel)fail(data.id,'Cancellation temporarily unavailable');else reply(data.id,true)}
  else fail(data.id,'Unexpected SDK method: '+data.method);
 });
 document.body.style.cssText='margin:0;padding:20px;background:#eef1f6';document.body.append(frame);frame.src=url;
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", required=True, type=Path)
    parser.add_argument("--plugin-root", type=Path, default=ROOT / "examples/audio_gain")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "tooling/.tmp/reference-view")
    args = parser.parse_args()
    sdk = args.studio_root / "src-tauri/resources/plugin-sdk/pymss-sdk.js"
    if not sdk.is_file():
        parser.error("--studio-root must contain the shipped Studio plugin SDK")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    passed, errors = [], []
    with serve(args.plugin_root.resolve()) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1120, "height": 900}, locale="en-US")
        page.on("pageerror", lambda error: errors.append(str(error)))
        sdk_available = [True]
        page.route("**/sdk/pymss-sdk.js", lambda route: route.fulfill(path=str(sdk), content_type="text/javascript") if sdk_available[0] else route.fulfill(body="/* SDK unavailable */", content_type="text/javascript"))
        try:
            page.set_content("<!doctype html><html><body></body></html>")
            page.evaluate(HOST, url)
            tool = page.frame_locator("#tool")
            expect(tool.get_by_role("heading", name="音频增益", exact=True)).to_be_visible()
            expect(tool.get_by_label("增益（dB）")).to_have_value("-3")
            tool.get_by_role("button", name="浏览", exact=True).first.click()
            tool.get_by_label("增益（dB）").fill("6")
            tool.get_by_role("switch").click()
            expect(tool.get_by_role("button", name="调整增益", exact=True)).to_be_enabled()
            passed.append("host-connection-local-translations-and-preferences")
            tool.get_by_role("button", name="调整增益", exact=True).click()
            page.wait_for_function("bridge.active !== null")
            request = page.evaluate("bridge.active.params")
            assert request["operation"] == "example.audio_gain.apply"
            assert request["params"] == {"gain_db": 6, "protect_peak": True}
            assert request["inputs"]["audio"]["path"] == "/audio/source.wav"
            assert request["outputDir"] == "/output"
            page.evaluate("progress(.6);progress(.2)")
            expect(tool.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "60")
            passed.append("real-sdk-payload-and-monotonic-progress")
            tool.get_by_role("button", name="日志", exact=True).click()
            expect(tool.get_by_role("dialog")).to_contain_text("正在调整增益")
            page.evaluate("bridge.locale='en';pushTheme()")
            expect(tool.get_by_role("dialog")).to_contain_text("Applying gain")
            expect(tool.get_by_label("Gain (dB)")).to_have_value("6")
            page.screenshot(path=str(args.artifacts / "log-zh-to-en.png"), full_page=True)
            page.keyboard.press("Escape")
            expect(tool.get_by_role("dialog")).to_have_count(0)
            passed.append("live-locale-switch-preserves-task-and-form")
            page.evaluate("complete()")
            expect(tool.get_by_role("button", name="Show output file", exact=True)).to_be_visible()
            tool.get_by_role("button", name="Show output file", exact=True).click()
            page.wait_for_function("bridge.requests.some(x=>x.method==='files.reveal')")
            light = tool.locator(".gain-panel").first.evaluate("node=>getComputedStyle(node).backgroundColor")
            page.evaluate("bridge.mode='dark';pushTheme()")
            tool.locator(".gain-panel").first.evaluate("node=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
            assert tool.locator(".gain-panel").first.evaluate("node=>getComputedStyle(node).backgroundColor") != light
            control = tool.locator(".n-input").first
            color = control.evaluate("node=>getComputedStyle(node).getPropertyValue('--n-color').trim()")
            expect(control).to_have_css("background-color", color)
            page.screenshot(path=str(args.artifacts / "result-dark-en.png"), full_page=True, animations="disabled")
            passed.append("structured-output-and-host-theme")
            page.evaluate("bridge.active=null")
            tool.get_by_role("button", name="Apply gain", exact=True).click()
            page.wait_for_function("bridge.active !== null")
            page.evaluate("bridge.rejectCancel=true")
            tool.get_by_role("button", name="Cancel", exact=True).click()
            expect(tool.get_by_text("Cancellation temporarily unavailable", exact=True)).to_be_visible()
            expect(tool.get_by_role("button", name="Cancel", exact=True)).to_be_enabled()
            page.evaluate("bridge.rejectCancel=false")
            tool.get_by_role("button", name="Cancel", exact=True).click()
            expect(tool.get_by_role("button", name="Stopping", exact=True)).to_be_disabled()
            page.evaluate("fail(bridge.active.id,'Cancelled','TASK_CANCELLED')")
            expect(tool.get_by_text("Cancelled", exact=True)).to_be_visible()
            expect(tool.get_by_role("button", name="Apply gain", exact=True)).to_be_enabled()
            passed.append("cancel-failure-retry-and-terminal-acknowledgement")
            page.evaluate("bridge.rejectSave=true")
            tool.get_by_label("Gain (dB)").fill("7")
            expect(tool.get_by_text("Preferences could not be saved. Current processing parameters are still available.", exact=True)).to_be_visible()
            tool.get_by_role("button", name="Apply gain", exact=True).click()
            page.wait_for_function("bridge.active.params.params.gain_db===7")
            page.evaluate("fail(bridge.active.id,'Input audio is no longer available','INPUT_UNAVAILABLE')")
            expect(tool.get_by_text("Input audio is no longer available", exact=True)).to_be_visible()
            passed.append("settings-and-processing-errors-remain-visible")
            page.set_viewport_size({"width": 460, "height": 820})
            expect(tool.get_by_role("button", name="Apply gain", exact=True)).to_be_visible()
            assert page.frames[-1].evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(args.artifacts / "narrow-en.png"), full_page=True)
            passed.append("narrow-layout-without-horizontal-overflow")
            sdk_available[0] = False
            page.close()
            page = browser.new_page(viewport={"width": 1120, "height": 900}, locale="en-US")
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/sdk/pymss-sdk.js", lambda route: route.fulfill(body="/* SDK unavailable */", content_type="text/javascript"))
            page.set_content("<!doctype html><html><body></body></html>")
            page.evaluate(HOST, url)
            tool = page.frame_locator("#tool")
            expect(tool.get_by_text("Open this tool inside pymss Studio. If it is already open there, reopen the tool to reconnect.", exact=True)).to_be_visible()
            expect(tool.get_by_role("button", name="Apply gain", exact=True)).to_be_disabled()
            passed.append("missing-sdk-shows-a-connection-state-instead-of-a-blank-page")
            assert not errors, errors
            (args.artifacts / "results.json").write_text(json.dumps({"passed": passed}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Reference view smoke passed: {len(passed)} checks")
        except Exception:
            page.screenshot(path=str(args.artifacts / "failure.png"), full_page=True)
            print("Browser errors:", errors)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
