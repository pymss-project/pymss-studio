"""Run against Vite to check live messages, quiet progress and model keyboard focus."""
import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/activity-accessibility')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        checks = []
        page.goto(args.base_url + '/tests/fixtures/activity-accessibility.html', wait_until='networkidle')
        live = page.locator('[data-app-announcements]')
        expect(live).to_have_attribute('role', 'status')
        expect(live).to_have_attribute('aria-live', 'polite')
        page.locator('#notify').click()
        expect(page.locator('#app-message-target .n-message')).to_have_text('已保存')
        assert page.locator('.n-message').evaluate('el => !!el.closest("[role=status][aria-live=polite]")')
        page.locator('#error').click()
        expect(page.locator('#app-message-target')).to_contain_text('无法保存文件')
        checks.append('ordinary success and error messages are inside a persistent polite live region')

        page.evaluate("""async () => {
          const {task, model, message} = activityFixture
          message.destroyAll()
          task.tasks = [{id:'old', input:'C:/old.wav', model:'Model A', status:'done', progress:100, files:[], outputs:[], logs:[]}]
          model.downloadTasks = {old: {taskId:'old-download', model:'old', status:'error', progress:0, logs:[]}}
          task.initialized = true; model.initialized = true
        }""")
        page.wait_for_timeout(900)
        expect(live.locator('.app-announcement-text').first).to_be_empty()
        page.evaluate("""() => {
          activityFixture.task.tasks.push({id:'new', input:'C:/audio.wav', model:'Model A', status:'preparing', progress:0, createdAt:Date.now(), updatedAt:Date.now(), files:[], outputs:[], logs:[]})
        }""")
        speech = live.locator('.app-announcement-text').first
        expect(speech).to_have_text('任务开始处理：audio.wav')
        page.evaluate("""() => {
          window.speechChanges = 0
          window.speechObserver = new MutationObserver(() => speechChanges++)
          speechObserver.observe(document.querySelector('[data-app-announcements]'), {childList:true, subtree:true, characterData:true})
          const task = activityFixture.task.tasks.find(t => t.id === 'new')
          task.status = 'separating'
          for(let i=1;i<=80;i++) task.progress=i
          activityFixture.state.progress=53
        }""")
        page.wait_for_timeout(900)
        assert page.evaluate('speechChanges') == 0
        page.evaluate("activityFixture.task.tasks.find(t => t.id === 'new').status = 'done'")
        expect(speech).to_have_text('任务已完成：audio.wav')
        page.evaluate("""async () => {
          const removed = activityFixture.task.tasks[0]
          activityFixture.task.tasks = activityFixture.task.tasks.filter(t => t !== removed)
          await new Promise(resolve=>setTimeout(resolve,0))
          activityFixture.task.tasks.push(removed)
        }""")
        page.wait_for_timeout(900)
        expect(speech).to_have_text('任务已完成：audio.wav')
        checks.append('history hydration and deletion rollback are silent; start/done announce without percentage chatter')

        page.evaluate("""() => {
          activityFixture.task.tasks.push(...Array.from({length:8},(_,i)=>({id:'batch'+i,input:'C:/batch'+i+'.wav',model:'Model A',status:'preparing',progress:0,createdAt:Date.now(),updatedAt:Date.now(),files:[],outputs:[],logs:[]})))
          for(const item of activityFixture.task.tasks.filter(t=>t.id.startsWith('batch'))) item.status='failed'
        }""")
        expect(speech).to_have_text('8 个任务失败')
        checks.append('bursts are coalesced and fast terminal results take precedence over starts')
        page.evaluate("activityFixture.task.tasks.find(t=>t.id === 'new').status='cancelled'")
        expect(speech).to_have_text('任务已取消：audio.wav')

        page.evaluate("""() => {
          activityFixture.model.models = [{name:'Model B',downloaded:false}]
          activityFixture.model.downloadTasks['Model B']={taskId:'download_new',model:'Model B',status:'downloading',progress:0,logs:[],totalFiles:1,completedFiles:0}
        }""")
        expect(speech).to_have_text('模型下载中：Model B')
        page.evaluate("""() => activityFixture.model.handleWorkerEvent({type:'download_done',taskId:'download_new',payload:{model:'Model B',modelInfo:{name:'Model B',downloaded:true},downloaded:['weight'],skipped:[]}})""")
        expect(speech).to_have_text('模型已下载：Model B')
        assert page.evaluate("!activityFixture.model.downloadTasks['Model B']")
        checks.append('download completion is announced even when the successful record is immediately removed')

        model_progress = page.get_by_role('progressbar', name='Model A', exact=True)
        expect(model_progress).to_have_attribute('aria-valuenow', '53')
        expect(model_progress).to_have_attribute('aria-valuetext', '53% · 下载中 · 1 / 4')
        workflow_progress = page.get_by_role('progressbar', name='流程进度', exact=True)
        expect(workflow_progress).to_have_attribute('aria-valuetext', '50% · 已完成 1/2')
        page.evaluate("activityFixture.state.job.tasks[1].status='failed';activityFixture.state.job.progress=100")
        expect(workflow_progress).to_have_attribute('aria-valuetext', '100% · 已完成 1/2 · 失败 1')
        checks.append('model and workflow progress expose names, percentages and file counts')
        for locale, suffix, separator in [('en', 'en', '. '), ('zh-CN', 'zh', '；')]:
            page.evaluate('''({locale, suffix}) => {
              document.querySelector('#app').__vue_app__.config.globalProperties.$i18n.locale = locale
              activityFixture.task.tasks.push({id: 'joined-' + suffix, input: 'C:/joined-' + suffix + '.wav', model: 'Model A',
                status: 'done', progress: 100, files: [], outputs: [], logs: []})
              activityFixture.model.downloadTasks['Joined-' + suffix] = {taskId: 'joined-download-' + suffix,
                model: 'Joined-' + suffix, status: 'paused', progress: 20, logs: []}
            }''', {'locale': locale, 'suffix': suffix})
            expect(speech).to_have_text(re.compile(r'joined-' + suffix + r'\.wav' + re.escape(separator) + r'.*Joined-' + suffix))
        checks.append('coalesced task and download messages retain sentence punctuation in both languages')
        fixture_snapshot = live.aria_snapshot()

        page.add_init_script("localStorage.setItem('pymss-studio:app-settings',JSON.stringify({startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,beginnerGuideProgress:{status:'skipped',step:0},locale:'zh-CN',animationsEnabled:false}))")
        page.goto(args.base_url + '/', wait_until='networkidle')
        page.evaluate("""async () => {
          const [{useSettingsStore},{useBeginnerGuideStore},{useModelStore},{useTaskStore},{createDemoModels}] = await Promise.all([
            import('/src/stores/settings.ts'),import('/src/stores/beginnerGuide.ts'),import('/src/stores/model.ts'),import('/src/stores/task.ts'),import('/src/features/onboarding/demoData.ts')])
          const settings=useSettingsStore(), model=useModelStore(), task=useTaskStore()
          settings.startupOnboardingSeen=true; settings.animationsEnabled=false; settings.locale='zh-CN'
          useBeginnerGuideStore().end('skipped')
          const base=createDemoModels()[0]
          const entries=Array.from({length:15},(_,i)=>({...base,name:'Model '+String(i+1).padStart(2,'0'),downloaded:true}))
          model.models=entries; model.selectedModel=entries[0].name;model.selectedInfo=entries[0];model.modelsLoaded=true
          task.ensembleEnabled=false;task.modelListViewMode='list';task.modelListPageSize=12
          window.modelSelections=[]
          model.selectModel=async item=>{modelSelections.push(item.name);model.selectedModel=item.name;model.selectedInfo=item}
          window.modelKeyboardFixture={model,task,entries}
        }""")
        listing = page.locator('.target-list--models')
        options = listing.get_by_role('option')
        expect(options).to_have_count(12)
        expect(listing.locator('[role=option][tabindex="0"]')).to_have_count(1)
        options.first.focus()
        page.keyboard.press('ArrowDown')
        expect(options.nth(1)).to_be_focused()
        assert page.evaluate('modelSelections.length') == 0
        page.keyboard.press('Enter')
        assert page.evaluate('modelSelections') == ['Model 02']
        page.keyboard.press('End')
        expect(options.last).to_be_focused()
        page.keyboard.press('ArrowRight')
        expect(options.last).to_be_focused()
        expect(options).to_have_count(12)
        page.keyboard.press('Home')
        expect(options.first).to_be_focused()
        page.keyboard.press('ArrowUp')
        expect(options.first).to_be_focused()
        checks.append('model list has one Tab stop; arrows/Home/End move within the page and Enter selects')
        page.keyboard.press('Tab')
        assert not listing.evaluate('el=>el.contains(document.activeElement)')
        page.keyboard.press('Shift+Tab')
        expect(options.first).to_be_focused()
        page.evaluate('modelKeyboardFixture.task.ensembleEnabled = true')
        page.keyboard.press('Space')
        expect(options.first).to_have_attribute('aria-selected', 'true')
        page.keyboard.press('ArrowDown')
        expect(options.nth(1)).to_be_focused()
        page.keyboard.press('Space')
        expect(options.first).to_have_attribute('aria-selected', 'true')
        expect(options.nth(1)).to_have_attribute('aria-selected', 'true')
        page.keyboard.press('Space')
        expect(options.nth(1)).to_have_attribute('aria-selected', 'false')
        checks.append('Tab leaves and returns; multi-selection uses Space without losing earlier choices')
        page.evaluate("modelKeyboardFixture.model.models=modelKeyboardFixture.entries.filter(m=>m.name!=='Model 02')")
        expect(options.nth(1)).to_be_focused()
        expect(options.nth(1)).to_contain_text('Model 03')
        expect(listing.locator('[role=option][tabindex="0"]')).to_have_count(1)
        page.evaluate("modelKeyboardFixture.task.modelListViewMode='card'")
        page.keyboard.press('End')
        expect(options.last).to_be_focused()
        page.evaluate('modelKeyboardFixture.model.models=[]')
        expect(page.locator('[data-model-list-fallback]')).to_be_focused()
        checks.append('background removal repairs focus and card view keeps keyboard navigation')
        assert not errors, errors
        (output / 'results.json').write_text(json.dumps({'checks': checks, 'errors': errors, 'announcementSnapshot': fixture_snapshot}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False, indent=2))
    finally:
        browser.close()
