"""Check persisted file/audio result actions and session-only workflow output settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:1567')
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix='studio-file-result-ui-'))
    def text(name):
        return {'stem': name, 'path': '/out/' + name + '.txt', 'kind': 'file', 'mediaType': 'text/plain'}

    def audio(name):
        return {'stem': name, 'path': '/out/' + name + '.wav'}
    records = []
    for index, (name, job, outputs) in enumerate([
        ('files', 'files', [text('lf'), text('crlf')]),
        ('audio', 'audio', [audio('voice'), audio('music')]),
        ('mixed', 'mixed', [audio('clip'), text('captions')]),
        ('batch-a', 'documents', [text('first')]), ('batch-b', 'documents', [text('second')]),
    ]):
        records.append({'id': name, 'jobId': job, 'model': name, 'input': '/in/' + name,
            'output': '/out', 'status': 'done', 'progress': 100, 'createdAt': index + 1, 'updatedAt': index + 1,
            'files': [item['path'] for item in outputs], 'outputs': outputs, 'logs': []})
    workflow = {'id': 'graph', 'name': 'Output settings', 'description': '', 'format': 'graph', 'formatVersion': 1,
        'createdAt': 1, 'updatedAt': 1, 'definition': {'nodes': [{'id': 1, 'type': 'pymss_save_audio', 'pos': [40, 40]}], 'links': []}}
    state = {'app-settings': {'startupOnboardingSeen': True, 'beginnerGuideInvitationSeen': True,
        'locale': 'zh-CN', 'animationsEnabled': False, 'outputDir': '/tmp/default'},
        'task-history': {'tasks': records}, 'workflow-state': {'selectedWorkflowId': 'graph', 'workflows': [workflow]}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script('for(const [name,value] of Object.entries(' + json.dumps(state) + ''')) {
              const key='pymss-studio:'+name;
              if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify(value));
            }''')
            page.goto(args.url.rstrip('/') + '/#/results', wait_until='networkidle')

            def verify():
                expect(page.locator('#result-card-files')).to_contain_text(re.compile(r'2\s*个文件'))
                expect(page.locator('#result-card-files').get_by_role('button', name='在编辑器中打开', exact=True)).to_have_count(0)
                expect(page.locator('#result-card-audio')).to_contain_text(re.compile(r'2\s*个音轨'))
                expect(page.locator('#result-card-audio').get_by_role('button', name='在编辑器中打开', exact=True)).to_be_visible()
                expect(page.locator('#result-card-mixed')).to_contain_text(re.compile(r'2\s*个文件'))
                expect(page.locator('#result-card-mixed').get_by_role('button', name='在编辑器中打开', exact=True)).to_be_visible()
                batch = page.locator('#result-card-documents')
                expect(batch).to_contain_text(re.compile(r'2\s*个文件'))
                batch.locator('.result-row__main').click()
                expect(batch.locator('.result-detail-card')).to_have_count(2)
                for row in batch.locator('.result-detail-card').all():
                    expect(row).to_contain_text(re.compile(r'1\s*个文件'))
                expect(batch.get_by_role('button', name='在编辑器中打开', exact=True)).to_have_count(0)

            verify()
            page.evaluate("""()=>{
              const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
              window.__editorRequests=[];window.__revealed=[];
              stores.get('editor').ensureProjectForTask=async task=>{window.__editorRequests.push(task.id);return {id:task.id}};
              stores.get('editor').openProjectWindow=async()=>{};
              stores.get('task').revealPath=async path=>window.__revealed.push(path);
            }""")
            for name in ['audio', 'mixed']:
                page.locator('#result-card-' + name).get_by_role('button', name='在编辑器中打开', exact=True).click()
            page.locator('#result-card-files').get_by_role('button', name='打开目录', exact=True).click()
            assert page.evaluate('window.__editorRequests') == ['audio', 'mixed']
            assert page.evaluate('window.__revealed') == ['/out/lf.txt']
            page.screenshot(path=str(output / 'file-audio-mixed-results.png'), full_page=True)
            page.reload(wait_until='networkidle')
            verify()
            page.evaluate("()=>document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/workflow-node-editor?workflowId=graph')")
            expect(page.locator('canvas.lg-canvas')).to_be_visible()
            # The native auxiliary editor has no main-window title bar.
            page.add_style_tag(content='.title-bar { display: none !important; }')
            page.get_by_role('button', name='输出设置', exact=True).click()
            binding = page.locator('[data-workbench-output]')
            expect(binding).to_contain_text('此选择仅用于当前会话')
            page.evaluate("async()=>{const {useWorkbenchStore}=await import('/src/stores/workbench.ts');useWorkbenchStore().session('graph').bindings.outputDirectory='/tmp/selected'}")
            expect(binding.locator('input')).to_have_value('/tmp/selected')
            page.reload(wait_until='networkidle')
            expect(page.locator('canvas.lg-canvas')).to_be_visible()
            page.add_style_tag(content='.title-bar { display: none !important; }')
            page.get_by_role('button', name='输出设置', exact=True).click()
            expect(page.locator('[data-workbench-output] input')).to_have_value('/tmp/default')
            assert not errors, errors
            print('File/audio/mixed cards, persisted file types and session-only output settings passed:', output)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
