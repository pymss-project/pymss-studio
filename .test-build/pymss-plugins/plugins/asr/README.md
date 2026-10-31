# asr

Speech-to-text plugin for pymss and Studio. Exposes `pymss.asr.transcribe` as an Operation and `asr_transcribe` as a Python capability.

Install this directory through Studio's plugin manager to add a **Speech to Text** entry to Tools. The page accepts an audio file and the model/language parameters defined by the Operation. Results contain recognized text and timestamps when the backend provides them.

Apple Silicon uses funasr-mlx when available. Other supported environments use FunASR on CUDA or CPU. Model availability and language support depend on the selected backend and checkpoint. Plugin installation does not download all model weights.

The plugin page manages its runtime component and model selection through the Studio SDK and exports TXT, JSON, or SRT when timestamps are available. The `AsrTranscribe` workflow node exposes text and backend outputs.

The MLX CTC presets use `funasr-mlx>=0.2.2` and `sentencepiece>=0.2`. Audio longer than 30 seconds is decoded in bounded chunks and the text is joined in input order; this path does not provide subtitle timestamps. Model and tokenizer downloads use Studio's selected model directory when the tool page supplies one.

On Apple Silicon, the page offers Chinese Paraformer and multilingual SenseVoice. The current `funasr-mlx` download catalog has no English Paraformer artifact; the Operation can use that preset only with a local converted MLX checkpoint. Use SenseVoice with English selected for automatic model installation.
