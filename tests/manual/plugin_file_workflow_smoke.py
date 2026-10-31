"""Run a file graph in Studio using an isolated fixture from plugin_command_smoke.py."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:1565')
    parser.add_argument('--fixture', type=Path, required=True)
    args = parser.parse_args()
    studio = Path(__file__).resolve().parents[2]
    fixture = args.fixture.resolve()
    if not (fixture / 'active.json').is_file() or not (fixture / 'sources/rust_text/pyproject.toml').is_file():
        raise ValueError('Expected an isolated plugin_command_smoke.py fixture')
    work = Path(tempfile.mkdtemp(prefix='studio-file-workflow-'))
    (work / 'scratch').mkdir()
    source = work / '中文 input.txt'
    source.write_bytes('一行\r\nsecond\rthird\n'.encode())
    expected = '一行\nsecond\nthird\n'.encode()
    environment = {**os.environ,
        'PYTHONPATH': os.pathsep.join([str(studio.parent / 'pymss'), str(studio / 'python')]),
        'PYMSS_STUDIO_RUNTIME_ENVS_DIR': str(fixture / 'runtimes'),
        'PYMSS_STUDIO_ACTIVE_RUNTIME_FILE': str(fixture / 'active.json'),
        'PYMSS_PLUGINS_DIR': str(fixture / 'unmanaged'),
        'PYMSS_SCRATCH_DIR': str(work / 'scratch'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PIP_NO_INDEX': '1',
    }
    sequence = 0

    def worker(command, payload):
        nonlocal sequence
        sequence += 1
        request = work / f'request-{sequence}.json'
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        result = subprocess.run([sys.executable, '-X', 'utf8', str(studio / 'python/worker.py'),
            command, '--payload', str(request)], env=environment, capture_output=True,
            text=True, encoding='utf-8', timeout=60)
        (work / f'worker-{sequence}.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"type"')]
        assert result.returncode == 0 or command == 'infer_workflow' and events, result.stdout + result.stderr
        return events

    declarations = worker('list_nodes', {})[-1]['payload']['nodes']
    node = next(item for item in declarations if item['registered'] and item.get('savesOutput'))
    definition = {'nodes': [
        {'id': 1, 'type': 'StringConstant', 'pos': [30, 50], 'widgets_values': [str(source)]},
        {'id': 2, 'type': 'StringConstant', 'pos': [30, 250], 'widgets_values': ['lf']},
        {'id': 3, 'type': 'StringConstant', 'pos': [30, 430], 'widgets_values': ['crlf']},
        {'id': 4, 'type': node['type'], 'pos': [560, 100], 'inputs': [
            {'name': 'source', 'type': 'STRING', 'link': 1}, {'name': 'line_ending', 'type': 'STRING', 'link': 2}]},
        {'id': 5, 'type': node['type'], 'pos': [560, 400], 'inputs': [
            {'name': 'source', 'type': 'STRING', 'link': 3}, {'name': 'line_ending', 'type': 'STRING', 'link': 4}]},
    ], 'links': [[1, 1, 0, 4, 0, 'STRING'], [2, 2, 0, 4, 1, 'STRING'],
        [3, 1, 0, 5, 0, 'STRING'], [4, 3, 0, 5, 1, 'STRING']]}
    entry = {'id': 'rpc-files', 'name': 'RPC Text Workflow', 'definition': definition,
        'format': 'graph', 'formatVersion': 1, 'createdAt': 1, 'updatedAt': 1}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.set_default_timeout(15000)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('crash', lambda: errors.append('Page renderer crashed'))
            context.add_init_script('localStorage.setItem("pymss-studio:app-settings",JSON.stringify(' + json.dumps({
                'startupOnboardingSeen': True, 'beginnerGuideInvitationSeen': True, 'locale': 'zh-CN',
                'animationsEnabled': False, 'outputDir': str(work / 'output'),
            }) + ')); localStorage.setItem("pymss-studio:workflow-state",JSON.stringify(' + json.dumps({
                'workflows': [entry], 'selectedWorkflowId': entry['id'],
            }) + '));')
            context.expose_binding('__worker', lambda _source, request: worker(request['command'], request['payload']))

            page.goto(args.url.rstrip('/') + '/#/workflows', wait_until='networkidle')
            transport = """async(label)=>{
              const globals=document.querySelector('#app').__vue_app__.config.globalProperties;
              const callbacks=new Map(),listeners=new Map();let sequence=0;
              window.__dispatch=({event,payload})=>{
                for(const [id,listener] of listeners)if(listener.event===event)callbacks.get(listener.handler)?.({event,id,payload});
              };
              const events=new BroadcastChannel('pymss-workflow-smoke');
              events.onmessage=({data})=>{if(data.target.label===label)window.__dispatch(data)};
              window.__audioSources=[];const Audio=window.Audio;
              window.Audio=function(...args){window.__audioSources.push(args);return new Audio(...args)};
              window.__workerPayloads=[];
              window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener:()=>{}};
              window.__TAURI_INTERNALS__={metadata:{currentWindow:{label},currentWebview:{label}},
                transformCallback(callback){callbacks.set(++sequence,callback);return sequence},convertFileSrc:path=>path,
                async invoke(command,args){
                  if(command==='plugin:event|listen'){listeners.set(++sequence,args);return sequence}
                  if(command==='plugin:event|unlisten'){listeners.delete(args.eventId);return null}
                  if(command==='plugin:event|emit_to'){events.postMessage(args);return null}
                  if(command==='list_nodes')return (await window.__worker({command,payload:{}})).at(-1).payload;
                  if(command==='get_workflow_editors')return {revision:0,editors:[]};
                  if(command==='open_workflow_node_editor_window')return {label:'workflow-files'};
                  if(command==='load_app_store')return JSON.parse(localStorage.getItem('pymss-studio:'+args.name)||'null');
                  if(command==='save_app_store'){localStorage.setItem('pymss-studio:'+args.name,JSON.stringify(args.data));return null}
                  if(command==='mutate_workflow_store'){
                    const state=JSON.parse(localStorage.getItem('pymss-studio:workflow-state'));
                    const entry=args.payload.entry,index=state.workflows.findIndex(item=>item.id===entry.id);
                    if(index<0)state.workflows.push(entry);else state.workflows[index]=entry;
                    localStorage.setItem('pymss-studio:workflow-state',JSON.stringify(state));return {state};
                  }
                  if(command==='start_workflow_inference'){
                    window.__workerPayloads.push(args.payload);
                    setTimeout(()=>window.__worker({command:'infer_workflow',payload:args.payload}).then(async events=>{
                      const {useTaskStore}=await import('/src/stores/task.ts');
                      for(const event of events)useTaskStore().handleWorkerEvent(event);
                    }),0);
                    return {taskId:args.payload.taskId,started:true};
                  }
                  if(command==='is_maximized')return false;
                  return null;
                }};
              await globals.$pinia._s.get('workflowNodes').load();
              await (await import('/src/utils/workbenchBridge.ts')).initializeWorkbenchBridge();
            }"""
            page.evaluate(transport, 'main')
            expect(page.locator('.wf-row--advanced')).to_contain_text('节点 5 · 输出 2')
            page.get_by_role('button', name='打开编辑器', exact=True).click()
            page.locator('.n-dialog').get_by_role('button', name='继续打开', exact=True).click()
            expect(page.locator('.n-dialog')).not_to_be_visible()
            print('Workflow library and editor opening validated', flush=True)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(args.url.rstrip('/') + '/#/workflows', wait_until='networkidle')
            page.evaluate(transport, 'workflow-files')
            page.evaluate("async()=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/workflow-node-editor?workflowId=rpc-files')")
            expect(page.locator('canvas.lg-canvas')).to_be_visible()
            run = page.locator('[data-workbench-run-panel]')
            expect(run.get_by_role('button', name='运行', exact=True)).to_be_enabled()
            run.get_by_role('button', name='运行', exact=True).click()
            page.wait_for_function("""()=>document.querySelector('#app').__vue_app__.config.globalProperties.$pinia
              ._s.get('workbench').remoteJobs['rpc-files']?.some(job=>job.tasks.length&&job.tasks.every(task=>['done','failed','cancelled'].includes(task.status)))""", timeout=30000)
            failures = page.evaluate("""()=>document.querySelector('#app').__vue_app__.config.globalProperties.$pinia
              ._s.get('workbench').remoteJobs['rpc-files'].flatMap(job=>job.tasks.filter(task=>task.status!=='done').map(task=>task.error||task.status))""")
            assert not failures, {'failures': failures, 'logs': str(work)}
            run.get_by_role('button', name='日志与结果', exact=True).click()
            page.locator('.workflow-run-details-modal .n-tabs-tab').filter(has_text='结果 · 2').click()
            expect(page.locator('.result-track--file')).to_have_count(2)
            expect(page.locator('.result-preview-heading')).to_contain_text('处理结果')
            expect(page.locator('.result-track__play')).to_have_count(0)
            expect(page.locator('.result-track__slider')).to_have_count(0)
            assert page.evaluate('window.__audioSources') == []
            paths = page.locator('.result-track').evaluate_all('rows=>rows.map(row=>row.dataset.outputPath)')
            assert [Path(path).read_bytes() for path in paths] == [expected, expected.replace(b'\n', b'\r\n')]
            page.screenshot(path=str(work / 'workflow-file-results.png'), full_page=True)
            assert not errors, errors
            print(f'File workflow passed library, canvas, real Worker and result checks: {work}', flush=True)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
