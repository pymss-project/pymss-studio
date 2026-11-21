"""Run against pnpm dev: python tests/manual/app_select_ui_smoke.py --base-url http://127.0.0.1:1420."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/app-select')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1280, 'height': 960})
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.base_url + '/tests/fixtures/app-select.html', wait_until='networkidle')
        results = []

        def value(key):
            return page.evaluate('key => selectFixture.state[key]', key)

        def open_control(key, button='Enter'):
            control = page.locator('#' + key)
            control.focus()
            control.press(button)
            expect(control).to_have_attribute('aria-expanded', 'true')
            expect(page.get_by_role('listbox')).to_be_visible()
            if control.evaluate('el => el.tagName') == 'BUTTON' and not value('loading'):
                page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
            return control

        control = open_control('simple', 'Space')
        page.keyboard.press('End')
        expect(page.get_by_role('option', name='Gamma', exact=True)).to_be_focused()
        page.keyboard.press('NumpadEnter')
        expect(control).to_have_attribute('aria-expanded', 'false')
        assert value('simple') == 'c'
        expect(control).to_be_focused()
        open_control('simple')
        page.keyboard.press('Home')
        expect(page.get_by_role('option', name='Alpha', exact=True)).to_be_focused()
        page.keyboard.press('Escape')
        assert value('simple') == 'c'
        results.append('plain keyboard selection, numpad enter, escape and focus return')

        open_control('simple')
        page.keyboard.press('Home')
        expect(page.get_by_role('option', name='Alpha', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Beta', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Gamma', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        assert value('simple') == 'c'
        results.append('disabled option skipped by keyboard')
        page.get_by_role('button', name='Clear selection: Plain selection', exact=True).click()
        assert value('simple') is None
        results.append('clear emits null')
        open_control('simple')
        page.keyboard.press('Tab')
        expect(page.locator('#simple')).to_have_attribute('aria-expanded', 'false')
        expect(page.locator('#search')).to_be_focused()
        open_control('search')
        page.keyboard.press('Tab')
        expect(page.locator('#search')).to_have_attribute('aria-expanded', 'false')
        results.append('Tab leaves both select and search menus without a focus trap')

        control = page.locator('#search')
        control.fill('Bet')
        expect(page.get_by_role('option', name='Beta', exact=True)).to_be_visible()
        expect(page.get_by_role('option', name='Alpha', exact=True)).to_have_count(0)
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        assert value('search') == 'b'
        expect(control).to_have_value('Beta')
        expect(control).to_have_attribute('aria-expanded', 'false')
        page.evaluate("selectFixture.state.options = selectFixture.state.options.map(o => o.value === 'b' ? {...o, label:'Beta updated'} : o)")
        expect(control).to_have_value('Beta updated')
        results.append('search filtering, selection, dynamic option labels')
        control.focus()
        control.press('g')
        expect(control).to_have_value('g')
        expect(page.get_by_role('option', name='Gamma', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        results.append('typing replaces an existing selected label')
        control.fill('No match')
        expect(page.get_by_role('status')).to_have_text('No options available')
        control.dispatch_event('compositionstart', {'data': ''})
        control.dispatch_event('keydown', {'key': 'Enter', 'code': 'Enter', 'isComposing': True})
        expect(control).to_have_attribute('aria-expanded', 'true')
        control.dispatch_event('compositionend', {'data': '中'})
        page.keyboard.press('Escape')
        results.append('empty search and IME Enter do not commit an option')

        control = open_control('multiple')
        page.get_by_role('option', name='Alpha', exact=True).click()
        page.get_by_role('option', name='Gamma', exact=True).click()
        assert value('multiple') == ['a', 'c']
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Remove: Alpha', exact=True).click()
        assert value('multiple') == ['c']
        page.get_by_role('button', name='Clear selection: Multiple selection', exact=True).click()
        assert value('multiple') == []
        results.append('multiple selection, individual removal and clear')
        page.evaluate("selectFixture.state.multiple = ['saved-unknown', 'a']")
        page.locator('label').filter(has=page.locator('#multiple')).click(position={'x': 5, 'y': 5})
        assert value('multiple') == ['saved-unknown', 'a']
        page.keyboard.press('Escape')
        page.locator('.app-select__tag > span').filter(has_text='Alpha').click()
        assert value('multiple') == ['saved-unknown', 'a']
        expect(page.locator('#multiple')).to_have_attribute('aria-expanded', 'true')
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Remove: Alpha', exact=True).click()
        assert value('multiple') == ['saved-unknown']
        control = page.locator('#multiple')
        control.fill('Gam')
        page.get_by_role('option', name='Gamma', exact=True).click()
        assert value('multiple') == ['saved-unknown', 'c']
        expect(control).to_have_value('')
        expect(page.get_by_role('option', name='Alpha', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        results.append('unknown multi-values preserved, tag text safe, query reset after selection')
        control = open_control('multiReadOnly', 'Space')
        page.keyboard.press('ArrowDown')
        page.wait_for_function("document.getElementById('multiReadOnly').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('multiReadOnly').getAttribute('aria-activedescendant'))")
        page.keyboard.press('Space')
        assert len(value('multiReadOnly')) == 1
        assert value('controlledOpen')
        page.keyboard.press('Escape')
        assert not value('controlledOpen')
        results.append('non-search multi selection with Space and controlled popup state')

        for label, expected in [('String zero', '0'), ('Empty string', ''), ('Numeric zero', 0)]:
            open_control('typed')
            expect(page.get_by_role('group', name='Values', exact=True)).to_be_visible()
            expect(page.get_by_role('option', name='Blocked', exact=True)).to_be_disabled()
            page.get_by_role('option', name=label, exact=True).click()
            assert value('typed') == expected and type(value('typed')) is type(expected)
        results.append('group names, disabled groups and string/number/empty identity')

        control = open_control('large')
        page.keyboard.press('End')
        page.wait_for_function("document.getElementById('large').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('large').getAttribute('aria-activedescendant'))?.textContent.includes('1199')")
        assert page.get_by_role('option').count() < 100
        page.keyboard.press('Enter')
        assert value('large') == 1199
        open_control('large')
        page.keyboard.press('Home')
        page.wait_for_function("document.getElementById('large').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('large').getAttribute('aria-activedescendant'))?.textContent.includes('0000')")
        page.keyboard.press('Enter')
        assert value('large') == 0
        results.append('1200-option virtual list, Home/End and valid active descendant')
        control = open_control('largeSearch')
        control.fill('1199')
        expect(page.get_by_role('option', name='Item 1199', exact=True)).to_be_visible()
        page.keyboard.press('ArrowDown')
        page.wait_for_function("document.getElementById('largeSearch').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('largeSearch').getAttribute('aria-activedescendant'))?.textContent.includes('1199')")
        page.keyboard.press('Enter')
        assert value('largeSearch') == 1199
        results.append('virtual list filtering to a single match retains keyboard navigation')

        page.evaluate('selectFixture.state.disabled = true')
        expect(page.locator('#simple')).to_be_disabled()
        page.evaluate('selectFixture.state.disabled = false; selectFixture.state.loading = true')
        open_control('simple')
        expect(page.locator('#simple')).to_have_attribute('aria-busy', 'true')
        expect(page.get_by_role('option', name='Alpha', exact=True)).to_be_visible()
        page.get_by_role('option', name='Alpha', exact=True).click()
        assert value('simple') == 'a'
        loading_options = value('options')
        page.evaluate('selectFixture.state.options = []')
        open_control('simple')
        expect(page.get_by_role('option')).to_have_count(0)
        expect(page.get_by_role('status')).to_have_text('Loading…')
        page.keyboard.press('Escape')
        page.evaluate('options => selectFixture.state.options = options', loading_options)
        page.evaluate('selectFixture.state.loading = false')
        results.append('disabled blocks changes while loading preserves existing choices and reports empty loading state')

        open_control('simple')
        page.evaluate('selectFixture.blur()')
        expect(page.locator('#simple')).to_have_attribute('aria-expanded', 'false')
        expect(page.locator('#simple')).not_to_be_focused()
        page.evaluate('selectFixture.focus()')
        expect(page.locator('#simple')).to_be_focused()
        results.append('workflow-compatible exposed focus/blur')
        for key in ['reject', 'rejectSearch']:
            control = open_control(key)
            page.get_by_role('option', name='Alpha', exact=True).click()
            assert value(key) is None
            open_control(key)
            expect(page.get_by_role('option', selected=True)).to_have_count(0)
            page.keyboard.press('Escape')
        expect(page.locator('#reject')).to_have_text('Select an option')
        results.append('initial null values stay controlled when parent rejects updates')
        original_options = value('options')
        open_control('simple')
        page.evaluate("selectFixture.state.options = Array.from({length:150}, (_, value) => ({label:'Async '+value, value}))")
        expect(page.locator('#simple')).to_have_js_property('tagName', 'BUTTON')
        page.keyboard.press('Escape')
        expect(page.locator('#simple')).to_be_focused()
        page.keyboard.press('Tab')
        if value('simple') is not None:
            expect(page.get_by_role('button', name='Clear selection: Plain selection', exact=True)).to_be_focused()
            page.keyboard.press('Tab')
        expect(page.locator('#search')).to_be_focused()
        page.evaluate('options => selectFixture.state.options = options', original_options)
        results.append('async option growth preserves focused control and tab order')
        page.locator('#open-search-modal').click()
        search_modal = page.get_by_role('dialog', name='Search modal', exact=True)
        expect(search_modal).to_be_visible()
        modal_search = search_modal.get_by_role('combobox', name='Modal search', exact=True)
        modal_search.click()
        expect(search_modal.get_by_role('listbox')).to_be_visible()
        page.keyboard.press('Escape')
        expect(search_modal).to_be_visible()
        expect(modal_search).to_be_focused()
        page.keyboard.press('Escape')
        expect(search_modal).not_to_be_visible()
        results.append('searchable combobox inside Naive modal preserves Escape and focus boundary')

        page.locator('#open-export').click()
        modal = page.get_by_role('dialog')
        expect(modal).to_be_visible()
        depth = modal.get_by_role('combobox', name='WAV bit depth', exact=True)
        depth_id = depth.get_attribute('id')
        depth.focus()
        page.keyboard.press('Space')
        expect(modal.get_by_role('listbox', name='WAV bit depth', exact=True)).to_be_visible()
        page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
        page.keyboard.press('End')
        expect(page.get_by_role('option', name='FLOAT', exact=True)).to_be_focused()
        page.keyboard.press('NumpadEnter')
        assert value('wav') == 'FLOAT'
        depth = page.locator('[id="' + depth_id + '"]')
        depth.press('Enter')
        page.keyboard.press('Escape')
        expect(modal).to_be_visible()
        expect(depth).to_be_focused()
        results.append('actual editor export dialog, popup portal and Escape boundary')
        rate = modal.get_by_role('combobox', name='Sample Rate', exact=True)
        rate.click()
        page.get_by_role('option', name='48000 Hz', exact=True).click()
        assert value('rate') == 48000 and type(value('rate')) is int
        page.evaluate("selectFixture.state.format = 'flac'")
        flac = modal.get_by_role('combobox', name='FLAC bit depth', exact=True)
        flac.click()
        page.get_by_role('option', name='PCM_24', exact=True).click()
        assert value('flac') == 'PCM_24'
        page.evaluate("selectFixture.state.format = 'wav'")
        depth = modal.get_by_role('combobox', name='WAV bit depth', exact=True)
        expect(depth).to_have_text('FLOAT')
        results.append('actual export sample rate remains numeric and format-specific bit depths persist')

        for theme in ['light', 'dark']:
            page.evaluate('theme => selectFixture.theme(theme)', theme)
            depth.press('Enter')
            expect(modal.get_by_role('listbox')).to_be_visible()
            modal.screenshot(path=str(output / (theme + '-export.png')), animations='disabled')
            page.keyboard.press('Escape')
        page.emulate_media(forced_colors='active')
        depth.focus()
        assert depth.evaluate("el => { const s = getComputedStyle(el); return s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1 && !['transparent', 'rgba(0, 0, 0, 0)'].includes(s.outlineColor) }")
        results.append('light/dark screenshots and forced-colors focus')
        page.emulate_media(forced_colors='none')
        assert page.evaluate('selectFixture.shortcuts.togglePlay || 0') == 0
        assert page.evaluate('selectFixture.shortcuts.toHome || 0') == 0
        page.evaluate("window.dispatchEvent(new KeyboardEvent('keydown', {key:' ', bubbles:true}))")
        assert page.evaluate('selectFixture.shortcuts.togglePlay') == 1
        results.append('real editor shortcuts ignore dropdown navigation and still work outside controls')
        page.evaluate("window.dispatchEvent(new KeyboardEvent('keydown', {key:'s', bubbles:true}))")
        assert page.evaluate('selectFixture.shortcuts.toggleSnapping || 0') == 0
        page.evaluate('selectFixture.state.exportOpen = false')
        expect(modal).to_be_hidden()
        page.locator('#search').press('s')
        assert page.evaluate('selectFixture.shortcuts.toggleSnapping || 0') == 0
        page.locator('#open-export').focus()
        page.keyboard.press('s')
        assert page.evaluate('selectFixture.shortcuts.toggleSnapping') == 1
        results.append('timeline shortcuts work outside the timeline and respect search fields and export modals')
        page.goto(args.base_url + '/', wait_until='networkidle')
        page.evaluate("""async () => {
          const [{useSettingsStore}, {useBeginnerGuideStore}, {useModelStore}, {useTaskStore}, {createDemoModels}, {useAppStore}] = await Promise.all([
            import('/src/stores/settings.ts'), import('/src/stores/beginnerGuide.ts'), import('/src/stores/model.ts'), import('/src/stores/task.ts'), import('/src/features/onboarding/demoData.ts'), import('/src/stores/app.ts')
          ])
          const settings = useSettingsStore()
          settings.startupOnboardingSeen = true; settings.themeMode = 'light'; settings.locale = 'zh-CN'; settings.defaultFormat = 'flac'; settings.flacBitDepth = 'PCM_16'; settings.wavBitDepth = 'PCM_16'
          useBeginnerGuideStore().end('skipped')
          const model = useModelStore(), task = useTaskStore(), entry = {...createDemoModels()[0], downloaded: true}
          model.ensureModelMetadata = async () => entry
          model.models = [entry]; model.selectedModel = entry.name; model.selectedInfo = entry; model.modelsLoaded = true
          task.parameterMode = 'custom'; task.customParameterIntent = {}; task.ttaOverride = false
          useAppStore().envInfo = {pymssAvailable: true, torchAvailable: true, torchBackend: 'cpu'}
          window.selectSettingsFixture = settings
        }""")
        for fmt, key, field, expected in [('flac', 'flacBitDepth', 'FLAC 位深', 'PCM_24'), ('wav', 'wavBitDepth', 'WAV 位深', 'FLOAT'), ('mp3', 'mp3BitRate', 'MP3 比特率', '320k')]:
            page.evaluate('fmt => selectSettingsFixture.defaultFormat = fmt', fmt)
            page.get_by_role('button', name='高级设置', exact=True).click()
            selector = page.get_by_role('combobox', name=field, exact=True)
            expect(selector).to_be_visible()
            selector.focus()
            page.keyboard.press('Enter')
            page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
            page.keyboard.press('End')
            expect(page.get_by_role('option').last).to_be_focused()
            page.keyboard.press('NumpadEnter')
            page.get_by_role('button', name='应用', exact=True).click()
            assert page.evaluate('key => selectSettingsFixture[key]', key) == expected
            page.get_by_role('button', name='高级设置', exact=True).click()
            selector = page.get_by_role('combobox', name=field, exact=True)
            if fmt != 'mp3':
                assert expected == page.evaluate('key => selectSettingsFixture[key]', key)
                selector.focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
                page.keyboard.press('Home')
                expect(page.get_by_role('option').first).to_be_focused()
                page.keyboard.press('Enter')
            page.get_by_role('button', name='取消', exact=True).click()
            assert page.evaluate('key => selectSettingsFixture[key]', key) == expected
        results.append('actual advanced settings, FLAC/WAV/MP3 keyboard, apply/reopen/cancel')
        assert not errors, errors
        (output / 'results.json').write_text(json.dumps({'checks': results, 'pageErrors': errors}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(results), 'checks': results}, indent=2))
    finally:
        browser.close()
