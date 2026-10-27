"""Check the separation log footer with the actual task store and rendered app."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:1435")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""localStorage.setItem('pymss-studio:app-settings', JSON.stringify({
      startupOnboardingSeen:true,locale:'zh-CN',themeMode:'light',developerMode:false
    }))""")
    page.goto(args.url.rstrip("/") + "/#/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_load_state("networkidle")
    skip = page.get_by_role("button", name="跳过", exact=True)
    if skip.is_visible():
        skip.click()
    page.evaluate("""() => {
      const stores=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s;
      window.__tasks=stores.get('task'); window.__settings=stores.get('settings');
      const now=Date.now();
      __tasks.tasks=[{id:'log-progress',jobId:'log-progress',model:'roformer',input:'D:/audio/song.flac',
        output:'D:/audio/output',status:'separating',progress:0,stageLabel:'Separating',message:'Separating',
        createdAt:now-60000,updatedAt:now,startedAt:now-60000,files:[],outputs:[],runConfig:{runMode:'model'},
        logs:Array.from({length:80},(_,i)=>({schemaVersion:1,id:'line-'+i,timestamp:new Date(now).toISOString(),
          source:'pymss',level:'info',logger:'pymss.separator',event:'log',message:'Processing record '+i,context:{}}))}];
    }""")
    page.get_by_role("button", name="日志", exact=True).click()
    modal = page.locator(".task-log-modal")
    footer = modal.locator(".task-log-progress")
    bar = footer.get_by_role("progressbar")
    expect(footer).to_be_visible()
    expect(footer).to_contain_text("等待首个处理进度")
    assert bar.get_attribute("aria-valuenow") is None

    def progress(done, total, unit):
        page.evaluate("""({done,total,unit}) => __tasks.handleWorkerEvent({type:'task_progress',taskId:'log-progress',
          payload:{stage:'separating',message:'Processing audio',done,total,unit}})""", {
            "done": done, "total": total, "unit": unit,
        })

    progress(88, 231, "seconds")
    expect(bar).to_have_attribute("aria-valuenow", "38")
    expect(footer).to_contain_text("累计处理时长 00:01:28 / 00:03:51")
    expect(modal.locator(".log-line")).to_have_count(80)
    console = modal.locator(".log-console")
    modal.evaluate("element => Promise.all(element.getAnimations().map(animation => animation.finished.catch(() => {})))")
    before = footer.bounding_box()
    console.evaluate("element => { element.scrollTop=0 }")
    after = footer.bounding_box()
    assert abs(before["y"] - after["y"]) < 1, (before, after)
    assert console.evaluate("element => element.scrollHeight > element.clientHeight")

    for theme in ["light", "dark"]:
        page.evaluate("theme => __settings.themeMode=theme", theme)
        page.wait_for_function(r"""dark => {
          const color=getComputedStyle(document.querySelector('.task-log-modal')).backgroundColor.match(/\d+/g);
          return color && (dark ? Number(color[0]) < 128 : Number(color[0]) > 128);
        }""", arg=theme == "dark")
        modal.evaluate("element => Promise.all(element.getAnimations().map(animation => animation.finished.catch(() => {})))")
        expect(footer).to_be_visible()
        page.screenshot(path=str(args.output_dir / f"separation-log-{theme}.png"), full_page=True)

    page.set_viewport_size({"width": 780, "height": 560})
    expect(footer).to_be_in_viewport()
    bounds = modal.bounding_box()
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 560, bounds
    assert modal.evaluate("element => element.scrollWidth <= element.clientWidth")
    assert footer.bounding_box()["height"] <= 48
    text_rows = footer.locator(".task-log-progress__details > *").evaluate_all(
        "elements => elements.map(element => element.getBoundingClientRect().y)"
    )
    assert max(text_rows) - min(text_rows) < 1, text_rows
    page.screenshot(path=str(args.output_dir / "separation-log-narrow.png"), full_page=True)
    progress(12, 231, "patches")
    expect(footer).to_contain_text("已处理片段 12 / 231")
    expect(footer).not_to_contain_text("00:00:12")

    page.evaluate("""() => __tasks.handleWorkerEvent({type:'task_cancelled',taskId:'log-progress',payload:{message:'Cancelled'}})""")
    expect(footer).to_contain_text("已取消")
    expect(bar).to_have_attribute("aria-valuenow", "5")
    progress(200, 231, "seconds")
    expect(bar).to_have_attribute("aria-valuenow", "5")
    assert not errors, errors
    browser.close()

print("PASS: normal-mode live progress, units, fixed footer, themes, narrow layout and cancellation")
