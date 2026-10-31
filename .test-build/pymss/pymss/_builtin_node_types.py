"""Names reserved by core before graph executors (and Torch) are imported.

Keep this catalog synchronized with graph.nodes and graph.builtin_nodes. The
plugin contract tests compare it with registrations from a fresh interpreter.
"""

_SEPARATOR_NODE_TYPES = frozenset({
    "mss_separate", "mss_separate_list",
    "vr_separate", "vr_separate_list",
    "custom_mss_separate", "custom_mss_separate_list",
})

BUILTIN_NODE_TYPES = frozenset({
    "input_audio",
    "pymss_load_audio", "pymss_load_audio_batch", "pymss_save_audio",
    "pymss_mss_params", "pymss_vr_params",
    "pymss_audio_invert_phase", "pymss_audio_normalize", "pymss_audio_ensemble",
    "PreviewAudio", "StringConcatenate", "LoadAudio",
    "SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveAudioAdvanced",
    "TrimAudioDuration", "SplitAudioChannels", "JoinAudioChannels",
    "AudioConcat", "AudioMerge", "AudioAdjustVolume", "EmptyAudio", "AudioEqualizer3Band",
    "StringSubstring", "StringReplace", "StringTrim", "CaseConverter", "StringFormat",
    "RegexReplace", "RegexExtract", "JsonExtractString", "StringConstant",
}) | _SEPARATOR_NODE_TYPES | frozenset(f"pymss_{name}" for name in _SEPARATOR_NODE_TYPES)
