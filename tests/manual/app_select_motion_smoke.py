"""Verify select motion with pnpm dev and --base-url http://127.0.0.1:1420."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:1420')
parser.add_argument('--output', default='artifacts/app-select-motion')
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={'width': 1280, 'height': 960})
        page.set_default_timeout(8000)
        errors = []
        accessibility_warnings = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: accessibility_warnings.append(message.text) if 'Blocked aria-hidden' in message.text else None)
        page.goto(args.base_url + '/tests/fixtures/app-select.html', wait_until='networkidle')
        accessibility = page.context.new_cdp_session(page)
        accessibility.send('Accessibility.enable')
        checks = []
        opened = page.locator('.app-select__menu[data-state="open"]')

        def open_control(key='simple'):
            control = page.locator('#' + key)
            control.focus()
            control.press('Enter')
            expect(control).to_have_attribute('aria-expanded', 'true')
            expect(opened).to_be_visible()
            if control.evaluate('el => el.tagName') == 'BUTTON':
                page.wait_for_function("document.activeElement?.getAttribute('role') === 'option'")
            return control

        for key in ['simple', 'search', 'multiReadOnly']:
            control = open_control(key)
            assert opened.evaluate("el => getComputedStyle(el).animationName") == 'app-select-enter'
            assert opened.evaluate("el => getComputedStyle(el).animationDuration") == '0.14s'
            page.keyboard.press('Escape')
            expect(control).to_have_attribute('aria-expanded', 'false')
            expect(control).to_be_focused()
            expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('single, searchable and multiple menus use the same 140ms entry')

        for key in ['simple', 'search', 'multiReadOnly', 'large']:
            control = page.locator('#' + key)
            box = control.locator('xpath=ancestor::*[contains(@class,"app-select")][last()]').bounding_box()
            page.mouse.click(box['x'] + box['width'] - 12, box['y'] + box['height'] / 2)
            expect(control).to_have_attribute('aria-expanded', 'true')
            page.mouse.click(box['x'] + box['width'] - 12, box['y'] + box['height'] / 2)
            expect(control).to_have_attribute('aria-expanded', 'false')
            expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('clicking the arrow toggles every select variant open and closed')

        control = open_control()
        page.evaluate("""() => {
          window.exitState = null
          document.addEventListener('keydown', () => setTimeout(() => {
            const menu = document.querySelector('.app-select__menu')
            window.exitState = {focus: document.activeElement?.id, state: menu?.dataset.state,
              hidden: menu?.getAttribute('aria-hidden'), inert: menu?.inert,
              triggerHidden: !!document.getElementById('simple').closest('[aria-hidden="true"]'),
              duplicateIds: menu?.querySelectorAll('[id]').length,
              animation: menu && getComputedStyle(menu).animationName,
              duration: menu && getComputedStyle(menu).animationDuration}
          }, 0), {once: true, capture: true})
        }""")
        page.keyboard.press('Escape')
        page.wait_for_function('exitState !== null')
        snapshot = page.evaluate('exitState')
        assert snapshot == {'focus': 'simple', 'state': 'closed', 'hidden': 'true', 'inert': True, 'triggerHidden': False, 'duplicateIds': 0, 'animation': 'app-select-leave', 'duration': '0.08s'}, snapshot
        object_id = accessibility.send('Runtime.evaluate', {'expression': 'document.getElementById("simple")', 'returnByValue': False})['result']['objectId']
        ax_node = accessibility.send('Accessibility.getPartialAXTree', {'objectId': object_id, 'fetchRelatives': False})['nodes'][0]
        assert not ax_node['ignored'] and ax_node['role']['value'] == 'combobox', ax_node
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('80ms visual exit releases the real focus scope and keeps the trigger in the accessibility tree')

        open_control()
        page.keyboard.press('End')
        expect(page.get_by_role('option', name='Gamma', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        expect(control).to_be_focused()
        page.keyboard.press('Tab')
        # The clear button follows a selected plain control in DOM tab order.
        expect(control).not_to_be_focused()
        page.wait_for_timeout(160)
        expect(control).not_to_be_focused()
        checks.append('selection and following Tab are not undone when the fade-out ends')

        open_control()
        page.keyboard.press('Tab')
        expect(control).to_have_attribute('aria-expanded', 'false')
        focus_after_tab = page.evaluate('document.activeElement.outerHTML')
        page.wait_for_timeout(160)
        assert page.evaluate('document.activeElement.outerHTML') == focus_after_tab
        checks.append('Tab leaves the menu immediately with no later focus steal')

        for key in ['simple', 'search', 'multiReadOnly', 'large']:
            for _ in range(3):
                open_control(key)
                page.keyboard.press('Escape')
                open_control(key)
                expect(page.locator('.app-select__menu')).to_have_count(1)
                page.keyboard.press('Escape')
            expect(page.locator('.app-select__menu')).to_have_count(0)
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('reopening before exit completes retains option focus without duplicate menus')

        open_control('large')
        assert page.evaluate('selectFixture.state.large') == 0
        page.keyboard.press('Home')
        page.wait_for_function("document.getElementById('large').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('large').getAttribute('aria-activedescendant'))?.textContent.includes('0000')")
        page.keyboard.press('End')
        page.wait_for_function("document.getElementById('large').getAttribute('aria-activedescendant') && document.getElementById(document.getElementById('large').getAttribute('aria-activedescendant'))?.textContent.includes('1199')")
        original_scroll = opened.locator('.app-select__viewport').evaluate('el => el.scrollTop')
        page.evaluate("""() => {
          window.exitScroll = null
          document.addEventListener('keydown', () => setTimeout(() => {
            window.exitScroll = document.querySelector('.app-select__menu--exit-snapshot .app-select__viewport')?.scrollTop
          }, 0), {once:true, capture:true})
        }""")
        page.keyboard.press('Escape')
        page.wait_for_function('exitScroll !== null')
        assert page.evaluate('exitScroll') == original_scroll and original_scroll > 0, (original_scroll, page.evaluate('exitScroll'))
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('visual exit retains the virtual list scroll position')

        open_control()
        page.locator('#search').evaluate("el => el.closest('.app-select').style.cssText = 'position:fixed;top:30px;right:20px;width:240px;z-index:100'")
        page.locator('#search').click()
        expect(page.locator('#search')).to_be_focused()
        page.wait_for_timeout(160)
        expect(page.locator('#search')).to_be_focused()
        expect(opened).to_have_count(1)
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        page.locator('#search').evaluate("el => el.closest('.app-select').removeAttribute('style')")
        checks.append('switching to another dropdown while closing does not steal focus')

        search = page.locator('#search')
        search.focus()
        search.dispatch_event('compositionstart', {'data': ''})
        search.dispatch_event('keydown', {'key': 'Enter', 'code': 'Enter', 'keyCode': 229, 'isComposing': False})
        expect(search).to_have_attribute('aria-expanded', 'false')
        search.dispatch_event('compositionend', {'data': ''})
        expect(search).to_have_attribute('aria-expanded', 'true')
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        open_control('search')
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('IME confirmation cannot trigger the rapid-reopen Enter handler')

        page.evaluate('selectFixture.settings.animationsEnabled = false')
        control = open_control()
        assert opened.evaluate("el => getComputedStyle(el).animationName") == 'none'
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('application animation setting disables menus portaled to body')

        page.evaluate('selectFixture.settings.animationsEnabled = true')
        open_control()
        page.evaluate('selectFixture.settings.animationsEnabled = false')
        assert opened.evaluate("el => getComputedStyle(el).animationName") == 'none'
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('live setting changes cancel motion without retaining a menu')

        long_exit = page.add_style_tag(content='.app-select__menu--exit-snapshot{animation-duration:1s!important}')
        for cancel in ['setting', 'system']:
            page.evaluate('selectFixture.settings.animationsEnabled = true')
            open_control()
            page.keyboard.press('Escape')
            expect(page.locator('.app-select__menu--exit-snapshot')).to_have_count(1)
            if cancel == 'setting':
                page.evaluate('selectFixture.settings.animationsEnabled = false')
            else:
                page.emulate_media(reduced_motion='reduce')
            expect(page.locator('.app-select__menu--exit-snapshot')).to_have_count(0, timeout=500)
            page.emulate_media(reduced_motion='no-preference')
        long_exit.evaluate('el => el.remove()')
        checks.append('changing either motion preference during exit removes the visual copy immediately')

        page.evaluate('selectFixture.settings.animationsEnabled = true')
        page.emulate_media(reduced_motion='reduce')
        open_control()
        assert opened.evaluate("el => getComputedStyle(el).animationName") == 'none'
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        page.emulate_media(reduced_motion='no-preference')
        checks.append('system reduced-motion preference suppresses entry and exit')

        page.locator('#simple').evaluate("el => el.closest('.app-select').style.cssText = 'position:fixed;bottom:20px;left:30px;width:300px;z-index:100'")
        open_control()
        expect(opened).to_have_attribute('data-side', 'top')
        assert opened.evaluate("el => getComputedStyle(el).getPropertyValue('--app-select-enter-y').trim()") == '3px'
        page.keyboard.press('Escape')
        expect(page.locator('.app-select__menu')).to_have_count(0)
        page.locator('#simple').evaluate("el => el.closest('.app-select').removeAttribute('style')")
        checks.append('flipped placement reverses the entry offset')

        page.locator('#open-search-modal').click()
        modal = page.get_by_role('dialog', name='Search modal', exact=True)
        search = modal.get_by_role('combobox', name='Modal search', exact=True)
        search.click()
        assert modal.locator('.app-select__menu').evaluate("el => getComputedStyle(el).animationName") == 'app-select-enter'
        page.keyboard.press('Escape')
        expect(modal).to_be_visible()
        expect(search).to_be_focused()
        page.keyboard.press('Escape')
        expect(modal).not_to_be_visible()
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('modal portal preserves motion and successive Escape behavior')

        page.locator('#open-export').click()
        modal = page.get_by_role('dialog')
        depth = modal.get_by_role('combobox', name='WAV bit depth', exact=True)
        depth.focus()
        page.keyboard.press('Enter')
        expect(opened).to_be_visible()
        page.keyboard.press('Escape')
        expect(modal).to_be_visible()
        expect(depth).to_be_focused()
        page.keyboard.press('Escape')
        expect(modal).not_to_be_visible()
        expect(page.locator('.app-select__menu')).to_have_count(0)
        checks.append('plain select inside the export dialog preserves successive Escape behavior')

        touch_page = browser.new_page(viewport={'width':1280,'height':960}, has_touch=True)
        try:
            touch_page.goto(args.base_url + '/tests/fixtures/app-select.html', wait_until='networkidle')
            touch_page.evaluate('selectFixture.settings.animationsEnabled = false')
            for key in ['simple', 'search', 'multiReadOnly', 'large']:
                control = touch_page.locator('#' + key)
                control.scroll_into_view_if_needed()
                box = control.locator('xpath=ancestor::*[contains(@class,"app-select")][last()]').bounding_box()
                x, y = box['x'] + box['width'] - 12, box['y'] + box['height'] / 2
                touch_page.touchscreen.tap(x, y)
                expect(control).to_have_attribute('aria-expanded', 'true')
                touch_page.touchscreen.tap(x, y)
                expect(control).to_have_attribute('aria-expanded', 'false')
            checks.append('touch taps toggle the same four dropdown variants without reopening on pointerup')
        finally:
            touch_page.close()

        assert not errors, errors
        assert not accessibility_warnings, accessibility_warnings
        (output / 'results.json').write_text(json.dumps({'checks': checks, 'pageErrors': errors, 'accessibilityWarnings': accessibility_warnings, 'exitState': snapshot}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': len(checks), 'checks': checks}, indent=2))
    finally:
        browser.close()
