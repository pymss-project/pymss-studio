# convert

Independent Studio tool for batch audio conversion. Exposes `pymss.convert.batch` and a custom protocol-v1 tool page.

Supports WAV, FLAC, MP3 and OGG; 32/44.1/48 kHz; mono/stereo; format-specific encoding quality; recursive folder scanning; per-file failure reporting; progress and cancellation. Conversion reuses pymss's shared FFmpeg file service. FFmpeg must be available through the host runtime.

The tool page requires the Studio SDK methods `app.info`, `files.pickAudio`, `files.pickDirectory`, `files.scanAudio`, `files.reveal`, `settings.read`, `settings.write`, `operations.start` and `operations.cancel`. It is intended for Studio versions that include the preinstalled-tool host changes. Older view hosts can still describe the Operation but do not provide the complete interactive page.

Input is `files: {paths: string[]}`. Parameters include `output_dir`, `format`, `sample_rate`, `channels` and encoding-quality fields. Output is a `report` containing `outputPaths`, `outputDir`, `succeeded` and `failed`. Input paths are validated before conversion starts; duplicate paths are processed once and existing outputs are preserved by the shared file service.
