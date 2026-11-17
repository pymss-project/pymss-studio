"""Check workflow menu sizing and model action positioning against pnpm dev."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/menu-layout')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1280, 'height': 860})
        page.set_default_timeout(8000)
        errors = []
        checks = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.base_url + '/tests/fixtures/workflow-accessibility.html', wait_until='networkidle')
        names = ['bs_roformer_karaoke_anvuew.ckpt', 'mel_band_roformer_karaoke_becruily.ckpt']
        page.evaluate('''names => {
          const {state, models} = workflowA11y
          models.forEach((model, index) => model.name = state.draft.steps[index].model = names[index])
        }''', names)

        def center_node(node_id, zoom, x=500):
            page.evaluate('''({nodeId, zoom, x}) => {
              const ui = workflowA11y.state.draft.ui, node = ui.nodes[nodeId]
              ui.viewport = {x: x - node.x * zoom, y: 80 - node.y * zoom, zoom}
            }''', {'nodeId': node_id, 'zoom': zoom, 'x': x})

        def check_menu(control):
            control.focus()
            control.press('ArrowDown')
            menu = page.get_by_role('listbox')
            expect(menu).to_be_visible()
            box = menu.bounding_box()
            width = page.viewport_size['width']
            assert min(320, width - 24) - 1 <= box['width'] <= min(560, width - 24) + 1, box
            assert box['x'] >= -1 and box['x'] + box['width'] <= width + 1, box
            assert box['y'] >= -1 and box['y'] + box['height'] <= page.viewport_size['height'] + 1, box
            for option in page.get_by_role('option').all():
                text = option.locator('span').first
                assert text.get_attribute('title').startswith(text.text_content())
                assert option.bounding_box()['height'] <= 60, option.text_content()
            return menu

        source = page.locator('.simple-ensemble-source-select [role="combobox"]').first
        for zoom in [0.45, 0.8, 1, 1.8]:
            center_node('ensemble1', zoom)
            check_menu(source)
            if zoom == 0.8:
                page.screenshot(path=str(output / 'workflow-source.png'), animations='disabled')
            page.keyboard.press('Escape')
        checks.append('source menu stays readable at all supported canvas zoom levels')

        palettes = {}
        center_node('ensemble1', 0.8)
        for mode in ['light', 'dark']:
            for accent in ['blue', 'pink', 'sky', 'teal']:
                page.evaluate('''async ({mode, accent}) => {
                  const {applyTheme} = await import('/src/utils/theme.ts')
                  applyTheme(mode, accent)
                }''', {'mode': mode, 'accent': accent})
                menu = check_menu(source)
                palettes[(mode, accent)] = menu.evaluate('''el => {
                  const option = el.querySelector('[data-state="checked"]')
                  return {background: getComputedStyle(el).backgroundColor, text: getComputedStyle(option).color}
                }''')
                if accent == 'teal':
                    page.screenshot(path=str(output / ('workflow-' + mode + '-teal.png')), animations='disabled')
                page.keyboard.press('Escape')
        for mode in ['light', 'dark']:
            assert len({palettes[(mode, accent)]['text'] for accent in ['blue', 'pink', 'sky', 'teal']}) == 4, palettes
        assert palettes[('light', 'teal')]['background'] != palettes[('dark', 'teal')]['background'], palettes
        checks.append('light/dark menus and selected colors follow all four theme accents')
        page.evaluate("async () => (await import('/src/utils/theme.ts')).applyTheme('light', 'blue')")

        # Exceptionally long ASCII and Chinese names remain two lines visually, with full text in the DOM/title.
        long_name = 'mel_band_roformer_' + 'karaoke_becruily_' * 8 + '主唱与和声.ckpt'
        page.evaluate('''name => {
          workflowA11y.models[0].name = name
          workflowA11y.state.draft.steps[0].model = name
        }''', long_name)
        center_node('ensemble1', 0.8, 1080)
        check_menu(source)
        page.screenshot(path=str(output / 'workflow-long-source.png'), animations='disabled')
        page.keyboard.press('Escape')
        checks.append('very long source names are bounded and the menu shifts inside the right edge')

        center_node('step1', 1)
        model = page.locator('[data-simple-node="step1"]').get_by_role('combobox', name='模型', exact=True)
        check_menu(model)
        page.keyboard.press('Escape')
        center_node('step2', 1)
        step_input = page.locator('[data-simple-node="step2"]').get_by_role('combobox', name='输入来源', exact=True)
        check_menu(step_input)
        page.keyboard.press('Escape')
        checks.append('filterable model and separation input menus share the readable sizing')

        center_node('ensemble1', 1)
        check_menu(source)
        # The wheel gesture happens on the canvas, outside the portalled menu.
        canvas = page.locator('.simple-node-editor__canvas')
        canvas.dispatch_event('wheel', {'clientX': 150, 'clientY': 400, 'deltaY': 100})
        expect(page.get_by_role('listbox')).to_have_count(0)
        checks.append('canvas zoom closes the source popup instead of leaving it detached')

        page.set_viewport_size({'width': 640, 'height': 700})
        center_node('ensemble1', 0.8, 180)
        check_menu(source)
        page.keyboard.press('Escape')
        checks.append('source menu remains inside a narrow window')

        page.set_viewport_size({'width': 1280, 'height': 860})
        page.goto(args.base_url + '/', wait_until='networkidle')
        page.evaluate('''async () => {
          const [{useSettingsStore}, {useBeginnerGuideStore}, {useModelStore}, {createDemoModels}] = await Promise.all([
            import('/src/stores/settings.ts'), import('/src/stores/beginnerGuide.ts'),
            import('/src/stores/model.ts'), import('/src/features/onboarding/demoData.ts')
          ])
          Object.assign(useSettingsStore(), {startupOnboardingSeen: true, beginnerGuideInvitationSeen: true,
            locale: 'zh-CN', animationsEnabled: false, themeMode: 'light'})
          useBeginnerGuideStore().end('skipped')
          const store = useModelStore(), demos = createDemoModels()
          store.models = Array.from({length: 18}, (_, index) => ({...demos[index % demos.length], name: demos[index % demos.length].name + '-' + index, downloaded: true}))
          store.modelsLoaded = true; store.modelViewMode = 'list'; store.isLoading = false
          location.hash = '/models'
        }''')
        button = page.locator('.mc-more').first
        menu = page.locator('.n-dropdown-menu')

        def check_model_menu(key=None):
            box = button.bounding_box()
            if key:
                button.focus()
                button.press(key)
            else:
                button.click()
            expect(menu).to_be_visible()
            popup = menu.bounding_box()
            assert abs(popup['x'] + popup['width'] - box['x'] - box['width']) <= 2, (box, popup)
            below = box['y'] + box['height'] <= popup['y'] <= box['y'] + box['height'] + 16
            flipped_above = abs(popup['y'] + popup['height'] - box['y'] - box['height']) <= 8
            assert below or flipped_above, (box, popup)
            expect(button).to_have_attribute('aria-expanded', 'true')

        for key in ['Enter', 'Space', None]:
            check_model_menu(key)
            if key == 'Enter':
                page.screenshot(path=str(output / 'model-actions.png'), animations='disabled')
            page.keyboard.press('Escape')
            expect(menu).not_to_be_visible()
            expect(button).to_have_attribute('aria-expanded', 'false')
        checks.append('Enter, Space and pointer activation all anchor the actions to the button; Escape closes')

        check_model_menu()
        button.click()
        expect(menu).not_to_be_visible()
        check_model_menu('Enter')
        button.press('Space')
        expect(menu).not_to_be_visible()
        checks.append('repeated pointer and Space activation close the same model menu')

        check_model_menu('Enter')
        page.locator('.app-content').evaluate('el => el.scrollTop = 200')
        expect(menu).not_to_be_visible()
        checks.append('scrolling the model page dismisses the popup')

        button = page.locator('.mc-more').nth(4)
        button.scroll_into_view_if_needed()
        check_model_menu('Enter')
        page.set_viewport_size({'width': 1000, 'height': 720})
        expect(menu).not_to_be_visible()
        checks.append('window resizing dismisses the popup')

        row = page.locator('.model-card').nth(5)
        row.scroll_into_view_if_needed()
        row.click(button='right', position={'x': 80, 'y': 30})
        expect(menu).to_be_visible()
        box, popup = row.bounding_box(), menu.bounding_box()
        assert abs(popup['x'] - box['x'] - 80) <= 2, (box, popup)
        page.keyboard.press('Escape')
        checks.append('right-click keeps the pointer position')

        button = page.locator('.mc-more').nth(4)
        button.scroll_into_view_if_needed()
        check_model_menu('Enter')
        page.evaluate("location.hash = '/settings'")
        expect(menu).not_to_be_visible()
        page.evaluate("location.hash = '/models'")
        expect(page.locator('.mc-more').first).to_be_visible()
        expect(menu).not_to_be_visible()
        checks.append('leaving and returning to the model page does not revive its menu')

        button = page.locator('.mc-more').nth(4)
        button.scroll_into_view_if_needed()
        check_model_menu()
        page.locator('.n-dropdown-option-body__label', has_text='编辑备注').click()
        expect(page.get_by_role('dialog', name='编辑备注', exact=True)).to_be_visible()
        expect(menu).not_to_be_visible()
        checks.append('action selection still opens the model note editor')

        expect(page.locator('vite-error-overlay')).to_have_count(0)
        assert not errors, errors
        (output / 'results.json').write_text(json.dumps({'checks': checks, 'pageErrors': errors}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(checks), 'checks': checks}, indent=2))
    finally:
        browser.close()
