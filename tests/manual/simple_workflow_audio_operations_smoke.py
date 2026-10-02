"""Exercise simple audio operations, downstream links, and persisted workflows."""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def catalog_model(name, stems):
    return {
        "name": name,
        "aliases": [],
        "modelType": "mss",
        "supported": True,
        "downloaded": True,
        "missingPaths": [],
        "modelPath": f"D:/Models/{name}",
        "configInstruments": ",".join(stems),
        "targetStem": stems[0],
    }


def connect(page, source, target):
    source_port = page.locator(f'[data-simple-source="{source}"]')
    target_port = page.locator(f'[data-simple-target="{target}"]')
    source_box = source_port.bounding_box()
    target_box = target_port.bounding_box()
    assert source_box and target_box
    for port, box, event in (
        (source_port, source_box, "pointerdown"),
        (target_port, target_box, "pointerup"),
    ):
        port.dispatch_event(event, {
            "button": 0,
            "pointerId": 1,
            "clientX": box["x"] + box["width"] / 2,
            "clientY": box["y"] + box["height"] / 2,
        })


def add_operation(page, operation):
    page.get_by_role("button", name="Add Step", exact=True).click()
    chooser = page.locator(".simple-node-type-modal")
    expect(chooser).to_be_visible()
    expect(chooser.locator(".simple-node-type-modal__choices button")).to_have_count(5)
    if operation == "sum":
        expect(chooser).to_have_css("opacity", "1")
        page.screenshot(path="data/outputs/simple-audio-node-chooser.png", full_page=True)
    chooser.locator(f'[data-add-audio-operation="{operation}"]').click()
    node = page.locator(f'[data-audio-operation="{operation}"]')
    expect(node).to_have_count(1)
    expect(node.locator(".n-input-number")).to_have_count(0)
    return node


def workflow_entry(workflow_id, definition):
    return {
        "id": workflow_id,
        "name": workflow_id,
        "description": "",
        "definition": definition,
        "format": "simple",
        "formatVersion": 1,
        "createdAt": 1,
        "updatedAt": 1,
    }


def main():
    stems = ["Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"]
    definition = {
        "version": 1,
        "defaults": {"device": "auto", "output_format": "wav", "inference_params": {"normalize": False}},
        "steps": [
            {"id": "sw", "model": "sw.ckpt", "input": "input", "stems": stems, "save": {}},
            {"id": "gabox", "model": "gabox.ckpt", "input": "input", "stems": ["Instrumental"], "save": {}},
        ],
    }
    invert_only = {
        "version": 1,
        "defaults": {"device": "auto", "output_format": "wav"},
        "steps": [],
        "ensembles": [{
            "id": "phase", "algorithm": "invert", "output_stem": "Inverted",
            "inputs": [{"source": "input", "weight": 1}],
            "save": "Default", "output_name": "%filename%_%stem%_%step%",
        }],
    }
    workflow_state = {
        "selectedWorkflowId": "issue74",
        "workflows": [workflow_entry("issue74", definition), workflow_entry("invert-only", invert_only)],
    }
    model_state = {
        "models": [catalog_model("sw.ckpt", stems), catalog_model("gabox.ckpt", ["Instrumental"])],
        "count": 2,
        "categories": [],
        "categoriesCn": [],
        "modelDir": "D:/Models",
    }
    output_dir = Path("data/outputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(f"""(() => {{
              if (localStorage.getItem('pymss-studio:workflow-state')) return;
              localStorage.setItem('pymss-studio:app-settings', JSON.stringify({{startupOnboardingSeen: true, locale: 'en'}}));
              localStorage.setItem('pymss-studio:workflow-state', JSON.stringify({json.dumps(workflow_state)}));
              localStorage.setItem('pymss-studio:model-state', JSON.stringify({json.dumps(model_state)}));
            }})()""")
            page.goto("http://localhost:1420/#/workflow-simple-editor?workflowId=issue74")
            page.wait_for_load_state("networkidle")
            expect(page.locator(".simple-node--step")).to_have_count(2)

            sum_node = add_operation(page, "sum")
            sum_id = sum_node.get_attribute("data-simple-node")
            for _ in range(3):
                sum_node.get_by_role("button", name="Add Input", exact=True).click()
            expect(sum_node.locator(".simple-ensemble-input-row")).to_have_count(5)
            # Free each previous source before assigning it to the preceding port.
            for index, stem in reversed(list(enumerate(stems[1:]))):
                connect(page, f"sw.{stem}", f"ensemble:{sum_id}:{index}")
            for stem in stems[1:]:
                expect(sum_node).to_contain_text(f"sw.ckpt · {stem}")

            difference = add_operation(page, "subtract")
            difference_id = difference.get_attribute("data-simple-node")
            expect(difference.locator(".simple-ensemble-input-row")).to_have_count(2)
            expect(difference.locator(".simple-ensemble-input-row__port").first).to_have_text("A")
            expect(difference.locator(".simple-ensemble-input-row__port").last).to_have_text("B")
            expect(difference).to_contain_text("Original input")
            expect(difference).to_contain_text("sw.ckpt · Vocals")
            expect(difference.get_by_role("button", name="Add Input", exact=True)).to_have_count(0)

            invert = add_operation(page, "invert")
            invert_id = invert.get_attribute("data-simple-node")
            expect(invert.locator(".simple-ensemble-input-row")).to_have_count(1)
            connect(page, f"{difference_id}.Difference", f"ensemble:{invert_id}:0")
            expect(invert).to_contain_text("Subtract Audio · Difference")
            page.get_by_role("button", name="Undo", exact=True).click()
            expect(invert).to_contain_text("sw.ckpt · Vocals")
            page.get_by_role("button", name="Redo", exact=True).click()
            expect(invert).to_contain_text("Subtract Audio · Difference")

            page.get_by_role("button", name="Add Step", exact=True).click()
            page.locator(".simple-node-type-modal__choices button", has_text="Ensemble").click()
            blend = page.locator(".simple-node--ensemble:not(.simple-node--audio-operation)")
            blend_id = blend.get_attribute("data-simple-node")
            connect(page, f"{sum_id}.Sum", f"ensemble:{blend_id}:0")
            connect(page, "gabox.Instrumental", f"ensemble:{blend_id}:1")
            expect(blend).to_contain_text("Add Audio · Sum")
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
            connect(page, f"{difference_id}.Difference", f"ensemble:{blend_id}:0")
            expect(blend).to_contain_text("Subtract Audio · Difference")
            connect(page, f"{sum_id}.Sum", f"ensemble:{blend_id}:0")

            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator(".n-message__content", has_text="Workflow saved")).to_be_visible()
            saved = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows.find(item => item.id === 'issue74').definition")
            assert [node["algorithm"] for node in saved["ensembles"]] == ["sum", "subtract", "invert", "avg_wave"]
            assert [item["source"] for item in saved["ensembles"][0]["inputs"]] == [f"sw.{stem}" for stem in stems[1:]]
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator('[data-audio-operation="sum"] .simple-ensemble-input-row')).to_have_count(5)
            expect(page.locator('[data-audio-operation="invert"]')).to_contain_text("Subtract Audio · Difference")
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
            page.screenshot(path=str(output_dir / "simple-audio-operations.png"), full_page=True)

            page.goto("http://localhost:1420/#/workflow-simple-editor?workflowId=invert-only")
            page.wait_for_load_state("networkidle")
            expect(page.locator(".simple-node--step")).to_have_count(0)
            expect(page.locator('[data-audio-operation="invert"]')).to_contain_text("Original input")
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator(".n-message__content", has_text="Workflow saved")).to_be_visible()
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(".simple-node--step")).to_have_count(0)
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Fit view", exact=True).click()
            page.screenshot(path=str(output_dir / "simple-invert-only.png"), full_page=True)
            assert not errors, errors
            print("simple audio operations UI smoke passed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
