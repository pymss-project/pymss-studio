import assert from 'node:assert/strict'
import test from 'node:test'
import { APP_TITLE, documentTitle, routeNavKey } from '../src/utils/routeTitle.ts'

test('routes resolve to the navigation section that names them', () => {
  assert.equal(routeNavKey('separate'), 'separate')
  assert.equal(routeNavKey('settings'), 'settings')
  assert.equal(routeNavKey('workflow-node-editor'), 'workflows')
  assert.equal(routeNavKey('workflow-simple-editor'), 'workflows')
  assert.equal(routeNavKey('plugin-view'), 'plugins')
})

test('unnamed routes have no section title', () => {
  for (const name of [undefined, null, '', Symbol('route')]) assert.equal(routeNavKey(name), null)
})

test('the document title leads with the page so screen readers announce it first', () => {
  assert.equal(documentTitle('模型'), `模型 - ${APP_TITLE}`)
  assert.equal(documentTitle(''), APP_TITLE)
})
