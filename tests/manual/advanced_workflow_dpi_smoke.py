"""Check advanced workflow raster resolution and pointer interaction across DPRs."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://localhost:1420')
parser.add_argument('--output-dir', type=Path, default=Path('.tmp/advanced-dpi'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)


def metrics(canvas):
    return canvas.evaluate('''e => ({
      dpr:window.devicePixelRatio, css:[e.getBoundingClientRect().width,e.getBoundingClientRect().height],
      backing:[e.width,e.height], bg:[e.data.bgcanvas.width,e.data.bgcanvas.height],
      visible:[...e.data.visible_area], scale:e.data.ds.scale, offset:[...e.data.ds.offset],
      nativeDprGetter:Object.getOwnPropertyDescriptor(window,'devicePixelRatio').get === window.__dpiGetterBefore,
    })''')


def verify_resolution(canvas, ratio):
    canvas.page.wait_for_function('''ratio => {
      const e=document.querySelector('canvas.lg-canvas')
      const r=e.getBoundingClientRect()
      return window.devicePixelRatio===ratio && e.width===Math.round(r.width*ratio)
        && e.height===Math.round(r.height*ratio) && e.data.bgcanvas.width===e.width
        && e.data.bgcanvas.height===e.height
    }''', arg=ratio)
    canvas.evaluate('e => e.data.draw(true,true)')
    result = metrics(canvas)
    assert result['nativeDprGetter'], result
    assert abs(result['visible'][2] - result['css'][0] / result['scale']) < 1, result
    assert abs(result['visible'][3] - result['css'][1] / result['scale']) < 1, result
    return result


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        results = []
        for ratio in [0.75, 1, 1.25, 1.5, 2, 2.25]:
            page = browser.new_page(viewport={'width':1280,'height':840}, device_scale_factor=ratio)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script('''
              window.__dpiGetterBefore=Object.getOwnPropertyDescriptor(window,'devicePixelRatio').get
              localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
                startupOnboardingSeen:true,locale:'zh-CN',themeMode:'dark',scaleFactor:1,
              }))
            ''')
            page.goto(args.url.rstrip('/')+'/#/workflow-node-editor?new=1')
            page.wait_for_load_state('networkidle')
            canvas = page.locator('canvas.lg-canvas')
            expect(canvas).to_be_visible()
            page.wait_for_function("document.querySelector('canvas.lg-canvas')?.data?.graph?.nodes?.length===4")
            initial = verify_resolution(canvas, ratio)
            canvas.screenshot(path=str(args.output_dir / f'after-dpr-{ratio}.png'))

            old_pixel = None
            if ratio < 1:
                old_pixel = canvas.evaluate('''e => {
                  const editor=e.data,node=editor.graph.nodes.find(n=>n.type==='pymss_load_audio')
                  editor.clear_background_color=null;editor.background_image=null;editor.draw(true,true)
                  const p=editor.ds.convertOffsetToCanvas([node.pos[0]+8,node.pos[1]+8])
                  const xy=p.map(v=>Math.round(v*window.devicePixelRatio))
                  return {xy,alpha:editor.ctx.getImageData(...xy,1,1).data[3]}
                }''')
                assert old_pixel['alpha'] > 0, old_pixel

            node = canvas.evaluate('''e => {
              const editor=e.data, node=editor.graph.nodes.find(n=>n.type==='pymss_load_audio')
              const r=e.getBoundingClientRect(), p=editor.ds.convertOffsetToCanvas([node.pos[0]+80,node.pos[1]-12])
              return {id:node.id,pos:[...node.pos],point:[r.left+p[0],r.top+p[1]],scale:editor.ds.scale}
            }''')
            page.mouse.move(*node['point'])
            page.mouse.down()
            # Cross LiteGraph's drag threshold before measuring motion.
            page.mouse.move(node['point'][0]+10, node['point'][1]+10)
            armed = canvas.evaluate('(e,id) => [...e.data.graph.getNodeById(id).pos]', node['id'])
            page.mouse.move(node['point'][0]+50, node['point'][1]+34, steps=8)
            page.mouse.up()
            moved = canvas.evaluate('(e,id) => [...e.data.graph.getNodeById(id).pos]', node['id'])
            assert abs(moved[0]-armed[0]-40/node['scale']) < 2, {'ratio':ratio,'before':armed,'after':moved}
            assert abs(moved[1]-armed[1]-24/node['scale']) < 2, {'ratio':ratio,'before':armed,'after':moved}
            verify_resolution(canvas, ratio)
            if old_pixel:
                alpha = canvas.evaluate('(e,xy) => e.data.ctx.getImageData(...xy,1,1).data[3]', old_pixel['xy'])
                assert alpha == 0, {'ratio':ratio,'stale_alpha':alpha,'old_pixel':old_pixel}

            ports = canvas.evaluate('''e => {
              const editor=e.data, r=e.getBoundingClientRect()
              const from=editor.graph.nodes.find(n=>n.type==='mss_separate')
              const to=editor.graph.nodes.find(n=>n.type==='pymss_save_audio')
              const screen=p=>{const c=editor.ds.convertOffsetToCanvas(p);return [r.left+c[0],r.top+c[1]]}
              return {from:screen(from.getConnectionPos(false,1)),to:screen(to.getConnectionPos(true,1)),links:editor.graph.serialize().links.length}
            }''')
            page.mouse.move(*ports['from'])
            page.mouse.down()
            page.mouse.move(*ports['to'], steps=12)
            page.mouse.up()
            links = canvas.evaluate('e => e.data.graph.serialize().links.length')
            assert links == ports['links']+1, {'ratio':ratio,'ports':ports,'links':links}

            selection = canvas.evaluate('''e => {
              const editor=e.data,node=editor.graph.nodes.find(n=>n.type==='pymss_load_audio')
              editor.deselectAll()
              const r=e.getBoundingClientRect()
              const screen=p=>{const c=editor.ds.convertOffsetToCanvas(p);return [r.left+c[0],r.top+c[1]]}
              return {id:node.id,from:screen([node.pos[0]-20,node.pos[1]-50]),
                to:screen([node.pos[0]+node.size[0]+20,node.pos[1]+node.size[1]+20])}
            }''')
            page.keyboard.down('Control')
            page.mouse.move(*selection['from'])
            page.mouse.down()
            page.mouse.move(*selection['to'], steps=10)
            page.mouse.up()
            page.keyboard.up('Control')
            selected = canvas.evaluate('e => Object.values(e.data.selected_nodes).map(node=>node.id)')
            assert selected == [selection['id']], {'ratio':ratio,'selected':selected,'selection':selection}

            # A DPI-only change must resize the raster even when the CSS viewport stays fixed.
            changed_ratio = 2 if ratio != 2 else 1.25
            cdp = page.context.new_cdp_session(page)
            cdp.send('Emulation.setDeviceMetricsOverride', {
                'width':1280,'height':840,'deviceScaleFactor':changed_ratio,'mobile':False,
            })
            changed = verify_resolution(canvas, changed_ratio)
            cdp.send('Emulation.setDeviceMetricsOverride', {
                'width':1120,'height':760,'deviceScaleFactor':changed_ratio,'mobile':False,
            })
            resized = verify_resolution(canvas, changed_ratio)
            assert resized['css'][0] < changed['css'][0], resized
            assert not errors, errors
            results.append({'initial':initial,'changed':changed,'resized':resized,
                            'drag_node':True,'connect_ports':True,'rectangle_selection':True,
                            'no_stale_pixels':True if old_pixel else None,'page_errors':errors})
            page.close()
        (args.output_dir/'browser-result.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(json.dumps(results))
    finally:
        browser.close()
