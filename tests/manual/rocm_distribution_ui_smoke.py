"""Verify ROCm distribution selection with deterministic IPC and no native installation."""
import argparse
import ast
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1457')
parser.add_argument('--output-dir', type=Path, default=Path('.cache/rocm-distribution-ui-smoke'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
source = Path(__file__).with_name('rocm_runtime_ui_smoke.py').read_text(encoding='utf-8')
bootstrap = next(ast.literal_eval(node.value) for node in ast.parse(source).body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'BOOTSTRAP' for target in node.targets))
INVENTORY = r"""() => {
  __base.profileId='multi';__base.profileRevision=2;__base.installedTargets=['gfx1100'];
  __runtime.installState=structuredClone(__base);__runtime.installedEnvironments=[structuredClone(__base)];
  __runtime.seedPreparationRequired=false;
  __app.runtimeInfo=structuredClone(__runtime);__app.selectedRocmDeviceIds=[];
  window.__inventory={schemaVersion:1,platform:'win32',errors:[],devices:[
    {id:'hip-pci:0000:03:00.0',vendor:'amd',name:'AMD discrete GPU',gfx:'gfx1100',architectureSource:'native',architectureConfidence:'confirmed',isDiscrete:true},
    {id:'pnp:unknown-hardware',vendor:'amd',name:'AMD unknown GPU',gfx:'gfx1030',architectureSource:'unknown',architectureConfidence:'hint',isDiscrete:false}],
    profiles:[{id:'multi',revision:2,releaseState:'preview',installationEnabled:true,validationStatus:'preview',availableTargets:['all','gfx1030','gfx1100'],validatedTargets:[],torchVersion:'2.13.0'},
      {id:'blocked',revision:1,releaseState:'disabled',installationEnabled:false,validationStatus:'preview',availableTargets:['gfx1100'],validatedTargets:[]}]};
  const original=__TAURI_INTERNALS__.invoke;
  __TAURI_INTERNALS__.invoke=async(command,args={})=>{
    if(command==='list_models')return {models:[]};
    if(command==='activate_runtime'){
      __calls.push({command,args});__base.selectedDeviceIds=args.payload.selectedDeviceIds;
      __runtime.installState=structuredClone(__base);__runtime.installedEnvironments=[structuredClone(__base)];return true;
    }
    if(command==='runtime_hardware_inventory'){__calls.push({command,args});return structuredClone(__inventory)}
    if(command==='runtime_install_plan'){
      __calls.push({command,args});const p=args.payload;
      return {backend:'rocm',...p,profileRevision:2,releaseState:'preview',installationEnabled:true,requiresPreviewAcceptance:p.action!=='prepareSeed',
        requestedTargets:p.targets,resolvedTargets:p.targets.includes('all')?['gfx1030','gfx1100']:p.targets,
        expectedProfileId:'multi',expectedProfileRevision:2,expectedBaseFingerprint:'base-a',expectedTargetFingerprint:'target-a',downloadBytes:p.action==='prepareSeed'?0:1024};
    }
    if(command==='start_runtime_install'){__calls.push({command,args});return true}
    return original(command,args);
  };
}"""

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    summaries = []
    try:
        for locale in ['zh-CN', 'en']:
            page = browser.new_page(viewport={'width': 760, 'height': 600})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('pymss-studio:app-settings',JSON.stringify("+json.dumps({'startupOnboardingSeen': True, 'beginnerGuideInvitationSeen': True, 'beginnerGuideProgress': {'status': 'skipped', 'step': 0}, 'locale': locale, 'themeMode': 'dark', 'animationsEnabled': False})+"))")
            page.goto(args.url.rstrip('/')+'/index.html', wait_until='networkidle')
            page.evaluate(bootstrap)
            page.evaluate(INVENTORY)
            page.evaluate("__router.push({name:'settings',query:{section:'runtime',backend:'rocm'}})")
            card = page.locator('[data-runtime-backend="rocm"]').first
            manage = '管理 ROCm 环境' if locale == 'zh-CN' else 'Manage ROCm environment'
            card.get_by_role('button', name=manage, exact=True).click()
            modal = page.locator('.rocm-environment-selection')
            expect(modal).to_be_visible()
            use = '使用现有环境' if locale == 'zh-CN' else 'Use installed environment'
            expect(page.get_by_role('button', name=use, exact=True)).to_be_enabled()
            page.get_by_role('button', name=use, exact=True).click()
            page.wait_for_function("__calls.some(c=>c.command==='activate_runtime')")
            assert page.evaluate("__calls.find(c=>c.command==='activate_runtime').args.payload.selectedDeviceIds") == ['hip-pci:0000:03:00.0']
            page.wait_for_function("__app.runtimeInfo.installedEnvironments[0].selectedDeviceIds?.[0]==='hip-pci:0000:03:00.0'")
            page.wait_for_function("__app.runtimeInfoLoading===false&&__app.envLoading===false")
            card.get_by_role('button', name=manage, exact=True).click()
            assert not page.evaluate("__calls.some(c=>c.command==='runtime_install_plan')")
            # Select a hint-only adapter: an architecture hint must not silently authorize a target.
            modal.get_by_text('AMD unknown GPU', exact=True).click()
            expect(page.get_by_role('button', name=use, exact=True)).to_be_disabled()
            # An explicit manual target completes the selection; then inspect the exact plan.
            modal.locator('.n-base-selection').last.click()
            page.get_by_text('gfx1030', exact=True).last.click()
            page.keyboard.press('Escape')
            review = '查看安装计划' if locale == 'zh-CN' else 'Review installation plan'
            page.get_by_role('button', name=review, exact=True).click()
            expect(modal.locator('.rocm-plan-summary')).to_be_visible()
            confirm = '确认' if locale == 'zh-CN' else 'Confirm'
            expect(page.get_by_role('button', name=confirm, exact=True).last).to_be_disabled()
            modal.locator('.rocm-plan-summary .n-checkbox').click()
            expect(page.get_by_role('button', name=confirm, exact=True).last).to_be_enabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(args.output_dir/f'preview-plan-{locale}.png'), full_page=True)
            page.get_by_role('button', name=confirm, exact=True).last.click()
            page.wait_for_function("__calls.some(c=>c.command==='start_runtime_install')")
            payload = page.evaluate("__calls.findLast(c=>c.command==='start_runtime_install').args.payload")
            assert payload['action'] == 'addTargets' and payload['targets'] == ['gfx1030', 'gfx1100']
            assert payload['acceptPreview'] is True
            assert all(key in payload for key in ['expectedProfileId','expectedProfileRevision','expectedBaseFingerprint','expectedTargetFingerprint'])
            page.evaluate("__app.handleRuntimeEvent({type:'runtime_install_finished',taskId:__app.runtimeInstallTaskId,payload:{backend:'rocm',activated:false}})")
            # Read-only bundled seeds expose an offline preparation action, never a Torch download.
            page.evaluate("__runtime.seedPreparationRequired=true;__runtime.seedProfileId='multi';__runtime.seedInstalledTargets=['gfx1100'];__app.runtimeInfo=structuredClone(__runtime)")
            prepare = '准备内置环境' if locale == 'zh-CN' else 'Prepare bundled environment'
            card.get_by_role('button', name=prepare, exact=True).first.click()
            expect(modal).to_be_visible()
            page.get_by_role('button', name=review, exact=True).click()
            expect(modal.locator('.rocm-plan-summary')).to_contain_text('0 B')
            page.screenshot(path=str(args.output_dir/f'seed-plan-{locale}.png'), full_page=True)
            page.get_by_role('button', name=prepare, exact=True).last.click()
            page.wait_for_function("__calls.filter(c=>c.command==='start_runtime_install').length===2")
            assert page.evaluate("__calls.findLast(c=>c.command==='start_runtime_install').args.payload.action") == 'prepareSeed'
            assert not errors, errors
            summaries.append({'locale': locale, 'pageErrors': errors, 'previewAction': payload['action'], 'seedAction': 'prepareSeed', 'overflow': False})
            page.close()
    finally:
        browser.close()
(args.output_dir/'summary.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summaries, ensure_ascii=False))
