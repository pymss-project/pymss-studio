/* pymss plugin SDK script.
 * Bridges a sandboxed plugin view page to pymss Studio over JSON-RPC
 * (postMessage), and applies host design tokens to this document so plugin
 * pages visually match the app (dark/light, theme switches) with zero CSS
 * shipping on the plugin side.
 *
 * Usage:
 *   <link rel="stylesheet" href="../../../sdk/pymss-sdk.css">
 *   <script src="../../../sdk/pymss-sdk.js"></script>
 *   const result = await pymss.call('operations.start', {...})
 */
(function () {
  'use strict'

  var pending = new Map() // id -> {resolve, reject, onProgress}
  var nextId = 1
  var readyResolvers = []
  var hostReady = false
  var hostSdkVersion = null
  var hostCapabilities = Object.freeze([])

  function post(message) { window.parent.postMessage(message, '*') }

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return
    var data = event.data
    if (!data || typeof data !== 'object') return

    // Host pushes theme tokens; apply them to :root immediately.
    if (data.method === 'host.theme' && data.params) { applyTheme(data.params); return }
    if (data.method === 'host.ready') {
      hostSdkVersion = data.params.sdkVersion
      hostCapabilities = Object.freeze(data.params.capabilities.slice())
      hostReady = true
      var queue = readyResolvers; readyResolvers = []
      queue.forEach(function (fn) { try { fn() } catch (e) { /* plugin cb */ } })
      return
    }

    // Progress retains the pending call until its terminal RPC response.
    if (data.jsonrpc === '2.0' && data.method === 'task.progress' && data.params) {
      var progressEntry = pending.get(data.params.id)
      if (progressEntry && typeof progressEntry.onProgress === 'function') {
        try { progressEntry.onProgress(data.params) } catch (e) { /* plugin cb */ }
      }
      return
    }

    // RPC responses
    if (data.jsonrpc !== '2.0' || data.id === undefined) return
    var entry = pending.get(data.id)
    if (!entry) return
    pending.delete(data.id)
    if (data.error) {
      var error = new Error(String(data.error.message || 'RPC error'))
      if (data.error.data && typeof data.error.data === 'object') error.data = data.error.data
      if (data.error.data && typeof data.error.data.code === 'string') error.code = data.error.data.code
      entry.reject(error)
    }
    else entry.resolve(data.result)
  })

  function applyTheme(params) {
    var root = document.documentElement
    var vars = params.cssVariables || {}
    Object.keys(vars).forEach(function (key) {
      if (/^--[a-z0-9-]+$/i.test(key)) root.style.setProperty(key, vars[key])
    })
    if (params.colorMode === 'light' || params.colorMode === 'dark') {
      root.dataset.mode = params.colorMode
      root.style.colorScheme = params.colorMode
    }
    if (params.fontFamily) root.style.setProperty('--font-sans', params.fontFamily)
    document.dispatchEvent(new CustomEvent('pymss:theme', { detail: params }))
  }

  function call(method, params, options) {
    var id = nextId++
    var promise = new Promise(function (resolve, reject) {
      pending.set(id, { resolve: resolve, reject: reject, onProgress: options && options.onProgress })
      post({ jsonrpc: '2.0', id: id, method: method, params: params || {} })
    })
    Object.defineProperty(promise, 'id', { value: id, enumerable: true })
    return promise
  }

  // Handshake: tell the host this page is alive; it replies host.ready (and
  // typically follows with host.theme).
  post({ jsonrpc: '2.0', method: 'pymss.ready' })

  window.pymss = {
    get sdkVersion() { return hostSdkVersion },
    get capabilities() { return hostCapabilities },
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
    describeOperation: function (operation, options) {
      var params = { operation: operation }
      if (options && options.refresh === true) params.refresh = true
      return call('operations.describe', params)
    },
    startOperation: function (operation, params, inputs, onProgress, options) {
      var payload = { operation: operation, params: params || {}, inputs: inputs || {} }
      if (options && typeof options.outputDir === 'string' && options.outputDir) payload.outputDir = options.outputDir
      if (options && options.expectedSpecDigest !== undefined) payload.expectedSpecDigest = options.expectedSpecDigest
      return call('operations.start', payload, { onProgress: onProgress })
    },
    cancelOperation: function (id) { return call('operations.cancel', { id: id }) },
    pickAudio: function (options) { return call('files.pickAudio', options || {}) },
    pickFile: function (options) { return call('files.pickFile', options || {}) },
    pickDirectory: function () { return call('files.pickDirectory', {}) },
    scanAudio: function (paths, recursive) {
      return call('files.scanAudio', { paths: paths || [], recursive: recursive !== false })
    },
    revealPath: function (path) { return call('files.reveal', { path: path }) },
    readSettings: function () { return call('settings.read', {}) },
    writeSettings: function (data) { return call('settings.write', { data: data }) },
    pickModel: function (options) { return call('files.pickModel', options || {}) },
    writeClipboardText: function (text) { return call('clipboard.writeText', { text: text }) },
    openExternal: function (url) { return call('shell.openExternal', { url: url }) },
    analyzeAudio: function (payload, onProgress) {
      return call('audio.analyze', { payload: payload || {} }, { onProgress: onProgress })
    },
    packageStatus: function () { return call('runtime.packageStatus', { package: 'funasr' }) },
    managePackage: function (payload, onProgress) {
      return call('runtime.managePackage', { payload: payload || {} }, { onProgress: onProgress })
    },
    deleteToolModel: function (payload) { return call('models.deleteTool', { payload: payload || {} }) },
  }
})()
