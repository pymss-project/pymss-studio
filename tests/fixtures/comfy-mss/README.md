# comfy-mss example workflows

Verbatim copies of the workflows used by the import tests from `examples/` in [comfy-mss](https://github.com/pymss-project/comfy-mss)
ComfyUI extension (MIT, Copyright (c) 2026 pymss-project), vendored so the import tests run
against workflows ComfyUI actually saved rather than fixtures written from reading our own code.

That distinction has already paid for itself: importing them surfaced a `*_separate_list` node
being read as producing a stem literally named `audios`, which is the name of its bundled list
output, not a stem any model produces.

Refresh the referenced workflow files when comfy-mss changes its node schema.
