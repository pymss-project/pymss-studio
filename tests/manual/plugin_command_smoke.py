"""Run a native command through Runtime, CLI, DAG, Worker and Studio's iframe."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1564")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pymss-source", type=Path)
    parser.add_argument("--adapter", choices=["command", "rpc-process", "python-rpc"], default="command")
    args = parser.parse_args()
    studio = Path(__file__).resolve().parents[2]
    kernel = (args.pymss_source or studio.parent / "pymss").resolve()
    rpc = args.adapter != "command"
    python = args.adapter == "python-rpc"
    name = "python_rpc_text" if python else "rust_rpc_text" if rpc else "rust_text"
    plugin_id = "example." + name
    node_type = "example_" + name + "_normalize"
    original = studio.parent / "pymss-plugins/examples" / name
    work = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-command-smoke-"))
    if work.exists() and any(work.iterdir()):
        raise ValueError("The output directory must be empty")
    work.mkdir(parents=True, exist_ok=True)
    package = work / "sources/rust_text"
    shutil.copytree(original, package, ignore=shutil.ignore_patterns("bin", "__pycache__", ".cache", "target"))
    if not python:
        subprocess.run([sys.executable, str(package / "scripts/build.py"), *(["--offline"] if rpc else [])], check=True, timeout=180)
    source = work / "中文 input.txt"
    source.write_bytes("一行\r\nsecond\rthird\n".encode())
    expected = "一行\nsecond\nthird\n".encode()
    environment = {**os.environ,
        "PYTHONPATH": os.pathsep.join([str(kernel), str(studio / "python")]),
        "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
        "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
        "PYMSS_PLUGINS_DIR": str(work / "unmanaged"),
        "PYMSS_SCRATCH_DIR": str(work / "scratch"),
        "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": str(work / "outputs"),
        "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1",
    }
    (work / "scratch").mkdir()
    (work / "active.json").write_text('{"backend":"cpu"}', encoding="utf-8")
    sequence = 0
    observations = []

    released_environment = {**environment, "PYTHONPATH": str(studio / "python"),
                            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "released-runtimes")}
    helper = "register_rpc_processes" if rpc else "register_commands"
    availability = ("import importlib.util;print(importlib.util.find_spec('pymss.operations.python_profiles') is not None)" if python
                    else f"from pymss.plugins import kit;print(hasattr(kit,{helper!r}))")
    available = subprocess.run([sys.executable, "-c", availability],
        env=released_environment, capture_output=True, text=True, timeout=30)
    if available.returncode == 0 and available.stdout.strip() == "False":
        compatibility_request = work / "released-install.json"
        compatibility_request.write_text(json.dumps({"action": "install", "target": str(package)}), encoding="utf-8")
        rejected = subprocess.run([sys.executable, "-X", "utf8", str(studio / "python/worker.py"), "manage_plugin", "--payload", str(compatibility_request)],
                                 env=released_environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert rejected.returncode == 1, rejected.stdout
        assert ("does not support isolated Python process plugins" if python else "requires a pymss runtime with the RPC process adapter" if rpc else "requires a pymss runtime with the command adapter") in rejected.stdout, rejected.stdout
        assert not list((work / "released-runtimes").glob(f"plugins/*/installed/{plugin_id}"))
        observations.append({"command": "published_runtime_install", "status": "unsupported adapter rejected before publication"})

    def worker(command, payload, *, allow_failure=False):
        nonlocal sequence
        sequence += 1
        data = work / f"request-{sequence}.json"
        data.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run([sys.executable, "-X", "utf8", str(studio / "python/worker.py"), command, "--payload", str(data)],
                                   env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{"type"')]
        if (completed.returncode and not allow_failure) or not events:
            raise AssertionError({"code": completed.returncode, "events": events, "stderr": completed.stderr[-2000:]})
        observations.append({"command": command, "events": [event["type"] for event in events]})
        return events

    installed = worker("manage_plugin", {"action": "install", "target": str(package), "defaultPolicy": "review"})[-1]["payload"]
    row = next(row for row in installed["installed"] if row["name"] == plugin_id)
    assert row["loaded"], row
    managed_root = Path(row["path"])
    operation = plugin_id + ".normalize"
    spec = worker("describe_operation", {"operation": operation})[-1]["payload"]
    worker_events = worker("run_operation", {"operation": operation, "inputs": {"source": {"kind": "file", "path": str(source)}},
        "params": {"line_ending": "lf"}, "expectedSpecDigest": spec["specDigest"], "outputDir": str(work / "worker-out")})
    worker_result = worker_events[-1]["payload"]
    if rpc:
        assert any(event["type"] == "operation_progress" and event["payload"].get("event", {}).get("type") == "rpc_progress" for event in worker_events)
    assert Path(worker_result["outputs"]["document"]["path"]).read_bytes() == expected

    cli_environment = {**environment, "PYMSS_PLUGINS_DIR": str(managed_root.parent)}
    completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "pymss.cli", "ops", "run", operation,
        "--input", f"source={source}", "--output-dir", str(work / "cli-out")], env=cli_environment,
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr[-2000:]
    cli_result = json.loads(completed.stdout)
    assert cli_result["document"]["kind"] == "file"
    assert Path(cli_result["document"]["path"]).read_bytes() == expected

    graph_script = work / "graph.py"
    graph_script.write_text('''import json,sys
from pathlib import Path
from pymss.graph import DAG,DAGNode,DAGLink,run_dag
from pymss.operations import Runtime
from pymss.operations.host_services import ReadonlyHost
runtime=Runtime()
reports=runtime.load_plugins([Path(sys.argv[1])])
assert any(item["status"]=="activated" for item in reports),reports
source=DAGNode("source","StringConstant",data={"widgets_values":[sys.argv[2]]})
convert=DAGNode("convert","example_rust_text_normalize",inputs=[DAGLink(1,"source",0,"convert",0,"STRING")],data={"params":{"line_ending":"lf"}})
result=run_dag(DAG(nodes=[source,convert]),output_dir=sys.argv[3])
assert len(result)==1,result
assert result.records[0].kind=="file"
print(json.dumps(result.records[0].to_dict()))
'''.replace('example_rust_text_normalize', node_type).replace('output_dir=sys.argv[3])', 'output_dir=sys.argv[3],host_request=ReadonlyHost("graph"))'), encoding="utf-8")
    completed = subprocess.run([sys.executable, str(graph_script), str(managed_root.parent), str(source), str(work / "graph-out")],
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert Path(json.loads(completed.stdout)["path"]).read_bytes() == expected

    if rpc:
        definition = {"nodes": [
            {"id": 1, "type": "StringConstant", "widgets_values": [str(source)], "inputs": []},
            {"id": 2, "type": node_type, "inputs": [{"name": "source", "type": "STRING", "link": 1}]},
        ], "links": [[1, 1, 0, 2, 0, "STRING"]]}
        graph_file = work / "workflow.json"
        graph_file.write_text(json.dumps(definition), encoding="utf-8")
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "pymss.cli", "comfy", "run", "--config", str(graph_file),
            "--output", str(work / "cli-graph-out")], env=cli_environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert (work / "cli-graph-out/normalized.txt").read_bytes() == expected
        graph_result = worker("infer_workflow", {"taskId": "rpc-graph-" + work.name, "workflow": definition,
            "output": str(work / "worker-graph-out"), "outputLayout": "flat"})[-1]["payload"]
        assert graph_result["outputs"][0]["kind"] == "file"
        assert Path(graph_result["outputs"][0]["path"]).read_bytes() == expected

    metadata = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pymss"]["studio"]
    views = [{**view, "entryPath": str(managed_root / view["entry"]), "sdkVersion": metadata["sdk_version"],
              "requiredCapabilities": metadata["required_capabilities"]} for view in metadata["views"]]
    row["contributions"] = {"views": views}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""if(window===window.top)localStorage.setItem('pymss-studio:app-settings',JSON.stringify({
              startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',themeMode:'light',animationsEnabled:false,pluginDefaultsPolicy:'review'}))""")

            def invoke_worker(_source, request):
                return worker(request["command"], request["payload"], allow_failure=request["command"] == "run_operation")

            page.expose_binding("__invokeWorker", invoke_worker)

            def plugin_page(route):
                path = urlparse(route.request.url).path.removeprefix("/view")
                if path in {"/sdk/pymss-sdk.js", "/sdk/pymss-sdk.css"}:
                    route.fulfill(path=str(studio / "src-tauri/resources/plugin-sdk" / Path(path).name),
                                  content_type="text/javascript" if path.endswith('.js') else "text/css")
                elif path == f"/{plugin_id}/tool/index.html":
                    route.fulfill(path=str(package / "ui/index.html"), content_type="text/html")
                else:
                    route.fulfill(status=404, body="Not found")

            page.route("http://pymss-plugin.localhost/**", plugin_page)
            page.goto(args.url.rstrip('/') + '/#/tools', wait_until='networkidle', timeout=60000)
            page.evaluate(r"""async ({row,root,source,output}) => {
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              const callbacks=new Map(),listeners=new Map();let sequence=0;
              window.__nativeCalls=[];window.__operationResults=[];
              const inventory={installed:[row],pluginsDir:root,runtime:{identity:'cpu-command',pythonPath:'python'},catalog:[],catalogError:null};
              const emit=event=>{for(const callback of listeners.values())callbacks.get(callback)?.({event:'pymss://worker-event',payload:event})};
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(_event,id){listeners.delete(id)}};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
                convertFileSrc:(path,protocol)=>`http://${protocol}.localhost/${path}`,
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},unregisterCallback(id){callbacks.delete(id)},
                async invoke(command,args){
                  window.__nativeCalls.push({command,...structuredClone(args||{})});
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args.handler);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin_marketplace')return structuredClone(inventory);
                  if(command==='plugin:dialog|open')return args.options.directory?output:source;
                  if(command==='get_build_info')return {version:'0.0.23',updateSupported:false};
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='load_app_store')return null;
                  if(command==='describe_operation')return (await window.__invokeWorker({command:'describe_operation',payload:args.payload})).at(-1).payload;
                  if(command==='start_operation_task'){
                    const id=args.payload.requestId;
                    setTimeout(()=>window.__invokeWorker({command:'run_operation',payload:args.payload}).then(events=>{
                      window.__operationResults.push(events.at(-1).payload);
                      for(const event of events)emit({...event,taskId:id,requestId:id});
                    }).catch(error=>emit({type:'error',taskId:id,requestId:id,payload:{code:'OPERATION_FAILED',message:error.message}})),0);
                    return {taskId:id,started:true};
                  }
                  return null;
                }};
              const market=(await import('/src/features/plugin-marketplace/composable.ts')).usePluginMarketplace();
              await market.refreshInventory();
              await globals.$router.push('/tools?pluginView='+row.name+'/tool');
            }""", {"row": row, "root": str(managed_root.parent), "source": str(source), "output": str(work / 'browser-out')})
            frame = page.frame_locator('iframe.plugin-view-frame')
            expect(frame.get_by_role('heading', name='文本转换' if rpc else '文本换行转换', exact=True)).to_be_visible()
            expect(frame.locator('#status')).to_have_text('就绪', timeout=20000)
            frame.locator('#pickSource').click()
            expect(frame.locator('#source')).to_have_value(str(source))
            frame.locator('#pickOutput').click()
            frame.locator('#run').click()
            expect(frame.locator('#result')).to_be_visible(timeout=20000)
            path = frame.locator('#resultPath').inner_text()
            assert Path(path).read_bytes() == expected
            assert not frame.locator('#error').is_visible()
            frame.locator('#reveal').click()
            page.wait_for_function("window.__nativeCalls.some(item=>item.command==='reveal_path')")
            frame.locator('#run').click()
            expect(frame.locator('#resultPath')).not_to_have_text(path, timeout=20000)
            assert Path(path).read_bytes() == expected
            source.write_bytes(b'\xff')
            frame.locator('#run').click()
            expect(frame.locator('#error')).to_be_visible(timeout=20000)
            expect(frame.locator('#errorDetails')).to_contain_text('UTF-8')
            expect(frame.locator('#result')).not_to_be_visible()
            assert len(list((work / 'browser-out').iterdir())) == 2
            source.write_bytes('一行\r\nsecond\rthird\n'.encode())
            frame.locator('#ending').select_option('crlf')
            frame.locator('#run').click()
            expect(frame.locator('#result')).to_be_visible(timeout=20000)
            assert Path(frame.locator('#resultPath').inner_text()).read_bytes() == expected.replace(b'\n', b'\r\n')
            page.screenshot(path=str(work / 'native-file-result.png'), full_page=True)
            assert not errors, errors
        finally:
            browser.close()
    (work / 'summary.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
    print(f"{'RPC process' if rpc else 'Native command'} passed CLI, DAG, real Worker and Studio iframe checks: {work}")


if __name__ == '__main__':
    main()
