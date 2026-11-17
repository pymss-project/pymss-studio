"""Verify workflow keyboard alternatives against a running Vite server."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/workflow-accessibility')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1680, 'height': 1050})
        page.set_default_timeout(8000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        checks = []
        page.goto(args.base_url + '/tests/fixtures/workflow-accessibility.html', wait_until='networkidle')
        assert page.locator('.simple-node-editor__canvas').bounding_box()['height'] > 300
        page.get_by_role('button', name='适应视图', exact=True).focus()
        page.keyboard.press('Enter')
        def idle_port_reads():
            return page.evaluate('''async () => {
              await new Promise(resolve => setTimeout(resolve, 250))
              const original = Element.prototype.getBoundingClientRect
              let count = 0
              Element.prototype.getBoundingClientRect = function (...args) {
                if (this.matches('.simple-node-editor__world, .simple-port i, .simple-ensemble-output-port i, .simple-save-row__port')) count++
                return original.apply(this, args)
              }
              try {
                await new Promise(resolve => setTimeout(resolve, 250))
                return count
              } finally { Element.prototype.getBoundingClientRect = original }
            }''')

        assert idle_port_reads() == 0, 'A stationary canvas must not continuously measure its ports'
        checks.append('stationary canvas has no repeated port measurement loop')
        saved_vocals = page.get_by_role('button', name='分离步骤 1 · Model 1 · vocals · 已连接保存', exact=True)
        expect(saved_vocals).to_have_attribute('aria-pressed', 'true')
        saved_vocals.focus()
        saved_vocals.press('Enter')
        unsaved_vocals = page.get_by_role('button', name='分离步骤 1 · Model 1 · vocals · 保存音轨', exact=True)
        expect(unsaved_vocals).to_have_attribute('aria-pressed', 'false')
        unsaved_vocals.press('Space')
        expect(saved_vocals).to_have_attribute('aria-pressed', 'true')
        saved_ensemble = page.get_by_role('button', name='Ensemble 融合 1 · Ensemble · 已连接保存', exact=True)
        expect(saved_ensemble).to_have_attribute('aria-pressed', 'true')
        saved_ensemble.focus()
        saved_ensemble.press('Enter')
        unsaved_ensemble = page.get_by_role('button', name='Ensemble 融合 1 · Ensemble · 保存音轨', exact=True)
        expect(unsaved_ensemble).to_have_attribute('aria-pressed', 'false')
        unsaved_ensemble.press('Space')
        expect(saved_ensemble).to_have_attribute('aria-pressed', 'true')
        checks.append('save buttons identify each node and stem, and expose the toggled state to keyboard users')
        page.evaluate('''() => {
          const draft = workflowA11y.state.draft
          draft.ensembles.push({...JSON.parse(JSON.stringify(draft.ensembles[0])), id: 'ensemble2', outputStem: 'Second', save: ''})
          draft.ui.nodes.ensemble2 = {x: 1100, y: 620}
        }''')
        for node in [1, 2]:
            weight = page.get_by_label(f'Ensemble 融合 {node} · 输入 1 · Model 1 · vocals · 权重', exact=True)
            expect(weight).to_have_count(1)
            expect(weight).to_be_visible()
        page.evaluate('''() => {
          workflowA11y.state.draft.ensembles.pop()
          delete workflowA11y.state.draft.ui.nodes.ensemble2
        }''')
        checks.append('weights remain distinguishable when multiple ensemble nodes share a source')
        source = page.get_by_role('combobox', name='Ensemble 融合 1 · 输入 1来源', exact=True)
        source.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('option', name='Model 2 · vocals', exact=True)).to_have_count(0)
        expect(page.get_by_role('option', name='Model 1 · vocals', exact=True)).to_be_enabled()
        assert idle_port_reads() == 0, 'Opening a menu must not restart the canvas layout loop'
        checks.append('unavailable sources are hidden, the current source remains selectable and the open menu stays idle')
        page.keyboard.press('Home')
        expect(page.get_by_role('option', name='原始输入', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Model 1 · vocals', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Model 1 · instrumental', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == 'step1.instrumental'
        expect(source).to_be_focused()
        page.get_by_role('button', name='撤销', exact=True).focus()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == 'step1.vocals'
        page.get_by_role('button', name='重做', exact=True).focus()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == 'step1.instrumental'
        checks.append('ensemble source selection supports keyboard, rejects duplicate sources and preserves undo/redo')
        second_source = page.get_by_role('combobox', name='Ensemble 融合 1 · 输入 2来源', exact=True)
        second_source.focus()
        second_source.press('Enter')
        expect(page.get_by_role('option', name='Model 1 · instrumental', exact=True)).to_have_count(0)
        expect(page.get_by_role('option', name='Model 1 · vocals', exact=True)).to_be_enabled()
        page.keyboard.press('Escape')
        checks.append('source availability updates after another input changes')

        original_sources = page.evaluate('''() => ({stepInput: workflowA11y.state.draft.steps[1].input,
          secondSource: workflowA11y.state.draft.ensembles[0].inputs[1].source})''')
        page.evaluate('''() => {
          const draft = workflowA11y.state.draft, ensemble = draft.ensembles[0]
          ensemble.inputs[1].source = 'step1.vocals'
          draft.steps[1].input = ensemble.id + '.' + ensemble.outputStem
        }''')
        source.focus()
        source.press('Enter')
        expect(page.get_by_role('option', name='Model 2 · vocals', exact=True)).to_have_count(0)
        expect(page.get_by_role('option', name='Model 2 · instrumental', exact=True)).to_have_count(0)
        expect(page.get_by_role('option', name='Model 1 · instrumental', exact=True)).to_be_enabled()
        page.keyboard.press('Escape')
        page.evaluate('''original => {
          workflowA11y.state.draft.steps[1].input = original.stepInput
          workflowA11y.state.draft.ensembles[0].inputs[1].source = original.secondSource
        }''', original_sources)
        source.focus()
        source.press('Enter')
        expect(page.get_by_role('option', name='Model 2 · instrumental', exact=True)).to_be_enabled()
        page.keyboard.press('Escape')
        checks.append('cyclic sources are hidden and return when the conflicting dependency is removed')

        def expect_wire_attached(source_selector, target_selector):
            page.wait_for_function('''({sourceSelector, targetSelector}) => {
              const center = selector => {
                const port = document.querySelector(selector)
                const rect = (port.querySelector('i') || port).getBoundingClientRect()
                return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}
              }
              const source = center(sourceSelector), target = center(targetSelector)
              return [...document.querySelectorAll('.simple-node-editor__edge')].some(path => {
                const matrix = path.getScreenCTM()
                const start = path.getPointAtLength(0).matrixTransform(matrix)
                const end = path.getPointAtLength(path.getTotalLength()).matrixTransform(matrix)
                return Math.hypot(start.x - source.x, start.y - source.y) < 2 && Math.hypot(end.x - target.x, end.y - target.y) < 2
              })
            }''', arg={'sourceSelector': source_selector, 'targetSelector': target_selector})

        header = page.locator('[data-simple-node="step1"] > header strong')
        box = header.bounding_box()
        page.mouse.move(box['x'] + 10, box['y'] + 5)
        page.mouse.down()
        page.mouse.move(box['x'] + 70, box['y'] + 35, steps=6)
        page.mouse.up()
        expect_wire_attached('[data-simple-source="input"]', '[data-simple-target="step:step1"]')
        page.get_by_role('button', name='撤销', exact=True).click()
        expect_wire_attached('[data-simple-source="input"]', '[data-simple-target="step:step1"]')
        page.get_by_role('button', name='重做', exact=True).click()
        expect_wire_attached('[data-simple-source="input"]', '[data-simple-target="step:step1"]')
        for _ in range(2):
            page.evaluate('workflowA11y.state.draft.steps[0].stems.reverse()')
            expect_wire_attached('[data-simple-source="step1.instrumental"]', '[data-simple-target="ensemble:ensemble1:0"]')
        old_zoom = page.evaluate('workflowA11y.state.draft.ui.viewport.zoom')
        page.evaluate('workflowA11y.state.draft.ui.viewport.zoom *= 0.9')
        expect_wire_attached('[data-simple-source="step1.instrumental"]', '[data-simple-target="ensemble:ensemble1:0"]')
        page.evaluate('zoom => workflowA11y.state.draft.ui.viewport.zoom = zoom', old_zoom)
        expect_wire_attached('[data-simple-source="step1.instrumental"]', '[data-simple-target="ensemble:ensemble1:0"]')
        assert idle_port_reads() == 0
        checks.append('wires remain attached after node drag, undo/redo, stem reorder and zoom; the canvas settles again')
        step_input = page.locator('[data-simple-node="step1"]').get_by_role('combobox', name='输入来源', exact=True)
        step_input.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('option', name='Ensemble 融合 · Ensemble', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
        checks.append('source options exclude a connection that would introduce a cycle')
        page.get_by_role('button', name='清空选择: Ensemble 融合 1 · 输入 1来源', exact=True).focus()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == ''
        source.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('listbox')).to_be_visible()
        page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
        page.keyboard.press('Home')
        expect(page.get_by_role('option', name='原始输入', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Model 1 · vocals', exact=True)).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(page.get_by_role('option', name='Model 1 · instrumental', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == 'step1.instrumental'
        page.screenshot(path=str(output / 'source-selection.png'), full_page=True)
        output_port = page.locator('[data-simple-source="step1.vocals"]')
        input_port = page.locator('[data-simple-target="ensemble:ensemble1:0"]')
        source_box = output_port.bounding_box()
        target_box = input_port.bounding_box()
        page.mouse.move(source_box['x'] + source_box['width'] / 2, source_box['y'] + source_box['height'] / 2)
        page.mouse.down()
        page.mouse.move(target_box['x'] + target_box['width'] / 2, target_box['y'] + target_box['height'] / 2, steps=8)
        page.mouse.up()
        assert page.evaluate('workflowA11y.state.draft.ensembles[0].inputs[0].source') == 'step1.vocals'
        assert page.evaluate("document.activeElement.closest('[aria-hidden=true]') === null")
        checks.append('pointer cable editing still works and pointer-only ports do not capture keyboard focus')

        expect(page.get_by_role('button', name='连接管理', exact=True)).to_have_count(0)
        edge = page.locator('.simple-node-editor__edge-hit').first
        visible_point = edge.evaluate("""element => {
          const length = element.getTotalLength()
          for (const fraction of [0.5, 0.6, 0.7, 0.8, 0.9, 0.4, 0.3, 0.2, 0.1]) {
            const point = element.getPointAtLength(length * fraction)
            const screen = new DOMPoint(point.x, point.y).matrixTransform(element.getScreenCTM())
            const hit = document.elementFromPoint(screen.x, screen.y)
            if (hit === element || (hit?.matches('path') && hit.parentElement === element.parentElement)) return {x: screen.x, y: screen.y}
          }
          return null
        }""")
        assert visible_point, 'The edge must have a visible clickable segment'
        page.mouse.click(visible_point['x'], visible_point['y'])
        assert page.evaluate('workflowA11y.state.draft.steps[0].input') == ''
        page.get_by_role('button', name='撤销', exact=True).focus()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.steps[0].input') == 'input'
        page.get_by_role('button', name='重做', exact=True).focus()
        page.keyboard.press('Enter')
        assert page.evaluate('workflowA11y.state.draft.steps[0].input') == ''
        page.get_by_role('button', name='撤销', exact=True).focus()
        page.keyboard.press('Enter')
        checks.append('connection manager is absent; clicking an edge still disconnects it with undo/redo')

        page.evaluate('workflowA11y.state.readonly = true')
        for control in page.locator('.simple-node [role="combobox"]').all():
            expect(control).to_be_disabled()
        for port in page.locator('.simple-port, .simple-ensemble-output-port, .simple-save-drop-target').all():
            expect(port).to_have_attribute('tabindex', '-1')
            expect(port).to_have_attribute('aria-hidden', 'true')
        before_readonly = page.evaluate('JSON.stringify({steps:workflowA11y.state.draft.steps,ensembles:workflowA11y.state.draft.ensembles})')
        page.get_by_role('button', name='适应视图', exact=True).focus()
        page.keyboard.press('Control+z')
        page.keyboard.press('Control+y')
        assert page.evaluate('JSON.stringify({steps:workflowA11y.state.draft.steps,ensembles:workflowA11y.state.draft.ensembles})') == before_readonly
        page.locator('.simple-node-editor__canvas').dispatch_event('contextmenu', {'button': 2, 'clientX': 70, 'clientY': 150})
        page.locator('.simple-node-editor__canvas').dispatch_event('dblclick', {'button': 0, 'clientX': 70, 'clientY': 150})
        expect(page.get_by_role('menu')).to_have_count(0)
        expect(page.get_by_role('dialog')).to_have_count(0)
        checks.append('readonly snapshots cannot edit and pointer-only ports leave Tab order')

        page.evaluate("workflowA11y.state.mode='bindings'")
        expect(page.locator('.binding-file')).to_have_count(3)
        expect(page.locator('.file-order-controls')).to_have_count(0)
        assert page.evaluate("workflowA11y.workbench.session('a11y').bindings.inputs.input.files") == [r'C:\音频\one.wav', 'C:/two.wav', 'C:/three.wav']
        page.get_by_role('button', name='移除 one.wav', exact=True).focus()
        page.keyboard.press('Enter')
        expect(page.locator('.binding-file')).to_have_count(2)
        checks.append('workbench file list has no added order controls and retains keyboard removal')
        page.screenshot(path=str(output / 'workbench-input.png'), full_page=True)

        page.evaluate("workflowA11y.state.mode='advanced'")
        canvas = page.locator('canvas.lg-canvas')
        expect(canvas).to_be_visible()
        description_id = canvas.get_attribute('aria-describedby')
        assert description_id
        expected_description = '高级画布暂不支持通过读屏软件编辑节点和连线。需要键盘编辑时，请在工作流库新建简易工作流。'
        expect(canvas).to_have_accessible_description(expected_description)
        page.get_by_text('键盘与读屏说明', exact=True).focus()
        page.keyboard.press('Enter')
        expect(page.locator('[id="' + description_id + '"]')).to_be_visible()
        checks.append('advanced canvas exposes its support limitation and simple-editor guidance as an accessible description')

        page.add_init_script("localStorage.setItem('pymss-studio:app-settings',JSON.stringify({startupOnboardingSeen:true,beginnerGuideInvitationSeen:true,beginnerGuideProgress:{status:'skipped',step:0},locale:'zh-CN',animationsEnabled:false}))")
        page.goto(args.base_url + '/', wait_until='networkidle')
        page.evaluate("""async () => {
          const [{useTaskStore},{useSettingsStore},{useBeginnerGuideStore}] = await Promise.all([
            import('/src/stores/task.ts'),import('/src/stores/settings.ts'),import('/src/stores/beginnerGuide.ts')])
          const settings=useSettingsStore();settings.startupOnboardingSeen=true;settings.locale='zh-CN';settings.animationsEnabled=false
          useBeginnerGuideStore().end('skipped')
          const task=useTaskStore();task.inputFiles=[['C:','音频','one.wav'].join(String.fromCharCode(92)),'C:/two.wav','C:/three.wav']
          window.inputTask=task
        }""")
        expect(page.locator('.file-chip')).to_have_count(3)
        expect(page.locator('.file-order-controls')).to_have_count(0)
        first = page.locator('.file-chip').first
        last = page.locator('.file-chip').last
        handle = first.locator('.file-chip__handle').bounding_box()
        target = last.bounding_box()
        page.mouse.move(handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2)
        page.mouse.down()
        page.mouse.move(target['x'] + 30, target['y'] + target['height'] * 0.8, steps=8)
        page.mouse.up()
        assert page.evaluate('inputTask.inputFiles') == ['C:/two.wav', 'C:/three.wav', r'C:\音频\one.wav']
        expect(page.locator('.file-chip--dragging')).to_have_count(0)
        page.screenshot(path=str(output / 'input-list.png'), full_page=True)
        checks.append('main input list has no added order controls and preserves pointer drag ordering')
        assert not errors, errors
        (output / 'results.json').write_text(json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False, indent=2))
    finally:
        browser.close()
