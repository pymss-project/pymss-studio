"""Verify inherited inference settings and persisted VR model types in the editor."""

import json
import os

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


MODEL_NAME = "inference-model.ckpt"


def open_editor(browser, *, normalize, model_normalize=False, vr=False):
    definition = {
        "version": 1,
        "defaults": {"device": "auto", "output_format": "wav", "inference_params": {"normalize": normalize}},
        "steps": [{
            "id": "split", "model": MODEL_NAME, "input": "input", "stems": ["Vocals"],
            "save": {"Vocals": "Default"},
            "inference_params": {"window_size": 1024, "aggression": 0} if vr else {"batch_size": 3},
            **({"model_type": " VR "} if vr else {}),
        }],
    }
    model = {
        "name": MODEL_NAME, "aliases": [], "modelType": "mss", "supported": True,
        "downloaded": True, "missingPaths": [], "modelPath": f"D:/Models/{MODEL_NAME}",
        "configInstruments": "Vocals", "defaultInferenceParamsResolved": True,
        "defaultInferenceParams": {
            "batch_size": 1, "normalize": model_normalize,
            **({"overlap_size": 44100, "chunk_size": 352800} if not vr else {}),
        },
    }
    overrides = {
        "batch_size": 4, "window_size": 4096, "aggression": 5,
        "enable_post_process": True, "post_process_threshold": 0.25,
        "high_end_process": True, "normalize": False,
    } if vr else {"batch_size": 4}
    state = {
        "selectedWorkflowId": "inference-regression",
        "workflows": [{
            "id": "inference-regression", "name": "Inference", "description": "",
            "definition": definition, "format": "simple", "formatVersion": 1,
            "createdAt": 1, "updatedAt": 1,
        }],
    }
    models = {"models": [] if vr else [model], "modelInferenceOverrides": {MODEL_NAME: overrides}}
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    page.add_init_script(f"""(() => {{
      localStorage.setItem('pymss-studio:app-settings', JSON.stringify({{startupOnboardingSeen:true,locale:'en'}}));
      localStorage.setItem('pymss-studio:workflow-state', JSON.stringify({json.dumps(state)}));
      localStorage.setItem('pymss-studio:model-state', JSON.stringify({json.dumps(models)}));
    }})()""")
    base_url = os.environ.get("PYMSS_SMOKE_URL", "http://127.0.0.1:1420")
    page.goto(f"{base_url}/#/workflow-simple-editor?workflowId=inference-regression", wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        page.locator(".simple-node-editor").wait_for(state="visible")
    page.locator(".simple-inference-settings").click()
    modal = page.locator(".simple-inference-modal")
    expect(modal).to_be_visible()
    return page, modal


def persist(page, modal):
    modal.locator(".simple-inference-modal__footer .n-button--primary-type").click()
    expect(modal).to_be_hidden()
    previous_update = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].updatedAt")
    page.locator(".simple-node-editor__topbar .n-button--primary-type").click()
    page.wait_for_function(
        "previous => JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].updatedAt > previous",
        arg=previous_update,
    )
    expect(page.locator(".n-message__content").last).to_be_visible()
    return page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].definition")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for workflow_normalize in (True, False):
                page, modal = open_editor(browser, normalize=workflow_normalize, model_normalize=not workflow_normalize)
                normalize_switch = modal.locator(".simple-inference-switches label").nth(1).get_by_role("switch")
                expect(normalize_switch).to_have_attribute("aria-checked", str(workflow_normalize).lower())
                saved = persist(page, modal)
                assert saved["steps"][0]["inference_params"] == {"batch_size": 3}
                assert saved["defaults"]["inference_params"]["normalize"] is workflow_normalize

                page.locator(".simple-inference-settings").click()
                modal.locator(".simple-inference-grid label").first.locator("input").fill("5")
                normalize_switch.click()
                saved = persist(page, modal)
                assert saved["steps"][0]["inference_params"] == {
                    "batch_size": 5, "normalize": not workflow_normalize,
                }

                page.locator(".simple-inference-settings").click()
                expect(normalize_switch).to_have_attribute("aria-checked", str(not workflow_normalize).lower())
                follow = modal.locator(".simple-inference-follow").get_by_role("switch")
                follow.click()
                expect(normalize_switch).to_have_attribute("aria-checked", str(workflow_normalize).lower())
                saved = persist(page, modal)
                assert "inference_params" not in saved["steps"][0]

                page.locator(".simple-inference-settings").click()
                follow.click()
                saved = persist(page, modal)
                assert saved["steps"][0]["inference_params"]["normalize"] is workflow_normalize
                assert saved["steps"][0]["inference_params"]["batch_size"] == 4
                page.close()

            page, modal = open_editor(browser, normalize=True, vr=True)
            fields = modal.locator(".simple-inference-grid label")
            expect(fields.filter(has_text="Window Size").locator("input")).to_have_value("1024")
            expect(fields.filter(has_text="Aggression").locator("input")).to_have_value("0")
            expect(fields.filter(has_text="Batch Size").locator("input")).to_have_value("4")
            expect(fields.filter(has_text="Post-process Threshold").locator("input")).to_have_value("0.25")
            expect(fields.filter(has_text="Overlap Size")).to_have_count(0)
            expect(fields.filter(has_text="Chunk Size")).to_have_count(0)
            fields.filter(has_text="Window Size").locator("input").fill("1536")
            saved = persist(page, modal)
            assert saved["steps"][0]["model_type"] == "VR"
            assert saved["steps"][0]["inference_params"] == {"window_size": 1536, "aggression": 0}
            page.close()

            page, modal = open_editor(browser, normalize=True)
            fields = modal.locator(".simple-inference-grid label")
            fields.filter(has_text="Batch Size").locator("input").fill("1.5")
            fields.filter(has_text="Overlap Size").locator("input").fill("0")
            fields.filter(has_text="Chunk Size").locator("input").fill("44100.5")
            saved = persist(page, modal)
            assert saved["steps"][0]["inference_params"] == {"batch_size": 2, "overlap_size": 0, "chunk_size": 44101}
            page.locator(".simple-inference-settings").click()
            fields.filter(has_text="Overlap Size").locator("input").fill("44100.5")
            fields.filter(has_text="Chunk Size").locator("input").fill("0")
            saved = persist(page, modal)
            assert saved["steps"][0]["inference_params"] == {"batch_size": 2, "overlap_size": 44101, "chunk_size": 0}
            page.close()

            page, modal = open_editor(browser, normalize=True, vr=True)
            fields = modal.locator(".simple-inference-grid label")
            fields.filter(has_text="Batch Size").locator("input").fill("1.5")
            fields.filter(has_text="Window Size").locator("input").fill("1024.5")
            fields.filter(has_text="Aggression").locator("input").fill("1.5")
            fields.filter(has_text="Post-process Threshold").locator("input").fill("1.5")
            saved = persist(page, modal)
            assert saved["steps"][0]["inference_params"] == {
                "batch_size": 2, "window_size": 1025, "aggression": 2, "post_process_threshold": 1,
            }
            page.locator(".simple-inference-settings").click()
            fields.filter(has_text="Post-process Threshold").locator("input").fill("-0.5")
            saved = persist(page, modal)
            assert saved["steps"][0]["inference_params"]["post_process_threshold"] == 0
            page.close()

            page, modal = open_editor(browser, normalize=True)
            page.evaluate("""name => {
              document.querySelector('.simple-node-editor').__vueParentComponent.props.modelInferenceOverrides[name].batch_size = 1.5;
            }""", MODEL_NAME)
            follow = modal.locator(".simple-inference-follow").get_by_role("switch")
            follow.click()
            follow.click()
            modal.locator(".simple-inference-modal__footer .n-button--primary-type").click()
            expect(modal).to_be_visible()
            expect(page.locator(".n-message__content", has_text="batch_size")).to_be_visible()
            stored = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state')).workflows[0].definition")
            assert stored["steps"][0]["inference_params"] == {"batch_size": 3}
            modal.locator(".simple-inference-grid label").first.locator("input").fill("5")
            saved = persist(page, modal)
            assert saved["steps"][0]["inference_params"]["batch_size"] == 5
            page.close()
            print("simple workflow inference inheritance, VR fallback, and numeric boundaries passed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
