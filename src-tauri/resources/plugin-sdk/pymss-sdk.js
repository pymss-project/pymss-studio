/* pymss plugin SDK script.
 * Bridges a sandboxed plugin view page to pymss Studio over JSON-RPC
 * (postMessage), and applies host design tokens to this document so plugin
 * pages visually match the app (dark/light, theme switches) with zero CSS
 * shipping on the plugin side.
 *
 * Usage:
 *   <link rel="stylesheet" href="pymss-plugin://sdk/pymss-sdk.css">
 *   <script src="pymss-plugin://sdk/pymss-sdk.js"></script>
 *   const result = await pymss.call('operations.start', {...})
 */
(function () {
  'use strict'

  var pending = new Map() // id -> {resolve, reject, onProgress}
  var nextId = 1
  var readyResolvers = []
  var hostReady = false

  function post(message) { window.parent.postMessage(message, '*') }

  window.addEventListener('message', function (event) {
    var data = event.data
    if (!data || typeof data !== 'object') return

    // Host pushes theme tokens; apply them to :root immediately.
    if (data.method === 'host.theme' && data.params) { applyTheme(data.params); return }
    if (data.method === 'host.ready') {
      hostReady = true
      var queue = readyResolvers; readyResolvers = []
      queue.forEach(function (fn) { try { fn() } catch (e) { /* plugin cb */ } })
      return
    }

    // RPC responses
    if (data.jsonrpc !== '2.0' || data.id === undefined) return
    var entry = pending.get(data.id)
    if (!entry) return
    pending.delete(data.id)
    if (data.error) entry.reject(new Error(String(data.error.message || 'RPC error')))
    else entry.resolve(data.result)
  })

  function applyTheme(params) {
    var root = document.documentElement
    var vars = params.cssVariables || {}
    Object.keys(vars).forEach(function (key) {
      if (/^--[a-z0-9-]+$/i.test(key)) root.style.setProperty(key, vars[key])
    })
    if (params.colorMode) root.dataset.mode = params.colorMode
    if (params.fontFamily) root.style.setProperty('--font-sans', params.fontFamily)
    document.dispatchEvent(new CustomEvent('pymss:theme', { detail: params }))
  }

  function call(method, params, options) {
    return new Promise(function (resolve, reject) {
      var id = nextId++
      pending.set(id, { resolve: resolve, reject: reject, onProgress: options && options.onProgress })
      post({ jsonrpc: '2.0', id: id, method: method, params: params || {} })
    })
  }

  // Handshake: tell the host this page is alive; it replies host.ready (and
  // typically follows with host.theme).
  post({ jsonrpc: '2.0', method: 'pymss.ready' })

  window.pymss = {
    call: call,
    ready: function () {
      return new Promise(function (resolve) {
        if (hostReady) resolve()
        else readyResolvers.push(resolve)
      })
    },
    onTheme: function (handler) {
      document.addEventListener('pymss:theme', function (event) { handler(event.detail) })
    },
    // Convenience wrappers over the SDK surface
    appInfo: function () { return call('app.info') },
    describeOperation: function (operation) { return call('operations.describe', { operation: operation }) },
    startOperation: function (operation, params, inputs, onProgress) {
      return call('operations.start', { operation: operation, params: params || {}, inputs: inputs || {} }, { onProgress: onProgress })
    },
    pickAudio: function () { return call('files.pickAudio', {}) },
  }
})()
