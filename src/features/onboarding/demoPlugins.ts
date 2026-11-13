import { pluginSdkVersion } from '../plugin-marketplace/sdkContract.ts'

export function createDemoPlugin(locale: string, prefix: string) {
  const english = locale === 'en'
  const title = english ? 'Audio format conversion' : '音频格式转换'
  return {
    name: 'pymss.convert', version: '0.2.1', source: 'preinstalled', managed: true, loaded: true,
    enabled: true, prepared: true, availability: 'ready' as const, preinstalled: true,
    url: 'https://github.com/pymss-project/pymss-plugins', path: `${prefix}/Plugins/pymss.convert`,
    description: english ? 'Convert audio formats and adjust sample rates.' : '转换音频格式，调整采样率。',
    error: null, operations: ['audio.convert'], toolCategory: 'convert',
    locales: { en: { title: 'Audio format conversion' }, 'zh-CN': { title: '音频格式转换' } },
    contributions: { views: [{ id: 'tool', title, entry: 'index.html', placements: ['tool', 'workspace'],
      permissions: [], sdkVersion: pluginSdkVersion, requiredCapabilities: [], category: 'convert' }] },
  }
}

/** A sandboxed, session-only tool page. File selection and conversion never leave this document. */
export function demoToolPage(locale: string, input: string, output: string): string {
  const english = locale === 'en'
  const text = english ? {
    title: 'Audio format conversion', input: 'Input audio', choose: 'Select audio or drop files here',
    format: 'Output format', start: 'Start conversion', ready: 'Select an audio file to continue.',
    done: 'Conversion completed', output: 'Output directory',
  } : {
    title: '音频格式转换', input: '输入音频', choose: '选择音频或将文件拖入此处',
    format: '输出格式', start: '开始转换', ready: '请选择音频文件。', done: '转换完成', output: '输出目录',
  }
  return `<!doctype html><html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}body{margin:0;padding:24px;font:14px/1.6 system-ui,sans-serif;color:var(--on-surface,#172236);background:var(--surface-1,#fafbfe)}
h2{margin:0 0 20px;font-size:22px}label{display:block;margin:16px 0 6px;font-weight:600}
button,select{font:inherit;border:1px solid var(--outline,#ccd5df);border-radius:8px;padding:8px 12px;color:inherit;background:var(--surface-2,#edf2f7)}
.input{width:100%;min-height:72px;border-style:dashed;cursor:pointer}.actions{display:flex;justify-content:flex-end;margin-top:24px}
.start{background:var(--primary,#208c80);color:white;border-color:transparent;cursor:pointer}button:disabled{opacity:.5;cursor:default}
button:focus-visible,select:focus-visible{outline:2px solid var(--primary,#208c80);outline-offset:2px}
.path{overflow-wrap:anywhere;color:var(--on-surface-muted,#637087)}p{margin:8px 0}
</style><h2>${text.title}</h2><label>${text.input}</label><button class="input" id="choose">${text.choose}</button>
<label for="format">${text.format}</label><select id="format"><option>WAV</option><option>FLAC</option><option>MP3</option></select>
<label>${text.output}</label><p class="path" id="output"></p><p id="status" role="status">${text.ready}</p>
<div class="actions"><button class="start" id="start" disabled>${text.start}</button></div>
<script>
const input=${JSON.stringify(input)},output=${JSON.stringify(output)},done=${JSON.stringify(text.done)};
document.getElementById('output').textContent=output;
const select=()=>{document.getElementById('choose').textContent=input;document.getElementById('start').disabled=false;document.getElementById('status').textContent='';};
document.getElementById('choose').onclick=select;
window.ondragover=event=>event.preventDefault();window.ondrop=event=>{event.preventDefault();select();};
document.getElementById('start').onclick=()=>{document.getElementById('status').textContent=done;};
window.addEventListener('message',event=>{if(event.source!==parent||event.data?.method!=='host.theme')return;
for(const [key,value]of Object.entries(event.data.params.cssVariables||{}))document.documentElement.style.setProperty(key,String(value));
document.documentElement.style.colorScheme=event.data.params.colorMode;});
parent.postMessage({jsonrpc:'2.0',method:'pymss.ready'},'*');
parent.postMessage({jsonrpc:'2.0',method:'pymss.resize',params:{height:440}},'*');
</script></html>`
}
