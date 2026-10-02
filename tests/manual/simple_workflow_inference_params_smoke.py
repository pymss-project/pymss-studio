"""Headless UI smoke for per-step inference settings in the simple workflow editor."""

import json
import os

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


smoke_model_type = os.environ.get("PYMSS_SMOKE_MODEL_TYPE", "mel_band_roformer")
is_vr = smoke_model_type == "vr"
model_name = "workflow-vr-model.pth" if is_vr else "workflow-model.ckpt"
default_inference_params = (
    {
        "batch_size": 1,
        "window_size": 512,
        "aggression": 5,
        "enable_post_process": False,
        "post_process_threshold": 0.2,
        "high_end_process": False,
        "normalize": False,
    }
    if is_vr
    else {"batch_size": 1, "overlap_size": 44100, "chunk_size": 352800}
)
model = {
    "name": model_name,
    "aliases": [],
    "modelType": smoke_model_type,
    "architecture": smoke_model_type,
    "supported": True,
    "unsupportedReason": "",
    "category": "vocal",
    "categoryCn": "人声",
    "primaryCategory": "vocal",
    "primaryCategoryCn": "人声",
    "secondaryCategory": "",
    "secondaryCategoryCn": "",
    "targetStem": "Vocals",
    "configInstruments": "Vocals,Instrumental",
    "configTargetInstrument": "Vocals",
    "classificationConfidence": "high",
    "classificationBasis": "catalog",
    "sizeBytes": 1,
    "sha256": "",
    "downloaded": True,
    "missingPaths": [],
    "modelPath": f"D:/Models/{model_name}",
    "configPath": None,
    "auxiliaryPaths": [],
    "defaultInferenceParams": default_inference_params,
    "defaultInferenceParamsResolved": True,
    "inferenceParamMeta": ({"recommendedSampleStep": 44100, "source": "smoke"} if not is_vr else {}),
}
definition = {
    "version": 1,
    "defaults": {"device": "auto", "output_format": "wav"},
    "steps": [{
        "id": "split",
        "model": model_name,
        "input": "input",
        "stems": ["Vocals", "Instrumental"],
        "save": {"Vocals": "Default"},
        "output_names": {"Vocals": "%filename%_%stem%_%model%"},
    }],
}
workflow_state = {
    "selectedWorkflowId": "simple-inference",
    "workflows": [{
        "id": "simple-inference",
        "name": "Simple Inference",
        "description": "",
        "definition": definition,
        "format": "simple",
        "formatVersion": 1,
        "createdAt": 1,
        "updatedAt": 1,
    }],
}
model_state = {
    "models": [model],
    "categories": ["vocal"],
    "categoriesCn": ["人声"],
    "count": 1,
    "modelDir": "D:/Models",
    "modelInferenceOverrides": {
        model_name: {"batch_size": 4} if is_vr else {"batch_size": 4, "chunk_size": 485100}
    },
}


with sync_playwright() as playwright:
    locale = os.environ.get("PYMSS_SMOKE_LOCALE", "en")
    locale_json = json.dumps(locale)
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        f"""(() => {{
          localStorage.setItem(
            'pymss-studio:app-settings',
            JSON.stringify({{ startupOnboardingSeen: true, locale: {locale_json} }}),
          )
          localStorage.setItem('pymss-studio:workflow-state', JSON.stringify({json.dumps(workflow_state)}))
          localStorage.setItem('pymss-studio:model-state', JSON.stringify({json.dumps(model_state)}))
        }})()"""
    )
    base_url = os.environ.get("PYMSS_SMOKE_URL", "http://127.0.0.1:1420")
    page.goto(
        f"{base_url}/#/workflow-simple-editor?workflowId=simple-inference",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        # Vite's development WebSocket can keep the page non-idle on Windows.
        page.wait_for_selector(".simple-node-editor", state="visible", timeout=60_000)

    step = page.locator(".simple-node--step")
    expect(step.locator(".simple-inference-settings")).to_be_visible()
    step.locator(".simple-inference-settings").click()
    modal = page.locator(".simple-inference-modal")
    expect(modal).to_be_visible()
    page.wait_for_timeout(250)
    screenshot_suffix = f"{smoke_model_type}-{locale}"
    page.screenshot(path=f"data/outputs/simple-workflow-inference-global-{screenshot_suffix}.png", full_page=True)
    follow_switch = modal.locator(".simple-inference-follow").get_by_role("switch")
    expect(follow_switch).to_have_attribute("aria-checked", "true")
    follow_switch.click()
    expect(follow_switch).to_have_attribute("aria-checked", "false")
    page.wait_for_timeout(250)
    batch_input = modal.locator(".simple-inference-grid label").first.locator("input")
    expect(batch_input).to_have_value("4")
    batch_input.fill("3")
    page.screenshot(path=f"data/outputs/simple-workflow-inference-modal-{screenshot_suffix}.png", full_page=True)
    modal.locator(".simple-inference-modal__footer .n-button--primary-type").click()

    expect(step.locator(".simple-inference-settings--custom")).to_be_visible()
    expect(modal).to_be_hidden()
    page.screenshot(path=f"data/outputs/simple-workflow-inference-node-custom-{screenshot_suffix}.png", full_page=True)
    page.locator(".simple-node-editor__topbar .n-button--primary-type").click()
    expect(page.locator(".n-message__content")).to_be_visible()
    stored = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state'))")
    saved = stored["workflows"][0]["definition"]
    assert saved["steps"][0]["model_type"] == smoke_model_type
    assert saved["steps"][0]["inference_params"]["batch_size"] == 3
    if is_vr:
        assert saved["steps"][0]["inference_params"]["window_size"] == 512
    else:
        assert saved["steps"][0]["inference_params"]["chunk_size"] == 485100
    assert "inference_params" not in saved["defaults"]

    step.locator(".simple-inference-settings").click()
    follow_switch.click()
    expect(follow_switch).to_have_attribute("aria-checked", "true")
    modal.locator(".simple-inference-modal__footer .n-button--primary-type").click()
    page.locator(".simple-node-editor__topbar .n-button--primary-type").click()
    stored = page.evaluate("JSON.parse(localStorage.getItem('pymss-studio:workflow-state'))")
    assert "inference_params" not in stored["workflows"][0]["definition"]["steps"][0]

    page.screenshot(path=f"data/outputs/simple-workflow-inference-params-{screenshot_suffix}.png", full_page=True)
    assert not errors, errors
    print("simple workflow inference parameter UI smoke passed")
    browser.close()
