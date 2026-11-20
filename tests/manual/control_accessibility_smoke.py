"""Check real focus targets and Chromium AX data against a running Vite server."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/control-accessibility')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)


def ax_controls(page):
    session = page.context.new_cdp_session(page)
    try:
        session.send('Accessibility.enable')
        return [
            {'role': node['role']['value'], 'name': node.get('name', {}).get('value'),
             'value': node.get('value', {}).get('value'),
             'properties': {prop['name']: prop['value'].get('value') for prop in node.get('properties', [])}}
            for node in session.send('Accessibility.getFullAXTree')['nodes']
            if not node.get('ignored') and node.get('role', {}).get('value') in
            ('slider', 'switch', 'textbox', 'button', 'dialog')
        ]
    finally:
        session.detach()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        checks = []
        evidence = {}
        page.goto(args.base_url + '/tests/fixtures/control-accessibility.html', wait_until='networkidle')
        volume = page.get_by_role('slider', name='Master', exact=True)
        expect(volume).to_have_attribute('aria-valuetext', '80%')
        volume.focus()
        volume.press('ArrowRight')
        expect(volume).to_have_attribute('aria-valuetext', '81%')
        page.get_by_role('button', name='Master: reset to 100% (0 dB)', exact=True).click()
        expect(volume).to_have_attribute('aria-valuetext', '100%')
        duration = page.get_by_role('slider', name='Chunk duration slider', exact=True)
        expect(duration).to_have_attribute('aria-valuetext', '4 s')
        duration.focus()
        duration.press('ArrowRight')
        expect(duration).to_have_attribute('aria-valuetext', '4.5 s')
        checks.append('slider thumb names, units, keyboard changes and reset')

        field = page.get_by_role('textbox', name='Maximum Concurrent Tasks', exact=True)
        expect(field).to_have_value('1')
        decrease = page.get_by_role('button', name='Decrease Maximum Concurrent Tasks', exact=True)
        increase = page.get_by_role('button', name='Increase Maximum Concurrent Tasks', exact=True)
        expect(decrease).to_be_disabled()
        increase.click()
        expect(field).to_have_value('2')
        field.focus()
        field.press('ArrowUp')
        expect(field).to_have_value('3')
        expect(increase).to_be_disabled()
        field.press('ArrowDown')
        expect(field).to_have_value('2')
        checks.append('number names, step buttons, keyboard stepping and min/max boundaries')

        aligned = page.get_by_role('textbox', name='Chunk Size', exact=True)
        aligned.focus()
        aligned.press('ArrowUp')
        expect(aligned).to_have_value('1024')
        page.get_by_role('button', name='Increase Chunk Size', exact=True).click()
        expect(aligned).to_have_value('1536')
        checks.append('aligned step control keeps keyboard and pointer alignment')
        expect(page.get_by_role('textbox', name='Attack (ms)', exact=True)).to_be_visible()
        evidence['englishWidgets'] = ax_controls(page)
        sliders = [node for node in evidence['englishWidgets'] if node['role'] == 'slider']
        assert len(sliders) == 5 and all(node['name'] and node['properties'].get('valuetext') for node in sliders)
        assert any(node['properties'].get('valuetext') == '3.0 dB' for node in sliders)
        expect(page.locator('.result-track__slider [role="slider"]')).to_have_attribute('aria-valuetext', '00:00 / --:--')
        checks.append('Chromium AX tree exposes slider names and human-readable values')

        page.evaluate('controlFixture.state.disabled = true')
        expect(field).to_be_disabled()
        expect(increase).to_be_disabled()
        expect(volume).to_have_attribute('aria-disabled', 'true')
        page.evaluate("controlFixture.state.disabled = false; controlFixture.locale.value = 'zh-CN'")
        expect(page.get_by_role('textbox', name='最大并发任务数', exact=True)).to_have_value('2')
        expect(page.get_by_role('button', name='增加最大并发任务数', exact=True)).to_be_visible()
        expect(page.get_by_role('slider', name='分块时长滑块', exact=True)).to_have_attribute('aria-valuetext', '4.5 秒')
        page.evaluate('controlFixture.state.buttons = false')
        expect(page.get_by_role('button', name='增加最大并发任务数', exact=True)).to_have_count(0)
        page.evaluate('controlFixture.state.buttons = true')
        expect(page.get_by_role('button', name='增加最大并发任务数', exact=True)).to_be_visible()
        page.evaluate('controlFixture.state.visible = false')
        expect(page.get_by_role('textbox', name='最大并发任务数', exact=True)).to_have_count(0)
        page.evaluate('controlFixture.state.visible = true')
        expect(page.get_by_role('textbox', name='最大并发任务数', exact=True)).to_have_value('2')
        checks.append('disabled controls, locale changes and remounted/conditional buttons retain names')

        page.goto(args.base_url + '/tests/fixtures/app-select.html', wait_until='networkidle')
        page.locator('#open-export').focus()
        page.keyboard.press('Enter')
        modal = page.get_by_role('dialog', name='Export Settings', exact=True)
        expect(modal).to_be_visible()
        protection = modal.get_by_role('switch', name='Peak Protection', exact=True)
        expect(protection).to_be_checked()
        expect(modal.get_by_role('textbox', name='File Name', exact=True)).to_have_value('mix')
        expect(modal.get_by_role('textbox', name='Output Path', exact=True)).to_have_value('C:/Output')
        evidence['export'] = ax_controls(page)
        page.keyboard.press('Escape')
        expect(modal).not_to_be_visible()
        expect(page.locator('#open-export')).to_be_focused()
        checks.append('export dialog name, switch and text input names, Escape and focus return')

        page.add_init_script("localStorage.setItem('pymss-studio:app-settings',JSON.stringify({startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,beginnerGuideProgress:{status:'skipped',step:0},locale:'zh-CN',animationsEnabled:false}))")
        page.goto(args.base_url + '/#/settings', wait_until='networkidle')
        page.evaluate("""async () => {
          const [{useSettingsStore},{useBeginnerGuideStore}] = await Promise.all([
            import('/src/stores/settings.ts'),import('/src/stores/beginnerGuide.ts')])
          const settings = useSettingsStore()
          settings.startupOnboardingSeen = true; settings.locale = 'zh-CN'; settings.animationsEnabled = false
          useBeginnerGuideStore().end('skipped')
        }""")
        nav = page.locator('.settings-nav-item')
        for index in range(nav.count()):
            button = nav.nth(index)
            button.focus()
            button.press('Enter')
            expect(button).to_have_attribute('aria-current', 'true')
            expect(page.locator('.settings-nav-item[aria-current="true"]')).to_have_count(1)
        appearance = nav.filter(has_text='外观')
        appearance.click()
        animations = page.get_by_role('switch', name='动画效果', exact=True)
        expect(animations).not_to_be_checked()
        animations.focus()
        animations.press('Space')
        expect(animations).to_be_checked()
        animations.press('Space')
        expect(animations).not_to_be_checked()
        page.get_by_role('button', name='深色', exact=True).click()
        expect(page.get_by_role('button', name='深色', exact=True)).to_have_attribute('aria-pressed', 'true')
        expect(page.get_by_role('button', name='浅色', exact=True)).to_have_attribute('aria-pressed', 'false')
        page.get_by_role('button', name='浅色', exact=True).click()
        expect(page.get_by_role('button', name='浅色', exact=True)).to_have_attribute('aria-pressed', 'true')
        dots = page.locator('.accent-dot')
        dots.nth(1).click()
        expect(dots.nth(1)).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('.accent-dot[aria-pressed="true"]')).to_have_count(1)
        scale = page.get_by_role('slider', name='缩放因子', exact=True)
        expect(scale).to_have_attribute('aria-valuetext', '100%')
        scale.focus()
        scale.press('ArrowRight')
        expect(scale).to_have_attribute('aria-valuetext', '110%')
        scale.press('ArrowLeft')
        checks.append('actual Settings navigation, switches, theme states and scale percentages')
        evidence['appearance'] = ax_controls(page)
        for index in range(nav.count()):
            nav.nth(index).click()
            if page.get_by_role('textbox', name='最大并发任务数', exact=True).count():
                expect(page.get_by_role('button', name='增加最大并发任务数', exact=True)).to_be_visible()
                expect(page.get_by_role('switch', name='显示完整日志与诊断信息', exact=True)).to_be_visible()
                break
        else:
            raise AssertionError('Execution settings were not found')
        checks.append('actual Settings numeric input and developer switch expose names')

        page.goto(args.base_url + '/', wait_until='networkidle')
        page.evaluate("""async () => {
          const [{useModelStore},{useTaskStore},{createDemoModels},{useAppStore}] = await Promise.all([
            import('/src/stores/model.ts'),import('/src/stores/task.ts'),
            import('/src/features/onboarding/demoData.ts'),import('/src/stores/app.ts')])
          const models = useModelStore(), task = useTaskStore()
          const entries = createDemoModels().slice(0,3).map(model => ({...model, downloaded:true}))
          models.ensureModelMetadata = async () => entries[0]
          models.models = entries; models.selectedModel = entries[0].name
          models.selectedInfo = entries[0]; models.modelsLoaded = true
          task.parameterMode = 'custom'; task.customParameterIntent = {}; task.ttaOverride = false; task.ensembleEnabled = false
          useAppStore().envInfo = {pymssAvailable:true, torchAvailable:true, torchBackend:'cpu'}
        }""")
        expect(page.get_by_role('switch', name='提交后清空', exact=True)).to_be_visible()
        model_list = page.locator('.target-list--models')
        expect(model_list).to_have_attribute('aria-multiselectable', 'false')
        page.evaluate("async () => (await import('/src/stores/task.ts')).useTaskStore().ensembleEnabled = true")
        expect(model_list).to_have_attribute('aria-multiselectable', 'true')
        page.evaluate("""async () => {
          const [{useTaskStore},{useModelStore}] = await Promise.all([
            import('/src/stores/task.ts'),import('/src/stores/model.ts')])
          const task = useTaskStore(), name = useModelStore().models[0].name
          task.ensembleModels = [name]; task.ensembleWeights = {[name]: 0.15}
        }""")
        page.get_by_role('button', name='配置融合参数', exact=True).click()
        ensemble = page.get_by_role('dialog', name='Ensemble 融合参数', exact=True)
        expect(ensemble).to_be_visible()
        weight = ensemble.locator('.ensemble-modal__weight-input input')
        weight.fill('')
        weight.press('Tab')
        expect(ensemble.get_by_role('slider')).to_have_attribute('aria-valuetext', '0')
        page.evaluate("""async () => {
          const task = (await import('/src/stores/task.ts')).useTaskStore()
          task.ensembleWeights[task.ensembleModels[0]] = 0.15000000000000002
        }""")
        expect(ensemble.get_by_role('slider')).to_have_attribute('aria-valuetext', '0.15')
        evidence['ensemble'] = ax_controls(page)
        ensemble.get_by_role('button', name='关闭', exact=True).click()
        checks.append('ensemble dialog name and empty/floating weight value text')
        page.evaluate("async () => (await import('/src/stores/task.ts')).useTaskStore().ensembleEnabled = false")
        expect(model_list).to_have_attribute('aria-multiselectable', 'false')
        checks.append('model list identifies single versus multiple selection')
        page.get_by_role('button', name='高级设置', exact=True).click()
        advanced = page.get_by_role('dialog')
        expect(advanced).to_be_visible()
        expect(advanced).to_have_accessible_name('高级设置')
        evidence['advanced'] = ax_controls(page)
        checks.append('actual separation clear-input switch and advanced dialog name')
        assert not errors, errors
        (output / 'results.json').write_text(json.dumps({'checks': checks, 'errors': errors, 'evidence': evidence}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False, indent=2))
    finally:
        browser.close()
