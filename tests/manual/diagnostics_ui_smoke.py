"""Check diagnostics with isolated stores and mocked desktop commands."""
import argparse
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://localhost:1438')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/diagnostics-ui'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def main_tab(name):
        page.locator('.debug-tabs').get_by_role('button', name=name, exact=True).click()

    def open_details(details):
        if not details.evaluate('el=>el.open'):
            details.locator('summary').first.click()

    def check_advanced_layout():
        assert page.locator('.debug-advanced-nav').evaluate("""el=>{
          const container=el.closest('.debug-navigation');
          const bar=container.getBoundingClientRect();
          const primary=container.querySelector('.debug-tabs').getBoundingClientRect();
          const panel=el.getBoundingClientRect();
          const content=container.nextElementSibling.getBoundingClientRect();
          const overlaps=Math.min(primary.right,panel.right)-Math.max(primary.left,panel.left)>0.5
            &&Math.min(primary.bottom,panel.bottom)-Math.max(primary.top,panel.top)>0.5;
          return panel.left>=bar.left&&panel.right<=bar.right&&panel.top>=bar.top&&!overlaps
            &&panel.bottom<=bar.bottom&&panel.bottom<=content.top
            &&panel.left>=0&&panel.right<=innerWidth;
        }"""), 'all navigation entries should be adjacent, visible and clear of page content'

    def advanced_tab(name):
        navigation = page.locator('.debug-advanced-nav')
        expect(navigation).to_be_visible()
        check_advanced_layout()
        navigation.get_by_role('button', name=name, exact=True).click()
        expect(navigation).to_be_visible()

    def task_details(task_id):
        page.locator('.debug-task-entry').filter(has_text=task_id).get_by_role('button', name='查看详情', exact=True).click()
        dialog = page.locator('.debug-execution-log-modal')
        expect(dialog.get_by_text('任务详情', exact=True)).to_be_visible()
        expect(dialog.locator('.worker-event-modal-meta')).to_contain_text(task_id)
        return dialog

    page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,locale:'zh-CN',themeMode:'light',developerMode:false
    }))""")
    page.goto(args.url.rstrip('/') + '/#/debug')
    page.wait_for_load_state('networkidle')
    expect(page.locator('.debug-page__lock')).to_be_visible()
    for button in page.locator('.debug-header-actions button').all():
        expect(button).to_be_disabled()
    page.evaluate("""async () => {
      const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      window.__app=stores.get('app');window.__settings=stores.get('settings');
      window.__tasks=stores.get('task');window.__workflow=stores.get('workflow');window.__models=stores.get('model');window.__updates=stores.get('update');
      window.__calls=[];window.__revealedPaths=[];window.__copied='';window.__runtimeFailure=false;window.__envFailure=false;window.__logFailure=false;
      window.__pluginRows=[];window.__verifyCalls=0;
      window.__captureRender=(await import('/src/utils/renderDiagnostics.ts')).captureRenderDiagnostics;
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async(text)=>window.__copied=text}});
      window.__env={pythonVersion:'3.12.10',platform:'Windows-11',workerVersion:'2.1.0',
        pymssAvailable:true,pymssVersion:'2.3.1',torchAvailable:true,torchVersion:'2.7.1',
        torchBackend:'cpu',cudaAvailable:false,avAvailable:true,librosaAvailable:true,customModelsSupported:true};
      window.__runtime={platform:'win32',machine:'AMD64',ready:true,installedBackend:'cpu',torchBackend:'cpu',
        torchVersion:'2.7.1',pymssVersion:'2.3.1',pymssGraphAvailable:true,packages:{audioread:true},
        installState:{backend:'cpu',pythonPath:'D:/runtime/cpu/python.exe'},installedEnvironments:[]};
      window.__logInfo={path:'D:/logs/session.log',logsDir:'D:/logs',exists:true,sizeBytes:240,
        persistentPath:'D:/logs/studio.log',persistentExists:true,persistentSizeBytes:240,
        maxBytes:1048576,persistentMaxBytes:1048576,reportExists:false,reportPath:'D:/logs/report.md'};
      window.__logText='2026-10-12T09:00:00Z INFO worker.event taskId=task_a message="Node started"\\n'
        +'2026-10-12T09:00:01Z ERROR worker.event taskId=task_b message="Decoder failed"';
      window.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},transformCallback:()=>1,
        invoke:async(command,args)=>{
          window.__calls.push(command);
          if(command==='runtime_info'){if(window.__runtimeFailure)throw new Error('Runtime probe failed');return structuredClone(window.__runtime);}
          if(command==='get_env_info'){if(window.__envFailure)throw new Error('Environment probe failed');return structuredClone(window.__env);}
          if(command==='optional_runtime_package_status')return {package:'funasr',requirement:window.__runtime.platform==='darwin'?'funasr-mlx>=0.2.2':'funasr==1.3.26',installed:false,present:false,version:null,source:null,runtimeIdentity:'cpu-py312'};
          if(command==='plugin_diagnostics'){
            if(args.payload.verify)window.__verifyCalls++;
            return {checkedAt:'2026-11-03T00:00:00Z',runtime:{pythonPath:__runtime.installState.pythonPath,configuredPythonPath:__runtime.installState.pythonPath,interpreterMatches:true,bindingDir:'D:/data/plugins/cpu',identity:'cpu',pymssVersion:'2.3.1',managementSupported:true,pluginApisSupported:true,apiError:null},
              plugins:__pluginRows.map(row=>({...row,loaded:args.payload.verify?true:null})),verification:{performed:args.payload.verify,error:null}};
          }
          if(command==='debug_plugin_source')return {defaults:{mode:'repository',repositoryUrl:'https://github.com/pymss-project/pymss-plugins',ref:'main',registryRef:'main'},effective:{mode:'repository',repositoryUrl:'https://github.com/pymss-project/pymss-plugins',ref:'main',registryRef:'main'},available:true,error:null,catalogCount:null,configPath:'D:/data/plugin-source.json'};
          if(command==='debug_log_info')return window.__logInfo;
          if(command==='debug_log_read'){if(window.__logFailure)throw new Error('Log read failed');return {path:'D:/logs/studio.log',content:window.__logText,sizeBytes:240,truncated:false};}
          if(command==='get_workflow_editors')return {revision:2,editors:[{label:'workflow-node-editor-flow',workflowId:'flow',kind:'advanced'}]};
          if(command==='debug_runtime_pointers')return {runtimeEnvsDir:'D:/runtime',activeRuntimeFile:'D:/runtime/active-runtime.json',files:[]};
          if(command==='begin_business_diagnostics'){window.__businessRequestId=args.requestId;return;}
          if(command==='update_proxy_settings')return;
          if(command==='network_diagnostics')return {checkedAt:'now',proxyMode:'system',checks:[
            {id:'models',status:'ok',source:'modelscope',host:'modelscope.cn',httpStatus:200,elapsedMs:10},
            {id:'pypi',status:'timeout',source:'ustc',host:'pypi.mirrors.ustc.edu.cn',elapsedMs:15000,error:'Request timed out'}]};
          if(command==='plugin_marketplace')return {catalog:[],catalogSource:{supported:true,provider:'gitcode',preference:'auto',gitAvailable:true}};
          if(command==='check_managed_update')return null;
          if(command==='inspect_diagnostic_storage')return {checkedAt:'now',writeProbe:args.writeProbe,directories:args.directories.map(directory=>({
            ...directory,state:'directory',availableBytes:1024*1024,writeStatus:args.writeProbe?'ok':'not_checked'}))};
          if(command==='get_renderer_diagnostics')return {checkedAt:'now',requestId:args.requestId,webviewVersion:'123.4',webviewError:null,os:'windows',arch:'x86_64',
            windows:{status:'observed',expected:['main'],missing:[],observed:[{kind:'renderer',windowLabel:'main',observedAt:'now',
              rendering:__captureRender({theme:__settings.themeMode,resolvedTheme:document.documentElement.classList.contains('dark-theme')?'dark':'light',
                animations:__settings.animationsEnabled,scale:__settings.scaleFactor,locale:__settings.locale,resolvedLocale:__settings.locale,routePath:'/debug'}),changes:[]}]}};
          if(command==='get_business_diagnostics')return {
            checkedAt:'2026-11-03T01:00:00Z',requestId:args.requestId,
            windows:{status:'observed',expected:['editor-a','workflow-node-editor-flow','editor-unresponsive'],missing:['editor-unresponsive'],observed:[
              {kind:'workflow',windowLabel:'workflow-node-editor-flow',observedAt:'now',workflowId:'flow',dirty:true,saving:false,revisionConflict:true,error:'Revision changed'},
              {kind:'editor',windowLabel:'editor-a',observedAt:'now',projectId:'project',exporting:true,exportPhase:'rendering',exportCompleted:40,exportTotal:100,playbackStatus:'paused',recordingState:'idle',lastExportPath:'D:/exports/last.wav',pluginStateError:'Native host unavailable'}]},
            nativeHost:{capabilities:{enabled:true,platform:'windows',formats:['vst3'],nativeEditor:true,nativeTransport:true},stopping:false,
              session:{status:'busy'},scan:{status:'scanning',completed:1,total:3},renders:{status:'active',activeCount:1},
              storage:{status:'observed',cache:{status:'observed',entryCount:3,descriptorCount:2,failureCount:1},blacklist:{status:'observed',count:1,manualCount:0,timeoutCount:1}}}};
          if(command==='inspect_diagnostic_outputs')return {checkedAt:'2026-11-03T01:00:00Z',files:args.paths.map(path=>({path,state:path.includes('missing')?'missing':'present'}))};
          if(command==='debug_catalog_info')return {baseCatalogPath:'D:/catalog.json',debugCatalogPath:'D:/debug/catalog.json',debugDir:'D:/debug',baseCatalog:{models:[]},debugCatalog:{models:[]},effectiveCatalog:{models:[]},status:{}};
          if(command==='list_models')return {models:[],categories:[],categoriesCn:[],modelDir:'D:/models'};
          if(command==='debug_log_create_report'){window.__reportSnapshot=args.payload;window.__logInfo.reportExists=true;return {path:'D:/logs/report.md',exists:true,sizeBytes:240};}
          if(command==='reveal_path'){window.__revealedPaths.push(args.path);return;}
          if(command==='save_app_store')return;
          if(command==='get_build_info')return {version:'0.0.12',variant:'default',updateSupported:false};
          if(command==='plugin:event|listen')return 1;
          throw new Error('Unexpected command: '+command);
        }};
      window.__app.envInfo=structuredClone(window.__env);
      window.__app.buildInfoVersion='0.0.24';
      window.__app.setWorkerEventConnection('connected');
      await window.__app.checkRuntimeInfo();
      window.__settings.developerMode=true;
      window.__settings.appPaths={...window.__settings.appPaths,dataRoot:'D:/data'};
    }""")
    overview = page.locator('.debug-status-card').first
    expect(overview).to_contain_text('正常')
    expect(page.locator('.debug-tabs button')).to_have_text(['概览', '任务排查', '环境与路径', '应用日志'])
    expect(page.locator('.debug-overview-summary')).to_be_visible()
    health = page.locator('.debug-health-details')
    assert not health.evaluate('el=>el.open'), 'healthy diagnostics should start folded'
    advanced = page.locator('.debug-advanced-nav')
    expect(advanced).to_be_visible()
    expect(page.locator('.debug-advanced-toggle')).to_have_count(0)
    expect(advanced.get_by_role('button')).to_have_count(7)
    check_advanced_layout()
    expect(page.locator('.diagnostic-row--optional').filter(has_text='FunASR')).to_contain_text('FunASR')
    expect(page.locator('.diagnostic-row--warn')).to_have_count(0)
    expect(page.locator('.debug-header-actions').get_by_role('button', name='重新检测', exact=True)).to_be_enabled()
    assert page.evaluate("!__calls.some(c=>['debug_catalog_info','debug_runtime_pointers','debug_log_read'].includes(c))")
    page.screenshot(path=str(args.output_dir / 'overview-light.png'), full_page=True)
    expect(page.locator('.debug-header-actions details')).to_have_count(0)
    expect(page.get_by_role('button', name='复制诊断摘要', exact=True)).to_be_visible()
    page.get_by_role('button', name='复制诊断摘要', exact=True).click()
    assert 'pymss: 正常' in page.evaluate('__copied')
    assert 'runtimeIdentity' not in page.evaluate('__copied')
    open_details(health)
    page.evaluate("__app.handleWorkerEvent({type:'error',taskId:'bad-input',payload:{code:'INVALID_INPUT',message:'Input file does not exist'}})")
    expect(overview).to_contain_text('正常')
    expect(page.locator('.diagnostic-row--error')).to_have_count(0)
    assert page.evaluate("__app.lastError==='Input file does not exist' && __app.envError===null")
    page.evaluate("__app.setWorkerEventConnection('error','Listener failed')")
    expect(page.locator('.diagnostic-row--error')).to_contain_text('Listener failed')
    page.locator('.diagnostic-row').get_by_role('button', name='重新连接', exact=True).click()
    expect(page.locator('.diagnostic-row--error')).to_have_count(0)

    # Unknown values and unavailable APIs are distinct states.
    page.evaluate('__app.runtimeInfo.pymssGraphAvailable=false;delete __app.envInfo.customModelsSupported')
    expect(page.locator('.diagnostic-row--warn')).to_contain_text('高级工作流 API')
    expect(page.locator('.diagnostic-row--unknown')).to_contain_text('本地模型导入')
    page.evaluate('__app.runtimeInfo.pymssGraphAvailable=true;__app.envInfo.customModelsSupported=true;__runtimeFailure=true')
    detect = page.locator('.debug-header-actions').get_by_role('button', name='重新检测', exact=True)
    detect.click()
    expect(page.locator('.diagnostic-row--error')).to_contain_text('Runtime probe failed')
    expect(page.locator('.diagnostic-row').filter(has_text='pymss')).to_contain_text('2.3.1')
    page.screenshot(path=str(args.output_dir / 'probe-failed.png'), full_page=True)
    page.evaluate('__runtimeFailure=false')
    detect.click()
    expect(page.locator('.diagnostic-row--error')).to_have_count(0)
    expect(detect).to_be_enabled()

    page.evaluate('__envFailure=true')
    detect.click()
    expect(page.locator('.diagnostic-row--error')).to_contain_text('Environment probe failed')
    page.evaluate("__app.handleWorkerEvent({type:'error',taskId:'bad-output',payload:{code:'OUTPUT_WRITE_FAILED',message:'Output file is locked'}})")
    expect(page.locator('.diagnostic-row--error')).to_contain_text('Environment probe failed')
    assert page.evaluate("__app.envError==='Environment probe failed'")
    page.evaluate('__envFailure=false')
    detect.click()
    expect(page.locator('.diagnostic-row--error')).to_have_count(0)
    assert page.evaluate('__app.envError===null')

    page.evaluate("""() => {
      const common={model:'voice.ckpt',input:'D:/audio/voice.wav',output:'D:/output',createdAt:1,
        updatedAt:Date.now(),message:'',progress:0,stageLabel:'',logs:[],files:[],outputs:[]};
      __tasks.tasks=[{...common,id:'task_a',status:'separating',progress:42,logs:['[INFO] Workflow started'],nodeLogs:{2:['[ERROR] Node decode failed']},runConfig:{runMode:'workflow',workflowId:'flow',workflowName:'人声处理'},batchId:'batch_1',batchIndex:0},
        {...common,id:'task_missing',status:'queued'}, {...common,id:'task_b',status:'failed',error:'Decoder failed'}];
      __workflow.workflows=[{id:'flow',name:'人声处理',definition:{},format:'graph',createdAt:1,updatedAt:1}];
      __workflow.initialized=true;
      __models.downloadTasks={'download-voice.ckpt':{taskId:'download_task',model:'download-voice.ckpt',status:'downloading',progress:27,
        message:'Downloading model archive',completedFiles:0,totalFiles:2,updatedAt:Date.now(),
        logs:[{ts:Date.now()-1,level:'info',message:'Downloading model archive'},
          {ts:Date.now(),level:'warn',message:'Retrying download mirror',record:{schemaVersion:1,id:'download-log-1',
            timestamp:'2026-10-12T09:00:03Z',source:'third_party',level:'warn',logger:'aria2',event:'download.log',
            message:'Retrying download mirror',detail:'HTTP 503\\nMirror unavailable',taskId:'download_task',
            requestId:'download-request',location:{file:'downloader.py',line:42},context:{mirrorHost:'mirror.example'}}}]}};
      __app.workerEvents=[{type:'workflow_execution',taskId:'task_a',timestamp:'2026-10-12T09:00:02Z',payload:{version:1,event:'node_progress',nodeId:'2'}},
        {type:'error',taskId:'task_b',timestamp:'2026-10-12T09:00:01Z',payload:{message:'Decoder failed'}}];
    }""")
    expect(page.locator('.debug-latest-failure')).to_contain_text('Decoder failed')
    page.locator('.debug-latest-failure').get_by_role('button', name='查看详情', exact=True).click()
    expect(page.locator('.debug-execution-log-error')).to_have_text('Decoder failed')
    page.keyboard.press('Escape')
    expect(page.locator('.debug-overview-summary')).to_be_visible()
    main_tab('任务排查')
    expect(page.locator('.debug-activity-grid')).to_contain_text('工作流任务')
    expect(page.locator('.debug-activity-grid')).to_contain_text('批量任务 #1')
    expect(page.locator('.debug-activity-grid')).to_contain_text('高级编辑器')
    failures = page.locator('.debug-failed-tasks')
    expect(failures.locator('[role="listitem"]')).to_have_count(1)
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 1–1 / 1 条')
    expect(failures.locator('.n-pagination')).to_have_count(0)
    expect(page.locator('.debug-task-entry__actions').get_by_role('button', name='应用日志', exact=True)).to_have_count(0)
    download_detail = task_details('download_task')
    expect(download_detail).to_contain_text('Downloading model archive')
    expect(download_detail).to_contain_text('Retrying download mirror')
    expect(download_detail.locator('.debug-task-detail-actions').get_by_role('button', name='查看应用日志', exact=True)).to_be_visible()
    expect(download_detail.locator('.debug-task-detail-actions').get_by_role('button', name='查看原始事件', exact=True)).to_be_visible()
    download_detail.get_by_role('button', name='复制', exact=True).click()
    assert 'Downloading model archive' in page.evaluate('__copied')
    assert all(value in page.evaluate('__copied') for value in ['download-request', 'mirrorHost', 'HTTP 503', 'downloader.py'])
    page.evaluate("""() => {
      const previous=__models.downloadTasks['download-voice.ckpt'];
      __models.downloadTasks['download-voice.ckpt']={...previous,progress:66,
        logs:[...previous.logs,{ts:Date.now(),level:'info',message:'Download tail refreshed'}]};
    }""")
    expect(download_detail).to_contain_text('Download tail refreshed')
    page.evaluate("""() => {
      __models.downloadStates['download-voice.ckpt']='done';
      delete __models.downloadTasks['download-voice.ckpt'];
    }""")
    expect(download_detail).to_contain_text('已下载')
    expect(download_detail).to_contain_text('Downloading model archive')
    expect(download_detail).to_contain_text('Download tail refreshed')
    download_detail.get_by_role('button', name='复制', exact=True).click()
    assert all(value in page.evaluate('__copied') for value in ['download-request', 'mirrorHost', 'HTTP 503', 'downloader.py', 'Download tail refreshed'])
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    page.screenshot(path=str(args.output_dir / 'download-completed-detail.png'), full_page=True)
    download_detail.locator('.n-card-header__close').click()
    expect(page.locator('.debug-activity-grid')).to_be_visible()
    page.evaluate("""() => {
      window.__originalTasks=JSON.parse(JSON.stringify(__tasks.tasks));
      const base=__tasks.tasks.find(item=>item.id==='task_b');
      __tasks.tasks=[...__tasks.tasks.filter(item=>item.status!=='failed'),
        ...Array.from({length:23},(_,index)=>({...base,id:'failure_'+(index+1),
          updatedAt:index+1,error:index===22?Array.from({length:100},(_,line)=>'Decoder traceback line '+line).join('\\n'):'Decoder failed '+(index+1)}))];
      window.__taskOrder=__tasks.tasks.map(item=>item.id);
    }""")
    expect(failures.locator('[role="listitem"]')).to_have_count(5)
    expect(failures.locator('[role="listitem"] > code').first).to_have_text('failure_23')
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 1–5 / 23 条')
    first_error = failures.locator('.debug-task-entry__error').first
    assert first_error.evaluate('el=>el.getBoundingClientRect().height<=60')
    failures.locator('[role="listitem"]').first.get_by_role('button', name='查看详情', exact=True).click()
    expect(page.locator('.debug-execution-log-error')).to_contain_text('Decoder traceback line 99')
    assert page.locator('.debug-execution-log-error').evaluate('el=>el.scrollHeight>el.clientHeight')
    page.keyboard.press('Escape')
    failures.locator('.n-pagination-item').filter(has_text=re.compile(r'^2$')).click()
    expect(failures.locator('[role="listitem"] > code').first).to_have_text('failure_18')
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 6–10 / 23 条')
    failures.locator('[role="listitem"]').first.get_by_role('button', name='查看详情', exact=True).click()
    page.keyboard.press('Escape')
    expect(failures.locator('[role="listitem"] > code').first).to_have_text('failure_18')
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 6–10 / 23 条')
    assert page.evaluate('__tasks.tasks.map(item=>item.id).join()===__taskOrder.join()')
    failures.locator('.n-pagination-item').filter(has_text=re.compile(r'^5$')).click()
    expect(failures.locator('[role="listitem"]')).to_have_count(3)
    expect(failures.locator('[role="listitem"] > code').last).to_have_text('failure_1')
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 21–23 / 23 条')
    page.screenshot(path=str(args.output_dir / 'failed-tasks-pagination.png'), full_page=True)
    page.evaluate("__tasks.tasks=__tasks.tasks.filter(item=>item.status!=='failed'||['failure_22','failure_23'].includes(item.id))")
    expect(failures.locator('[role="listitem"]')).to_have_count(2)
    expect(failures.locator('.debug-failed-task-pagination')).to_contain_text('显示 1–2 / 2 条')
    expect(failures.locator('.n-pagination')).to_have_count(0)
    page.evaluate("__tasks.tasks=__tasks.tasks.filter(item=>item.status!=='failed')")
    expect(failures).to_contain_text('暂无失败任务记录。')
    expect(failures.locator('.debug-failed-task-pagination')).to_have_count(0)
    page.evaluate("""() => {
      __tasks.tasks=JSON.parse(JSON.stringify(__originalTasks));
      __tasks.tasks.find(item=>item.id==='task_b').message='Decoder failed without an error field';
      delete __tasks.tasks.find(item=>item.id==='task_b').error;
    }""")
    failures.get_by_role('button', name='查看详情', exact=True).click()
    expect(page.locator('.debug-execution-log-error')).to_have_text('Decoder failed without an error field')
    page.keyboard.press('Escape')
    page.evaluate('__tasks.tasks=__originalTasks')
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    page.screenshot(path=str(args.output_dir / 'activity.png'), full_page=True)
    page.evaluate("""() => {
      const task=__tasks.tasks.find(item=>item.id==='task_a');
      window.__traceback=[
        '[ERROR] Traceback (most recent call last):',
        '[ERROR]   File "D:/app/python/worker_workflows.py", line 1420, in cmd_infer_workflow',
        '[ERROR]     saved = graph.run_dag(',
        '[ERROR]             ^^^^^^^^^^^^^^',
        '[ERROR]   File "D:/runtime/pymss/graph/nodes.py", line 438, in _execute_save_audio',
        '[ERROR]     raise DAGError("pymss_save_audio requires an AUDIO input")',
        '[ERROR] pymss.graph.errors.DAGError: pymss_save_audio requires an AUDIO input'
      ];
      task.logs=['[INFO] Workflow started',...Array.from({length:80},(_,i)=>
        '[INFO] Separator configuration '+i+': D:/Models/voice/model.ckpt '+ 'configuration '.repeat(12)),...__traceback.slice(0,4)];
      task.nodeLogs={2:['[ERROR] Node decode failed',...__traceback],...Object.fromEntries(Array.from({length:8},(_,i)=>
        [i+3,Array.from({length:6},(_,j)=>'[INFO] Node '+(i+3)+' log '+j)]))};
    }""")
    modal = task_details('task_a')
    expect(modal).to_contain_text('Workflow started')
    expect(modal).to_have_css('opacity', '1')
    assert modal.evaluate('el=>el.getBoundingClientRect().width<=960')
    page.screenshot(path=str(args.output_dir / 'execution-logs-initial.png'), full_page=True)
    assert modal.evaluate('el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight}'), 'execution log modal exceeds the viewport'
    console = modal.locator('.debug-execution-log-body > .log-console').first
    expect(console.locator('.log-line')).to_have_count(82)
    traceback = console.locator('.log-line').filter(has_text='Traceback (most recent call last):')
    expect(traceback).to_have_count(1)
    page.evaluate("__tasks.tasks.find(item=>item.id==='task_a').logs.push(...__traceback.slice(4),'[INFO] Worker finished')")
    expect(console.locator('.log-line')).to_have_count(83)
    expected_traceback = '\n'.join(line[len('[ERROR] '):] for line in page.evaluate('__traceback'))
    expect(traceback.locator('.log-line-text')).to_have_text(expected_traceback, use_inner_text=False)
    assert traceback.locator('.log-line-text').text_content() == expected_traceback
    expect(traceback.locator('.log-line-level')).to_have_count(1)
    expect(traceback.locator('.log-line-number')).to_have_text('082–088')
    expect(traceback.locator('.log-line-text')).to_have_css('font-weight', '400')
    expect(console.locator('.log-source-badge')).to_have_count(0)
    expect(console.locator('.log-line').last).to_contain_text('Worker finished')
    page.locator('.debug-node-logs summary').first.click()
    expect(page.locator('.debug-node-logs').first).to_contain_text('Node decode failed')
    expect(page.locator('.debug-node-logs').first.locator('.log-line')).to_have_count(2)
    modal.get_by_role('button', name='复制', exact=True).click()
    assert '[node:2]' in page.evaluate('__copied') and 'Node decode failed' in page.evaluate('__copied')
    assert 'configuration 79:' in page.evaluate('__copied') and 'Node 10 log 5' in page.evaluate('__copied')
    assert '    saved = graph.run_dag(' in page.evaluate('__copied')
    assert 'pymss.graph.errors.DAGError:' in page.evaluate('__copied')
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    traceback.scroll_into_view_if_needed()
    page.screenshot(path=str(args.output_dir / 'execution-traceback.png'))
    for summary in page.locator('.debug-node-logs summary').all()[1:]:
        summary.click()
    for width, height, theme in [(1280, 850, 'light'), (1000, 600, 'dark'), (820, 600, 'light')]:
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate("theme=>__settings.themeMode=theme", theme)
        body = modal.locator('.debug-execution-log-body')
        body.evaluate('el=>el.scrollTop=0')
        assert modal.evaluate("el=>{const r=el.getBoundingClientRect();const top=document.querySelector('.title-bar')?.getBoundingClientRect().bottom||0;return r.top>=top&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth}")
        assert body.evaluate('el=>el.scrollHeight>el.clientHeight')
        assert modal.locator('.log-console').evaluate_all("els=>els.every(el=>getComputedStyle(el).overflowY==='visible')")
        header_top = modal.locator('.n-card-header').evaluate('el=>el.getBoundingClientRect().top')
        body.evaluate('el=>el.scrollTop=el.scrollHeight')
        expect(modal.get_by_text('Node 10 log 5', exact=True)).to_be_in_viewport()
        assert modal.locator('.n-card-header').evaluate('el=>el.getBoundingClientRect().top') == header_top
        expect(modal.get_by_role('button', name='复制', exact=True)).to_be_in_viewport()
        expect(modal.locator('.n-card-header__close')).to_be_in_viewport()
        page.screenshot(path=str(args.output_dir / f'execution-logs-{width}-{theme}.png'))
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.evaluate("__settings.themeMode='light'")
    modal.locator('.debug-execution-log-body').evaluate('el=>el.scrollTop=0')
    page.screenshot(path=str(args.output_dir / 'execution-logs.png'), full_page=True)
    modal.locator('.n-card-header__close').click()
    expect(modal).to_be_hidden()
    modal = task_details('task_a')
    modal.locator('.debug-task-detail-actions').get_by_role('button', name='查看应用日志', exact=True).click()
    expect(modal).to_be_hidden()
    expect(page.locator('.debug-tabs').get_by_role('button', name='应用日志', exact=True)).to_have_attribute('aria-current', 'page')
    expect(page.locator('.debug-log-row')).to_have_count(1)
    expect(page.locator('.debug-log-row')).to_contain_text('Node started')
    page.get_by_role('button', name='复制筛选日志', exact=True).click()
    assert 'task_a' in page.evaluate('__copied') and 'task_b' not in page.evaluate('__copied')
    expect(page.get_by_role('button', name='生成诊断报告', exact=True)).to_have_count(1)
    expect(page.locator('.debug-log-toolbar').get_by_role('button', name='生成诊断报告', exact=True)).to_have_count(0)
    expect(page.locator('.debug-log-toolbar').get_by_role('button', name='打开诊断报告', exact=True)).to_have_count(0)
    # Failed refresh keeps the previous tail and displays the actual read error.
    page.evaluate('__logFailure=true')
    page.locator('.debug-log-toolbar').get_by_role('button', name='刷新', exact=True).click()
    expect(page.locator('.debug-log-read-error')).to_have_text('Log read failed')
    expect(page.locator('.debug-log-row')).to_have_count(1)
    page.evaluate('__logFailure=false')
    main_tab('任务排查')
    modal = task_details('task_missing')
    modal.locator('.debug-task-detail-actions').get_by_role('button', name='查看应用日志', exact=True).click()
    expect(modal).to_be_hidden()
    expect(page.locator('.debug-log-row')).to_have_count(0)
    expect(page.locator('.debug-log-filters')).to_contain_text('task_missing')
    page.get_by_role('button', name='清空日志', exact=True).click()
    expect(page.get_by_text('将清空会话日志、累计日志及其备份，并删除现有诊断报告。是否继续？', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    assert page.evaluate("!__calls.includes('debug_log_clear')")

    main_tab('任务排查')
    modal = task_details('task_a')
    modal.locator('.debug-task-detail-actions').get_by_role('button', name='查看原始事件', exact=True).click()
    expect(modal).to_be_hidden()
    expect(page.locator('.debug-event-toolbar input')).to_have_value('task_a')
    expect(page.locator('.worker-event-row')).to_have_count(1)
    expect(page.locator('.worker-event-row')).to_contain_text('task_a')
    main_tab('任务排查')
    expect(page.locator('.debug-task-entry').filter(has_text='task_a').get_by_role('button', name='查看详情', exact=True)).to_be_visible()
    advanced_tab('Worker 事件')
    page.locator('.debug-event-toolbar input').fill('')
    expect(page.locator('.worker-event-row')).to_have_count(2)
    page.locator('.debug-event-toolbar input').fill('task_a')
    expect(page.locator('.worker-event-row')).to_have_count(1)
    page.locator('.debug-event-toolbar input').fill('')
    page.locator('.debug-event-toolbar').get_by_text('只看异常', exact=True).click()
    expect(page.locator('.worker-event-row')).to_have_count(1)
    expect(page.locator('.worker-event-row')).to_contain_text('task_b')
    page.locator('.worker-event-row').click()
    expect(page.locator('.worker-event-modal-payload')).to_contain_text('Decoder failed')
    page.keyboard.press('Escape')
    page.evaluate("""() => {
      __app.workerEvents=[
        {type:'workflow_execution',taskId:'node_failure',timestamp:'2026-10-12T09:00:01Z',payload:{version:1,event:'node_failed',nodeId:'2',message:'Node decode failed'}},
        {type:'task_log',taskId:'recoverable_warning',timestamp:'2026-10-12T09:00:01Z',payload:{level:'warning',message:'Decoder fallback'}},
        {type:'task_done',taskId:'recoverable_warning',timestamp:'2026-10-12T09:00:02Z',payload:{}},
        {type:'task_log',taskId:'recoverable_error',timestamp:'2026-10-12T09:00:01Z',payload:{level:'error',message:'Recovered decoder error'}},
        {type:'workflow_execution',taskId:'normal_node',timestamp:'2026-10-12T09:00:02Z',payload:{version:1,event:'node_completed'}},
        {type:'task_log',taskId:'normal_log',timestamp:'2026-10-12T09:00:02Z',payload:{level:'info',message:'No error detected'}},
      ];
    }""")
    expect(page.locator('.worker-event-row')).to_have_count(3)
    expect(page.locator('.worker-event-list')).to_contain_text('node_failure')
    expect(page.locator('.worker-event-list')).to_contain_text('recoverable_error')
    expect(page.locator('.worker-event-list')).to_contain_text('recoverable_warning')
    page.locator('.debug-event-toolbar input').fill('recoverable_warning')
    expect(page.locator('.worker-event-row')).to_have_count(1)
    page.locator('.worker-event-row').click()
    expect(page.locator('.worker-event-modal-payload')).to_contain_text('Decoder fallback')
    page.keyboard.press('Escape')

    page.locator('.debug-event-toolbar input').fill('')
    page.evaluate("""() => {
      __app.workerEvents=[
        {type:'download_done',taskId:'download_fallback',timestamp:'2026-10-12T09:00:02Z',payload:{model:'voice.ckpt',downloaded:['D:/models/voice.ckpt'],progress:100}},
        {type:'download_log',taskId:'download_fallback',timestamp:'2026-10-12T09:00:01Z',payload:{level:'warning',message:'aria2c failed; retrying with urllib'}},
        {type:'download_log',taskId:'normal_download',timestamp:'2026-10-12T09:00:01Z',payload:{level:'info',message:'Download complete'}},
      ];
    }""")
    expect(page.locator('.worker-event-row')).to_have_count(1)
    expect(page.locator('.worker-event-row')).to_contain_text('download_fallback')
    page.locator('.worker-event-row').click()
    expect(page.locator('.worker-event-modal-payload')).to_contain_text('aria2c failed; retrying with urllib')
    expect(page.locator('.worker-event-modal-payload')).to_contain_text('download_done')
    page.keyboard.press('Escape')

    main_tab('环境与路径')
    expect(page.locator('.debug-environment-grid')).to_be_visible()
    expect(page.locator('.debug-environment-grid')).to_contain_text('D:/runtime/cpu/python.exe')
    expect(page.locator('.debug-environment-grid input')).to_have_count(0)
    assert page.evaluate("__calls.filter(c=>c==='debug_runtime_pointers').length") == 0
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'environment-paths.png'), full_page=True)
    page.locator('.path-debug-row').filter(has_text='D:/data').get_by_role('button', name='复制', exact=True).click()
    assert page.evaluate('__copied') == 'D:/data'
    page.locator('.debug-header-actions').get_by_role('button', name='生成诊断报告', exact=True).click()
    page.wait_for_function("__calls.includes('reveal_path')")
    assert page.evaluate("__calls.filter(c=>c==='debug_log_create_report').length") == 1
    expect(page.get_by_role('button', name='打开诊断报告', exact=True)).to_have_count(0)
    assert page.evaluate('__revealedPaths') == ['D:/logs/report.md']

    advanced_tab('环境配置')
    assert page.evaluate("__calls.filter(c=>c==='debug_runtime_pointers').length") == 1
    expect(page.get_by_text('可编辑配置文件', exact=True)).to_be_visible()
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'advanced-environment-config.png'), full_page=True)
    expect(advanced).to_be_visible()
    expect(page.locator('.debug-advanced-nav button')).to_have_text(['模型调试', 'Worker 事件', '环境配置', '插件', '应用更新', '编辑器与 VST', '系统检查'])

    advanced_tab('插件')
    expect(page.locator('.plugin-diagnostics-panel')).to_contain_text('D:/runtime/cpu/python.exe')
    assert page.evaluate('__verifyCalls') == 0
    assert page.evaluate("!__calls.some(c=>['plugin_marketplace','manage_plugin'].includes(c))")
    page.evaluate("__pluginRows=[{name:'example.provider',version:'1.0',enabled:true,path:'D:/plugins/example',error:null}]")
    page.locator('.plugin-diagnostics-panel').get_by_role('button', name='刷新', exact=True).click()
    expect(page.locator('.plugin-diagnostics-panel')).to_contain_text('尚未验证')
    assert page.evaluate('__verifyCalls') == 0
    page.locator('.plugin-diagnostics-panel').get_by_role('button', name='验证插件加载', exact=True).click()
    expect(page.locator('.plugin-diagnostics-panel')).to_contain_text('验证通过')
    assert page.evaluate('__verifyCalls') == 1
    page.screenshot(path=str(args.output_dir / 'plugin-diagnostics.png'), full_page=True)

    page.evaluate("__updates.status='failed';__updates.error='Update checksum mismatch';__updates.selectedSource='github';__updates.integrity={supported:true,valid:false,repairSupported:true,detail:'Resource changed'}")
    advanced_tab('应用更新')
    expect(page.locator('.update-diagnostics-panel')).to_contain_text('Update checksum mismatch')
    expect(page.locator('.update-diagnostics-panel')).to_contain_text('0.0.24')
    expect(page.locator('.update-diagnostics-panel')).to_contain_text('Resource changed')
    assert page.evaluate("__calls.filter(c=>['check_managed_update','start_managed_update'].includes(c)).length") == 0
    page.screenshot(path=str(args.output_dir / 'update-diagnostics.png'), full_page=True)
    page.locator('.debug-header-actions').get_by_role('button', name='生成诊断报告', exact=True).click()
    page.wait_for_function("__reportSnapshot?.applicationUpdate?.error==='Update checksum mismatch'")
    assert page.evaluate('__reportSnapshot.pluginDiagnostics.snapshot.verification.performed')

    advanced_tab('系统检查')
    expect(page.locator('.render-diagnostics')).to_contain_text('123.4')
    assert page.evaluate("!__calls.some(c=>['network_diagnostics','inspect_diagnostic_storage'].includes(c))")
    page.locator('.network-diagnostics').get_by_role('button', name='手动检查网络', exact=True).click()
    expect(page.locator('.network-diagnostics')).to_contain_text('gitcode')
    expect(page.locator('.network-diagnostics')).to_contain_text('Request timed out')
    expect(page.locator('.network-diagnostics')).to_contain_text('HTTP 200')
    assert page.evaluate("__calls.filter(c=>c==='check_managed_update').length") == 1
    page.locator('.storage-diagnostics').get_by_role('button', name='读取目录状态', exact=True).click()
    expect(page.locator('.storage-diagnostics-row')).to_have_count(7)
    expect(page.locator('.storage-diagnostics')).to_contain_text('未检查')
    page.locator('.storage-diagnostics').get_by_role('button', name='创建临时文件验证写入', exact=True).click()
    expect(page.locator('.storage-diagnostics')).to_contain_text('写入与清理通过')
    page.screenshot(path=str(args.output_dir / 'system-diagnostics.png'), full_page=True)
    page.locator('.debug-header-actions').get_by_role('button', name='生成诊断报告', exact=True).click()
    page.wait_for_function('__reportSnapshot?.networkDiagnostics?.checks?.length===4 && __reportSnapshot?.storageDiagnostics?.writeProbe===true && __reportSnapshot?.rendererDiagnostics?.webviewVersion==="123.4"')
    assert page.evaluate("!__calls.some(c=>['start_managed_update','manage_plugin'].includes(c))")

    page.evaluate("__runtime.platform='darwin';__runtime.machine='arm64'")
    page.locator('.debug-header-actions').get_by_role('button', name='重新检测', exact=True).click()
    page.wait_for_function("__app.runtimeInfo.platform==='darwin'")
    main_tab('概览')
    open_details(page.locator('.debug-health-details'))
    expect(page.locator('.debug-health-grid')).to_contain_text('FunASR MLX')
    page.screenshot(path=str(args.output_dir / 'advanced-tools.png'), full_page=True)
    advanced_tab('模型调试')
    page.wait_for_function("__calls.includes('debug_catalog_info')")
    expect(page.locator('.debug-model-workbench')).to_be_visible()
    debug_directory = page.locator('.debug-editor-meta .debug-task-context').filter(has_text='Debug 目录')
    debug_directory.get_by_role('button', name='打开', exact=True).click()
    page.wait_for_function("__revealedPaths.at(-1)==='D:/debug'")
    debug_directory.get_by_role('button', name='复制', exact=True).click()
    assert page.evaluate('__copied') == 'D:/debug'
    advanced_tab('应用更新')
    expect(page.locator('.update-endpoint-panel')).to_be_visible()
    main_tab('概览')
    page.evaluate("__settings.themeMode='dark'")
    page.set_viewport_size({'width': 1000, 'height': 760})
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    expect(page.locator('.n-message')).to_have_count(0, timeout=10000)
    page.screenshot(path=str(args.output_dir / 'overview-dark.png'), full_page=True)
    check_advanced_layout()
    page.screenshot(path=str(args.output_dir / 'navigation-dark.png'))
    page.evaluate("__settings.locale='en'")
    open_details(page.locator('.debug-health-details'))
    expect(page.locator('.debug-health-grid')).to_contain_text('Advanced workflow API')
    page.set_viewport_size({'width': 820, 'height': 800})
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'overview-en-narrow.png'), full_page=True)
    check_advanced_layout()
    page.screenshot(path=str(args.output_dir / 'navigation-en-narrow.png'))
    advanced_tab('Plugins')
    expect(page.locator('.plugin-diagnostics-panel')).to_contain_text('Actual worker interpreter')
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'plugin-diagnostics-en-narrow.png'), full_page=True)
    advanced_tab('Application updates')
    expect(page.locator('.update-diagnostics-panel')).to_contain_text('Update checksum mismatch')
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'update-diagnostics-en-narrow.png'), full_page=True)
    advanced_tab('System checks')
    page.locator('.render-diagnostics').get_by_role('button', name='Refresh', exact=True).click()
    expect(page.locator('.render-diagnostics')).to_contain_text('dark / dark → dark')
    assert page.locator('.debug-page').evaluate('el=>el.scrollWidth<=el.clientWidth')
    page.screenshot(path=str(args.output_dir / 'system-diagnostics-en-narrow.png'), full_page=True)
    page.evaluate("""() => {
      const task=(id,fields={})=>({id,model:'model',input:'input.wav',output:'D:/outputs',status:'done',message:'',createdAt:1,updatedAt:1,progress:100,stageLabel:'',files:[],outputs:[],logs:[],...fields});
      __tasks.tasks=[task('diagnostic-files',{files:['D:/outputs/exists.txt','D:/outputs/missing.txt']}),
        task('diagnostic-hidden',{resultHidden:true,files:['D:/outputs/hidden.txt']}),task('diagnostic-empty'),
        task('diagnostic-value',{nodeValues:[{nodeId:'metric',nodeType:'analysis',slot:0,value:'42'}]})];
      __workflow.recoveryBackupPath='D:/settings/workflows.backup.json';
      __workflow.lastMutationError={code:'WORKFLOW_REVISION_CONFLICT',message:'Revision changed',at:'now'};
    }""")
    expect(page.locator('.workflow-recovery-modal')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('.workflow-recovery-modal')).to_be_hidden()
    page.locator('.debug-tabs button').nth(1).click()
    expect(page.locator('.workflow-result-diagnostics')).to_contain_text('WORKFLOW_REVISION_CONFLICT')
    expect(page.locator('.workflow-result-diagnostics')).to_contain_text('workflows.backup.json')
    assert page.evaluate("!__calls.includes('inspect_diagnostic_outputs')")
    page.locator('.workflow-result-diagnostics').get_by_role('button', name='Check recorded outputs', exact=True).click()
    expect(page.locator('.workflow-result-diagnostics')).to_contain_text('D:/outputs/missing.txt')
    expect(page.locator('.workflow-result-diagnostics')).to_contain_text('Missing')
    assert page.evaluate("__tasks.tasks.find(t=>t.id==='diagnostic-hidden').resultHidden")
    page.screenshot(path=str(args.output_dir / 'workflow-results-diagnostics-en-narrow.png'), full_page=True)
    advanced_tab('Editor and VST')
    expect(page.locator('.editor-host-diagnostics')).to_contain_text('editor-unresponsive')
    expect(page.locator('.editor-host-diagnostics')).to_contain_text('Busy; not observed')
    expect(page.locator('.editor-host-diagnostics')).to_contain_text('rendering 40 / 100')
    expect(page.locator('.editor-host-diagnostics')).to_contain_text('Native host unavailable')
    assert page.evaluate("!__calls.some(c=>['plugin_host_request','plugin_host_scan','plugin_host_scan_report','plugin_host_render'].includes(c))")
    page.screenshot(path=str(args.output_dir / 'editor-host-diagnostics-en-narrow.png'), full_page=True)
    page.locator('.debug-header-actions').get_by_role('button', name='Create diagnostic report', exact=True).click()
    page.wait_for_function("__reportSnapshot?.businessDiagnostics?.windows?.missing?.length===1")
    assert page.evaluate("__reportSnapshot.outputChecks.files.some(f=>f.state==='missing')")
    main_tab('Overview')
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('.debug-status-card').first.evaluate('el=>getComputedStyle(el).transitionDuration') == '0s'
    assert not errors, errors
    browser.close()

print('PASS: directly accessible grouped navigation, direct summary action, task/download details and filtered jumps, diagnostic states, lazy reads, refresh recovery, pagination context, traceback grouping, modal sizing, reports, themes and narrow layout')
