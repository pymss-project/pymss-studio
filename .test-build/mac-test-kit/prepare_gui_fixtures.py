"""Create private, reproducible inputs and a portable workflow for manual acceptance."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit', type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    kit = args.kit.resolve()
    build = json.loads((kit / 'build-info.json').read_text(encoding='utf-8'))
    work = kit / ('gui-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    work.mkdir(exist_ok=False)
    for name in ['inputs', 'expected', 'screenshots', 'outputs/python', 'outputs/rust', 'outputs/workflow', 'outputs/audio']:
        (work / name).mkdir(parents=True, exist_ok=True)
    source = work / 'inputs/中文 input.txt'
    source.write_bytes('第一行\r\nsecond\rthird\n'.encode())
    (work / 'inputs/invalid-utf8.txt').write_bytes(b'\xff\xfe\xfa')
    expected = '第一行\nsecond\nthird\n'.encode()
    (work / 'expected/LF.txt').write_bytes(expected)
    (work / 'expected/CRLF.txt').write_bytes(expected.replace(b'\n', b'\r\n'))
    block = (b'0123456789abcdef\n' * 61681)[:1024 * 1024]
    with (work / 'inputs/cancel-64MiB.txt').open('xb') as stream:
        for _ in range(64):
            stream.write(block)
    frames = bytearray()
    for index in range(4800):
        for amplitude, phase in [(0.1, 0), (0.08, 0.5)]:
            value = round(amplitude * math.sin(2 * math.pi * 440 * index / 48000 + phase) * (2**23 - 1))
            frames.extend(value.to_bytes(3, 'little', signed=True))
    with wave.open(str(work / 'inputs/stereo-48k-12s.wav'), 'wb') as audio:
        audio.setnchannels(2)
        audio.setsampwidth(3)
        audio.setframerate(48000)
        audio.writeframes(bytes(frames) * 120)
    definition = {'version': 0.4, 'last_node_id': 5, 'last_link_id': 4, 'nodes': [
        {'id': 1, 'type': 'StringConstant', 'pos': [40, 40], 'widgets_values': [str(source)]},
        {'id': 2, 'type': 'StringConstant', 'pos': [40, 250], 'widgets_values': ['lf']},
        {'id': 3, 'type': 'StringConstant', 'pos': [40, 440], 'widgets_values': ['crlf']},
        {'id': 4, 'type': 'example_python_rpc_text_normalize', 'pos': [620, 100], 'inputs': [
            {'name': 'source', 'type': 'STRING', 'link': 1}, {'name': 'line_ending', 'type': 'STRING', 'link': 2}]},
        {'id': 5, 'type': 'example_rust_rpc_text_normalize', 'pos': [620, 430], 'inputs': [
            {'name': 'source', 'type': 'STRING', 'link': 3}, {'name': 'line_ending', 'type': 'STRING', 'link': 4}]},
    ], 'links': [[1, 1, 0, 4, 0, 'STRING'], [2, 2, 0, 4, 1, 'STRING'],
        [3, 1, 0, 5, 0, 'STRING'], [4, 3, 0, 5, 1, 'STRING']]}
    (work / 'RPC-Text-Workflow.json').write_text(json.dumps(definition, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (work / 'workspace.json').write_text(json.dumps({'formatVersion': 1, 'build': build, 'workspace': str(work)}, indent=2) + '\n', encoding='utf-8')
    paths = ['本轮测试路径（移动工具包后请重新生成）', '']
    for label, path in [
        ('Python 插件安装来源', kit / 'plugins/python_rpc_text'), ('Rust 插件安装来源', kit / 'plugins/rust_rpc_text'),
        ('音频分析插件安装来源', kit / 'plugins/analysis'), ('UTF-8 输入', source),
        ('非法 UTF-8 输入', work / 'inputs/invalid-utf8.txt'), ('取消操作输入', work / 'inputs/cancel-64MiB.txt'),
        ('测试音频', work / 'inputs/stereo-48k-12s.wav'), ('Python 输出目录', work / 'outputs/python'),
        ('Rust 输出目录', work / 'outputs/rust'), ('工作流输出目录', work / 'outputs/workflow'),
        ('音频输出目录', work / 'outputs/audio'), ('工作流导入文件', work / 'RPC-Text-Workflow.json'),
    ]:
        paths.extend([label + '：', str(path), ''])
    (work / 'GUI-PATHS.txt').write_text('\n'.join(paths), encoding='utf-8')
    (work / 'MANUAL-RESULTS.md').write_text('''# 界面验收记录

请将未执行、通过、失败分别填写，并在 screenshots 中保存对应截图。

| 项目 | 结果 | 说明／截图 |
| --- | --- | --- |
| 首次启动四个基础工具 | 未执行 | |
| Python 插件通过 GUI 从来源安装 | 未执行 | |
| Rust 插件通过 GUI 从来源安装 | 未执行 | |
| 两种文本工具 LF／CRLF、错误恢复、不覆盖 | 未执行 | |
| 两种工具观察到运行态后取消，再次运行 | 未执行 | |
| 工作流库节点 5、输出 2 | 未执行 | |
| 独立节点编辑器运行后显示两份文件，无播放器 | 未执行 | |
| 开启动画切换深浅主题、返回工具、运行和日志 | 未执行 | |
| 关闭动画后同样操作 | 未执行 | |
| WAV → FLAC → WAV，12 秒／48 kHz／双声道 | 未执行 | |
| 音频分析／频谱、静音输入 | 未执行 | |
| 重启保留插件、设置和文件结果 | 未执行 | |
| GUI 后自动检查及最终应用签名 | 未执行 | |
''', encoding='utf-8')
    (kit / 'last-gui-workspace.txt').write_text(str(work) + '\n', encoding='utf-8')
    print('GUI workspace:', work)
    print('Read GUI-PATHS.txt and MANUAL-RESULTS.md in that folder.')


if __name__ == '__main__':
    main()
