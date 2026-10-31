"""Browser smoke coverage for the file-tool plugin pages.

The parent page implements the public postMessage SDK contract.  No native
worker, model download, or external service is used.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACTS = Path(
    "E:/pymss-vst-lab-artifacts/plugin-tools-20261027/migrated-browser"
)


@contextmanager
def server_url(url: str | None):
    """Serve built plugin pages without importing the Python operation suite."""
    if url:
        yield url
        return

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


HOST = r"""({url, dark, platform, machine}) => {
  const frame = document.createElement('iframe');
  frame.id = 'plugin'; frame.sandbox = 'allow-scripts allow-forms';
  frame.style.cssText = 'border:0;width:100%;height:920px';
  const state = window.bridge = { requests: [], active: null, settings: { mergeSortBy: 'name', asrPreset:'fun-asr-nano' }, cancelNext: false, componentInstalled:true };
  const sdk = {sdkVersion:1, capabilities:[
    'app.info', 'audio.analyze', 'clipboard.writeText', 'files.pickAudio',
    'files.pickDirectory', 'files.pickModel', 'files.reveal', 'files.scanAudio',
    'models.deleteTool', 'operations.cancel', 'operations.start',
    'runtime.packageStatus', 'runtime.managePackage', 'settings.read',
    'settings.write', 'shell.openExternal', 'ui.overlay',
  ]};
  const componentStatus = () => ({package:'funasr',requirement:'funasr',installed:state.componentInstalled,present:state.componentInstalled,version:state.componentInstalled?'1.3.0':null,source:state.componentInstalled?'managed':null,canUninstall:state.componentInstalled,runtimeIdentity:'cpu'});
  const send = message => frame.contentWindow.postMessage(message, '*');
  const reply = (id, result) => send({jsonrpc:'2.0', id, result});
  const fail = (id, message, data) => send({jsonrpc:'2.0', id, error:{code:-32000, message, ...(data ? {data} : {})}});
  const legacy = (operation) => ({
    convert: {operation:'convert',outputDir:'/output',outputPaths:['/output/source.ogg','/output/second.ogg'],succeeded:2,failed:[]},
    inspect: {operation:'inspect', inputPath:'/audio/source.wav', fileName:'source.wav', fileSize:88200, format:{name:'WAV', longName:'WAV', duration:12.5, bitRate:1411200, tags:{artist:'Studio'}}, audioStreams:[], raw:{codec:'pcm_s16le'}},
    sdr: {operation:'sdr', sdr:[12.4], averageSdr:12.4, siSdr:[13.2], averageSiSdr:13.2, sampleRate:44100, referencePath:'/audio/reference.wav', estimatedPath:'/audio/estimated.wav'},
    slicer: {operation:'slicer', sourceCount:2, segments:[{outputPath:'/output/clip.wav', start:0, end:2.5}], keptDuration:2.5, outputDir:'/output'},
    merge: {operation:'merge', succeeded:2, merged:2, outputPath:'/output/merged.wav', outputDir:'/output'},
    loudness: {operation:'loudness', succeeded:1, outputDir:'/output', measurement:'rms', target:-16, measurements:[{path:'/output/source.wav',inputLevel:-20,outputLevel:-16,target:-16,unit:'dB',withinTolerance:true}]},
    asr: {operation:'asr', succeeded:1, inputPath:'/audio/source.wav', text:'测试转写', sentences:[{start:0,end:1,text:'测试'}], segmentCount:1, outputPath:'/output/source.srt', outputDir:'/output'},
    midi: {operation:'midi', succeeded:1, bpm:120, roundedBpm:120, action:'transcribe', inputDuration:8, firstNoteAt:0, lastNoteAt:7, noteCount:8, outputPath:'/output/source.mid', outputDir:'/output'},
    tempo_key: {operation:'tempo_key', succeeded:1, items:[{path:'/audio/source.wav', fileName:'source.wav', bpm:120, key:'C major', duration:12.5}]},
  })[operation] || {operation, succeeded:1};
  window.addEventListener('message', event => {
    if (event.source !== frame.contentWindow || event.data?.jsonrpc !== '2.0') return;
    const request = event.data; state.requests.push(request);
    if (request.method === 'pymss.ready') {
      send({jsonrpc:'2.0',method:'host.theme',params:{colorMode:dark?'dark':'light',cssVariables:dark?{'--surface':'#151a21','--surface-1':'#202732','--on-surface':'#e0e7ef','--on-surface-muted':'#aab8c8','--outline':'#3b4757','--primary':'#2c9c89'}:{}}});
      send({jsonrpc:'2.0',method:'host.ready',params:{}});
    } else if (request.method === 'pymss.resize') { if (!state.overlay) frame.style.height = request.params.height + 'px';
    } else if (request.method === 'pymss.overlay') {
      state.overlay = request.params.open;
      if (state.overlay) {state.previousStyle = frame.style.cssText; frame.style.cssText = 'position:fixed;inset:0;z-index:3000;border:0;width:100vw;height:100vh';}
      else if (state.previousStyle) frame.style.cssText = state.previousStyle;
    } else if (request.method === 'app.info') reply(request.id, {...sdk,studio:'pymss-studio',version:'test',locale:'en',defaultOutputDir:'/output',defaultModelDir:'/models',runtimeInfo:{platform,machine,backend:'cpu',packages:{funasr:'1.3.0'}}});
    else if (request.method === 'settings.read') reply(request.id, state.settings);
    else if (request.method === 'settings.write') { state.settings = request.params.data; reply(request.id, state.settings); }
    else if (request.method === 'files.pickAudio') reply(request.id, request.params.multiple ? {paths:['/audio/source.wav','/audio/second.wav']} : {path:'/audio/source.wav'});
    else if (request.method === 'files.pickDirectory') reply(request.id, {path:request.params.title?.includes('input') ? '/input' : (state.directoryPicked ? '/output' : (state.directoryPicked = true, '/input'))});
    else if (request.method === 'files.pickModel') reply(request.id, {path:'/models/game.pt'});
    else if (request.method === 'files.scanAudio') reply(request.id, {files:['/audio/source.wav','/audio/second.wav']});
    else if (request.method === 'files.reveal') reply(request.id, {ok:true});
    else if (request.method === 'runtime.packageStatus') reply(request.id, componentStatus());
    else if (request.method === 'runtime.managePackage') {
      state.active = request;
      send({jsonrpc:'2.0',method:'task.progress',params:{id:request.id,event:{type:'optional_package_stage',payload:{package:'funasr',action:request.params.payload.action,message:'Preparing ASR component'}}}});
      state.componentInstalled = request.params.payload.action === 'install';
      reply(request.id, componentStatus());
    }
    else if (request.method === 'operations.start') {
      state.active = request;
      const payload = request.params.inputs.request || {operation:'convert'};
      const op = payload.operation;
      if (state.failNext) { state.failNext = false; fail(request.id, 'Input audio is unavailable'); return; }
      if (op === 'convert') send({jsonrpc:'2.0',method:'task.progress',params:{id:request.id,fraction:0.5,message:'source.wav'}});
      send({jsonrpc:'2.0',method:'task.progress',params:{id:request.id,event:{type:'audio_tool_progress',payload:{operation:op,phase:'processing',completed:1,total:2,current:'source.wav'}}}});
      send({jsonrpc:'2.0',method:'task.progress',params:{id:request.id,event:{type:'audio_tool_progress',payload:{operation:op,phase:'processing',completed:2,total:2,current:'source.wav'}}}});
      if (!state.cancelNext) reply(request.id, {operation:request.params.operation,outputs:{report:{kind:'object',value:legacy(op)}}});
    } else if (request.method === 'audio.analyze') {
      state.active = request;
      const payload = request.params.payload; const op = payload.operation;
      send({jsonrpc:'2.0',method:'task.progress',params:{id:request.id,event:{type:'audio_tool_progress',payload:{operation:op,phase:'analyzing_tempo_key',completed:1,total:2,current:'source.wav'}}}});
      if (!state.cancelNext) reply(request.id, op === 'midi' ? {operation:'midi',action:'detect_bpm',bpm:120,roundedBpm:120} : legacy(op));
    } else if (request.method === 'operations.cancel') {
      reply(request.id, true);
      const active = state.active; state.cancelNext = false;
      if (active) fail(active.id, 'Cancelled by smoke test', {code:'TASK_CANCELLED'});
    } else if (request.method === 'shell.openExternal' || request.method === 'clipboard.writeText' || request.method === 'models.deleteTool') reply(request.id, {ok:true});
    else fail(request.id, `Unexpected SDK method: ${request.method}`);
  });
  document.body.style.margin = '0'; document.body.append(frame); frame.src = url;
}"""


def open_tool(
    browser,
    base_url: str,
    plugin: str,
    *,
    dark=False,
    width=1180,
    platform="win32",
    machine="AMD64",
):
    page = browser.new_page(viewport={"width": width, "height": 900}, locale="en-US")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content("<!doctype html><html><body></body></html>")
    page.evaluate(
        HOST,
        {
            "url": f"{base_url}/plugins/{plugin}/ui/dist/index.html",
            "dark": dark,
            "platform": platform,
            "machine": machine,
        },
    )
    frame = page.frame_locator("#plugin")
    expect(frame.locator("body")).to_be_visible()
    try:
        page.wait_for_function(
            "bridge.requests.some(request => request.method === 'pymss.ready')",
            timeout=5000,
        )
    except Exception as error:
        detail = "; ".join(errors) or "plugin did not send pymss.ready"
        page.close()
        raise AssertionError(
            f"{plugin} did not initialize in the Studio SDK host: {detail}"
        ) from error
    return page, frame


def click_text(frame, pattern: str, *, nth=0):
    button = frame.get_by_role("button", name=re.compile(pattern)).nth(nth)
    expect(button).to_be_visible()
    button.click()


def wait_for_success(page, operation: str):
    page.wait_for_function(
        "op => bridge.requests.some(x => x.method === 'operations.start' && x.params.inputs.request.operation === op) || bridge.requests.some(x => x.method === 'audio.analyze' && x.params.payload.operation === op)",
        arg=operation,
        timeout=5000,
    )


def assert_start_contract(page, operation: str, *, analysis=False):
    requests = page.evaluate("bridge.requests")
    request = next(
        item
        for item in requests
        if item["method"] == ("audio.analyze" if analysis else "operations.start")
        and (
            item["params"]["payload"]["operation"]
            if analysis
            else item["params"]["inputs"]["request"]["operation"]
        )
        == operation
    )
    if analysis:
        assert request["params"]["payload"]["operation"] == operation
    else:
        assert request["params"]["inputs"]["request"]["operation"] == operation
        assert request["params"]["params"] == {}


def cancel_once(page, frame, operation: str, *, analysis=False):
    page.evaluate("bridge.cancelNext = true")
    # The next task must remain pending until the UI presses cancel.
    if analysis:
        click_text(frame, r"Start|Detect")
    else:
        click_text(frame, r"Inspect|Calculate|Slice|Merge|Match|Transcribe|Convert")
    wait_for_success(page, operation)
    frame.locator(".tool-log-button").click()
    click_text(frame, r"Cancel")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "bridge.requests.some(x => x.method === 'operations.cancel')"
    )


def run_inspect(page, frame):
    click_text(frame, r"Browse")
    wait_for_success(page, "inspect")
    assert_start_contract(page, "inspect")
    frame.get_by_text("Metadata tags", exact=True).click()
    expect(frame.get_by_text("Studio", exact=True)).to_be_visible()
    frame.get_by_text("Raw FFprobe data", exact=True).click()
    expect(frame.locator("pre")).to_contain_text("pcm_s16le")


def run_sdr(page, frame):
    click_text(frame, r"Browse", nth=0)
    click_text(frame, r"Browse", nth=1)
    click_text(frame, r"Calculate")
    wait_for_success(page, "sdr")
    assert_start_contract(page, "sdr")
    expect(frame.get_by_text("12.4 dB", exact=True)).to_be_visible()


def run_slicer(page, frame):
    click_text(frame, r"Choose audio files")
    expect(frame.get_by_text("source.wav")).to_be_visible()
    # Numeric parameter controls must remain present after migration.
    expect(frame.locator("input")).to_have_count(6)
    click_text(frame, r"Slice audio")
    wait_for_success(page, "slicer")
    assert_start_contract(page, "slicer")
    expect(frame.get_by_text("clip.wav")).to_be_visible()


def run_merge(page, frame):
    click_text(frame, r"Browse", nth=0)
    click_text(frame, r"Browse", nth=1)
    # Preserve sort controls and drive a real select before starting.
    expect(frame.locator(".n-select")).to_have_count(2)
    click_text(frame, r"Merge")
    wait_for_success(page, "merge")
    assert_start_contract(page, "merge")
    expect(frame.get_by_text("merged.wav")).to_be_visible()


def run_loudness(page, frame):
    click_text(frame, r"Choose audio files")
    # Five measurement modes remain available through the Naive select menu.
    select = frame.locator(".n-select").nth(0)
    select.click()
    expect(frame.locator(".n-base-select-option")).to_have_count(5)
    page.keyboard.press("Escape")
    click_text(frame, r"Match loudness")
    wait_for_success(page, "loudness")
    assert_start_contract(page, "loudness")
    expect(frame.get_by_text("Measured output: -16.00 dB", exact=True)).to_be_visible()


def run_asr(page, frame):
    expect(frame.get_by_text(re.compile("Official preset|Local model"))).to_have_count(
        2
    )
    expect(frame.get_by_text(re.compile("Installed|FunASR"))).to_be_visible()
    click_text(frame, r"Browse")
    click_text(frame, r"Transcribe")
    wait_for_success(page, "asr")
    assert_start_contract(page, "asr")
    expect(frame.get_by_text("测试转写")).to_be_visible()
    click_text(frame, r"Uninstall component")
    dialog = frame.get_by_role("dialog")
    expect(dialog).to_contain_text("model")
    dialog.get_by_role("button", name="Uninstall component", exact=True).click()
    page.wait_for_function("bridge.componentInstalled === false")
    click_text(frame, r"Install component")
    page.wait_for_function("bridge.componentInstalled === true")
    expect(
        frame.get_by_role("button", name="Uninstall component", exact=True)
    ).to_be_enabled()


def run_midi(page, frame):
    click_text(frame, r"Browse", nth=0)
    click_text(frame, r"Browse|Choose model", nth=1)
    click_text(frame, r"Detect")
    wait_for_success(page, "midi")
    assert_start_contract(page, "midi", analysis=True)
    expect(frame.get_by_text("120", exact=True)).to_be_visible()
    click_text(frame, r"Convert")
    wait_for_success(page, "midi")
    assert_start_contract(page, "midi")


def run_tempo(page, frame):
    click_text(frame, r"Choose audio files")
    click_text(frame, r"Start detection")
    wait_for_success(page, "tempo_key")
    assert_start_contract(page, "tempo_key", analysis=True)
    expect(frame.get_by_text("C major")).to_be_visible()
    # A real cancel button must propagate the dedicated TASK_CANCELLED code.
    cancel_once(page, frame, "tempo_key", analysis=True)


def run_macos_asr(page, frame):
    frame.locator(".n-select").first.click()
    expect(frame.locator(".n-base-select-option")).to_have_count(3)
    page.keyboard.press("Escape")
    expect(
        frame.locator(".n-checkbox--disabled").filter(has_text="SRT")
    ).to_be_visible()
    expect(frame.get_by_placeholder(re.compile("hotword", re.I))).to_be_disabled()
    click_text(frame, r"Browse")
    click_text(frame, r"Transcribe")
    wait_for_success(page, "asr")
    request = page.evaluate(
        "bridge.requests.find(x => x.method === 'operations.start').params.inputs.request"
    )
    assert request["modelPreset"] == "paraformer-zh", request
    assert "srt" not in request["outputFormats"], request
    expect(frame.get_by_text("测试转写")).to_be_visible()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Use an existing loopback server")
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    cases = [
        ("inspect", "inspect", run_inspect),
        ("sdr", "sdr", run_sdr),
        ("slicer", "slicer", run_slicer),
        ("merge", "merge", run_merge),
        ("loudnorm", "loudness", run_loudness),
        ("asr", "asr", run_asr),
        ("game", "midi", run_midi),
        ("tempo_key", "tempo_key", run_tempo),
    ]
    passed, failures = [], []
    with server_url(args.url) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            for index, (plugin, operation, runner) in enumerate(cases):
                page, frame = open_tool(
                    browser,
                    base_url,
                    plugin,
                    dark=index == 6,
                    width=420 if index == 7 else 1180,
                )
                try:
                    runner(page, frame)
                    page.screenshot(
                        path=str(
                            args.artifacts
                            / f"{plugin}-{'dark' if index == 6 else 'light'}{'-narrow' if index == 7 else ''}.png"
                        ),
                        full_page=True,
                    )
                    passed.append(plugin)
                except Exception as error:
                    failures.append({"plugin": plugin, "error": str(error)})
                    page.screenshot(
                        path=str(args.artifacts / f"{plugin}-failed.png"),
                        full_page=True,
                    )
                finally:
                    page.close()
            page, frame = open_tool(
                browser, base_url, "asr", platform="darwin", machine="arm64"
            )
            try:
                run_macos_asr(page, frame)
                passed.append("asr-mlx-platform")
                page.screenshot(
                    path=str(args.artifacts / "asr-mlx-platform.png"), full_page=True
                )
            finally:
                page.close()
        finally:
            browser.close()
    (args.artifacts / "results.json").write_text(
        json.dumps(
            {"passed": passed, "failures": failures}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"File-tool browser smoke passed: {len(passed)} tools")
    if failures:
        raise AssertionError(json.dumps(failures, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
