"""Exercise ROCm views with isolated deterministic IPC, without native mutations."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:1456')
parser.add_argument('--modal-only', action='store_true', help='Only verify the narrow ROCm results footer')
parser.add_argument('--output-dir', type=Path, default=Path('.cache/rocm-ui-smoke'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

BOOTSTRAP = r"""() => {
  const properties=document.querySelector('#app').__vue_app__.config.globalProperties;
  window.__app=properties.$pinia._s.get('app');window.__settings=properties.$pinia._s.get('settings');
  window.__router=properties.$router;window.__calls=[];window.__recoveryFails=true;
  window.__base={backend:'rocm',source:'managed',health:'ready',manifestVersion:'2026.11.2',
    profileId:'win-rocm721',baseFingerprint:'base-a',targetFingerprint:'target-a',installedTargets:['gfx1031'],
    pythonPath:'D:/Pymss/runtime-envs/rocm/Scripts/python.exe',torchVersion:'2.9.1',acceleratorAvailable:true,
    pymssVersion:'2.4.1',pymssCoreVersion:'0.1.11',pymssGraphAvailable:true,coreUpdateSupported:true,
    packages:{torch:true,pymss:true,'pymss-core':true,torchaudio:true},packageVersions:{pymss:'2.4.1','pymss-core':'0.1.11'}};
  window.__runtime={platform:'win32',machine:'AMD64',ready:true,manifestVersion:'2026.11.2',
    installedBackend:'rocm',torchBackend:'rocm',installState:structuredClone(__base),
    installedEnvironments:[structuredClone(__base)],gpuVendors:[],acceleratorAvailable:true,
    environmentRollbackAvailable:true,previousRuntime:{profileId:'win-rocm721',torchVersion:'2.9.0'},
    pymssVersion:'2.4.1',pymssCoreVersion:'0.1.11',packages:__base.packages};
  window.__env={platform:'win32',pythonVersion:'3.12.10',pymssAvailable:true,torchAvailable:true,
    torchBackend:'rocm',cudaAvailable:true,cudaDeviceCount:1,pymssVersion:'2.4.1'};
  window.__report={probeVersion:1,profileId:'win-rocm721',baseFingerprint:'base-a',targetFingerprint:'target-a',
    checkedAt:'2026-11-07T00:00:00Z',status:'partial',cacheReusable:false,deviceCapabilities:[{
      index:0,name:'AMD Radeon RX 6700 XT',gfx:'gfx1031',rawArchitecture:'gfx1031:sramecc-',
      stableId:'unknown',driverVersion:'unknown',basicAvailable:true,modes:[{
        mode:'miopen',status:'partial',basicAvailable:true,checks:[
          {id:'allocation',precision:'fp32',status:'passed',requiredForBasic:true,message:'Device allocation and synchronization completed.'},
          {id:'sdpa',precision:'fp32',status:'failed',requiredForBasic:false,message:'This optional attention operator is unavailable on the selected device.'}]}]}]};
  window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},
    transformCallback:()=>1,invoke:async(command,args={})=>{
      __calls.push({command,args});
      if(command==='runtime_info')return structuredClone(__runtime);
      if(command==='get_env_info')return structuredClone(__env);
      if(command==='runtime_env_sizes')return {sizes:{rocm:5368709120},incompleteBackends:[]};
      if(command==='runtime_core_versions')return {packages:{pymss:{currentVersion:'2.4.1',latestVersion:'2.4.1',updateAvailable:false}}};
       if(command==='runtime_install_plan')throw {code:'RUNTIME_PROFILE_INVALID',message:'Multi-architecture installation requires explicit targets.'};
      if(['start_runtime_gpu_probe','cancel_runtime_gpu_probe','start_runtime_rollback','cancel_runtime_install'].includes(command))return true;
      if(command==='recover_runtime'){
        if(__recoveryFails)throw {code:'RUNTIME_RECOVERY_FAILED',message:'The runtime directory is in use.',recoverable:true};
        __runtime.recoveryRequired=false;__runtime.recoveryError=null;return {recovered:true};
      }
      if(command==='start_runtime_install'||command==='start_separation'||command.startsWith('download_'))throw new Error('Forbidden mutation in UI smoke: '+command);
      return null;
    }};
  __app.runtimeInfo=structuredClone(__runtime);__app.envInfo=structuredClone(__env);
  __app.runtimeCoreVersions={packages:{pymss:{currentVersion:'2.4.1',latestVersion:'2.4.1',updateAvailable:false}}};
  __app.ensureRuntimeInfo=async()=>__app.runtimeInfo;
  __app.ensureRuntimeEnvSizes=async()=>{};__app.ensureRuntimeCoreVersions=async()=>__app.runtimeCoreVersions;
  __settings.modelDir='D:/Pymss/Models';__settings.outputDir='D:/Pymss/Output';
}"""


def assert_no_overflow(page, target=None):
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), page.evaluate('[document.documentElement.scrollWidth,innerWidth]')
    if target:
        assert page.locator(target).evaluate('el=>el.scrollWidth<=el.clientWidth+1'), target


def snapshot(page, name):
    page.screenshot(path=str(args.output_dir / name), full_page=True)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    all_errors = []
    all_console_errors = []
    try:
        def new_page(locale='zh-CN'):
            page=browser.new_page(viewport={'width':1366,'height':768})
            page.on('pageerror',lambda error: all_errors.append(str(error)))
            page.on('console',lambda message: all_console_errors.append(message.text) if message.type=='error' else None)
            page.add_init_script("localStorage.setItem('pymss-studio:app-settings',JSON.stringify("+json.dumps({
                'startupOnboardingSeen':False,'beginnerGuideInvitationSeen':True,'beginnerGuideProgress':{'status':'skipped','step':0},'locale':locale,'themeMode':'dark','animationsEnabled':False,
            })+"))")
            page.goto(args.url.rstrip('/')+'/index.html',wait_until='networkidle')
            expect(page.locator('.onboarding-card')).to_be_visible()
            page.evaluate(BOOTSTRAP)
            return page

        if args.modal_only:
            page=new_page('zh-CN')
            page.evaluate("__settings.startupOnboardingSeen=true;__router.push({name:'settings',query:{section:'runtime',backend:'rocm'}})")
            card=page.locator('[data-runtime-backend="rocm"]').first
            card.get_by_role('button',name='检查 GPU 兼容性',exact=True).click()
            page.wait_for_function("__calls.some(c=>c.command==='start_runtime_gpu_probe')")
            page.evaluate("__app.handleRuntimeEvent({type:'runtime_gpu_probe_finished',taskId:__app.runtimeGpuProbeTaskId,payload:structuredClone(__report)})")
            page.locator('.runtime-gpu-report details summary').first.click()
            page.set_viewport_size({'width':760,'height':600})
            actions=page.locator('.runtime-gpu-actions')
            expect(actions.get_by_role('button',name='检查 GPU 兼容性',exact=True)).to_be_in_viewport()
            expect(actions.get_by_role('button',name='关闭',exact=True)).to_be_in_viewport()
            expect(page.locator('.runtime-gpu-checks')).to_contain_text('设备运算')
            expect(page.locator('.runtime-gpu-checks')).to_contain_text('Attention')
            expect(page.locator('.runtime-gpu-checks')).not_to_contain_text('Device allocation and synchronization completed.')
            before=actions.bounding_box()['y']
            page.locator('.runtime-gpu-report').evaluate('el=>{el.parentElement.scrollTop=el.parentElement.scrollHeight}')
            assert abs(actions.bounding_box()['y']-before)<1
            assert_no_overflow(page,'.runtime-gpu-report')
            snapshot(page,'gpu-partial-footer-zh-CN.png')
            actions.get_by_role('button',name='关闭',exact=True).click()
            expect(page.locator('.runtime-gpu-report')).not_to_be_visible()
            page.close()

        for locale in ([] if args.modal_only else ['zh-CN','en']):
            page=new_page(locale)
            for _ in range(3):
                page.locator('.onboarding-content__foot .n-button--primary-type').click()
            expect(page.locator('.onboarding-runtime-card').filter(has_text='AMD ROCm')).to_have_attribute('aria-pressed','true')
            expect(page.locator('.onboarding-runtime-status')).to_contain_text('GPU')
            expect(page.locator('.onboarding-readiness').last).to_contain_text('GPU')
            assert_no_overflow(page,'.onboarding-content')
            snapshot(page,f'onboarding-rocm-unverified-{locale}.png')
            page.evaluate("__runtime.installedEnvironments[0].health='broken';__app.runtimeInfo=structuredClone(__runtime)")
            expect(page.locator('.onboarding-runtime-status')).to_contain_text('环境损坏' if locale=='zh-CN' else 'Runtime damaged')
            page.evaluate("__runtime.installedEnvironments[0].health='ready';__app.runtimeInfo=structuredClone(__runtime)")
            page.set_viewport_size({'width':760,'height':600})
            assert_no_overflow(page,'.onboarding-content')
            snapshot(page,f'onboarding-rocm-narrow-{locale}.png')
            check='检查 GPU 兼容性' if locale=='zh-CN' else 'Check GPU compatibility'
            page.get_by_role('button',name=check,exact=True).click()
            expect(page.locator('.onboarding-readiness').last).to_contain_text('正在检查' if locale=='zh-CN' else 'Checking')
            page.get_by_role('button',name='取消' if locale=='zh-CN' else 'Cancel',exact=True).click()
            page.wait_for_function("__app.runtimeGpuProbeStatus==='cancelled'")
            page.evaluate("__settings.startupOnboardingSeen=true;__router.push({name:'settings',query:{section:'runtime',backend:'rocm'}})")
            expect(page.locator('.runtime-section')).to_be_visible()
            card=page.locator('[data-runtime-backend="rocm"]').first
            expect(card).to_contain_text('win-rocm721')
            expect(card).to_contain_text('gfx1031')
            expect(card).to_contain_text('GPU')
            assert_no_overflow(page,'.runtime-section')
            snapshot(page,f'settings-rocm-narrow-{locale}.png')
            page.set_viewport_size({'width':1366,'height':768})
            card.get_by_role('button',name=check,exact=True).click()
            expect(page.locator('.runtime-gpu-report')).to_be_visible()
            expect(page.locator('.runtime-gpu-report')).to_have_attribute('aria-busy','true')
            fp16=page.locator('.runtime-gpu-report').get_by_role('checkbox',name='同时检查半精度（FP16）' if locale=='zh-CN' else 'Also check half precision (FP16)',exact=True)
            expect(fp16).not_to_be_checked()
            expect(fp16).to_be_disabled()
            page.wait_for_function("__calls.filter(c=>c.command==='start_runtime_gpu_probe').at(-1)?.args.payload.includeFp16===false")
            page.evaluate("__app.handleRuntimeEvent({type:'runtime_gpu_probe_stage',taskId:__app.runtimeGpuProbeTaskId,payload:{phase:'check'}})")
            snapshot(page,f'gpu-running-{locale}.png')
            page.locator('.runtime-gpu-actions').get_by_role('button',name='取消' if locale=='zh-CN' else 'Cancel',exact=True).click()
            page.wait_for_function("__app.runtimeGpuProbeStatus==='cancelled'")
            expect(fp16).to_be_enabled()
            fp16.check()
            page.locator('.runtime-gpu-actions').get_by_role('button',name=check,exact=True).click()
            expect(fp16).to_be_disabled()
            page.wait_for_function("__calls.filter(c=>c.command==='start_runtime_gpu_probe').at(-1)?.args.payload.includeFp16===true")
            page.evaluate("__app.handleRuntimeEvent({type:'runtime_gpu_probe_finished',taskId:__app.runtimeGpuProbeTaskId,payload:{...__report,status:'unavailable',deviceCapabilities:[]}})")
            expect(page.locator('.runtime-gpu-report')).to_contain_text('未发现可用的 AMD GPU' if locale=='zh-CN' else 'No available AMD GPU')
            snapshot(page,f'gpu-no-hardware-{locale}.png')
            page.locator('.runtime-gpu-actions').get_by_role('button',name=check,exact=True).click()
            page.evaluate("__app.handleRuntimeEvent({type:'runtime_gpu_probe_finished',taskId:__app.runtimeGpuProbeTaskId,payload:structuredClone(__report)})")
            expect(page.locator('.runtime-gpu-report')).to_contain_text('部分 GPU 功能不可用' if locale=='zh-CN' else 'Some GPU features are unavailable')
            page.locator('.runtime-gpu-report details summary').first.click()
            expect(page.locator('.runtime-gpu-checks')).to_contain_text('Attention')
            assert page.evaluate("__app.runtimeInfo.ready&&__app.runtimeInfo.installedEnvironments[0].health==='ready'")
            assert_no_overflow(page,'.runtime-gpu-report')
            snapshot(page,f'gpu-partial-{locale}.png')
            page.set_viewport_size({'width':760,'height':600})
            assert_no_overflow(page,'.runtime-gpu-report')
            snapshot(page,f'gpu-partial-narrow-{locale}.png')
            recheck=page.locator('.runtime-gpu-actions').get_by_role('button',name=check,exact=True)
            expect(recheck).to_be_in_viewport()
            page.keyboard.press('Escape')
            expect(page.locator('.runtime-gpu-report')).not_to_be_visible()
            restore='恢复上一版 ROCm 环境' if locale=='zh-CN' else 'Restore previous ROCm environment'
            card.get_by_role('button',name=restore,exact=True).click()
            confirmation=page.locator('.n-dialog')
            expect(confirmation).to_contain_text('2.9.1')
            expect(confirmation).to_contain_text('2.9.0')
            snapshot(page,f'rollback-confirmation-{locale}.png')
            confirmation.get_by_role('button',name='取消' if locale=='zh-CN' else 'Cancel',exact=True).click()
            assert not page.evaluate("__calls.some(c=>c.command==='start_runtime_rollback')")
            page.evaluate("__runtime.recoveryRequired=true;__app.runtimeInfo=structuredClone(__runtime)")
            recover='重试恢复环境' if locale=='zh-CN' else 'Retry environment recovery'
            page.get_by_role('button',name=recover,exact=True).first.click()
            confirmation=page.locator('.n-dialog')
            confirmation.get_by_role('button',name=recover,exact=True).click()
            page.wait_for_function("__app.runtimeMaintenanceStatus==='error'")
            expect(page.locator('.runtime-section')).to_contain_text('环境恢复失败' if locale=='zh-CN' else 'Runtime recovery failed')
            snapshot(page,f'recovery-failed-{locale}.png')
            page.evaluate('__recoveryFails=false')
            page.get_by_role('button',name=recover,exact=True).first.click()
            page.locator('.n-dialog').get_by_role('button',name=recover,exact=True).click()
            page.wait_for_function("__app.runtimeMaintenanceStatus==='success'")
            assert page.evaluate("__calls.filter(c=>c.command==='recover_runtime').length===2")
            assert not page.evaluate("__calls.some(c=>c.command==='start_runtime_install'||c.command==='start_separation'||c.command.startsWith('download_'))")
            assert_no_overflow(page,'.runtime-section')
            page.close()
        assert not all_errors, all_errors
        assert not all_console_errors, all_console_errors
    except Exception:
        if 'page' in locals() and not page.is_closed():
            snapshot(page,'failure.png')
            (args.output_dir/'failure-dom.html').write_text(page.content(),encoding='utf-8')
        (args.output_dir/'failure-errors.json').write_text(json.dumps({'pageerror':all_errors,'console.error':all_console_errors},ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    finally:
        browser.close()

print('PASS: narrow Chinese ROCm modal keeps localized results and fixed footer visible; no pageerror or console.error.' if args.modal_only else 'PASS: ROCm cards, startup readiness, running/cancelled/no-device/partial diagnostics, version-specific rollback confirmation, recovery retry, two locales and narrow layouts; no pageerror or console.error.')
