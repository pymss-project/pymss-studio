import assert from 'node:assert/strict'
import test from 'node:test'
import { schemaDefaults, supportsOperationSchema, validateParameters } from '../../frontend/src/components/operationSchema.ts'
const schema = { required: ['enabled', 'mode'], properties: { enabled: { type: 'boolean', default: false }, mode: { type: 'integer', enum: [0, 2], default: 0 }, gain: { type: 'number', minimum: -2, maximum: 2, default: 0 } } }
test('schema defaults preserve false and numeric enum values', () => assert.deepEqual(schemaDefaults(schema), { enabled: false, mode: 0, gain: 0 }))
test('schema validation rejects only invalid parameter boundaries', () => { assert.deepEqual(validateParameters(schema, { enabled: false, mode: 2, gain: 1 }), {}); assert.deepEqual(validateParameters(schema, { enabled: false, mode: 1, gain: 3 }), { mode: 'enum', gain: 'maximum' }) })
test('schema rejects unsupported types and invalid string or boolean values', () => { assert.equal(supportsOperationSchema({ properties: { source: { type: 'array' } } }), false); assert.deepEqual(validateParameters({ required: ['name'], properties: { name: { type: 'string' }, enabled: { type: 'boolean' } } }, { name: 1, enabled: 'false' }), { name: 'string', enabled: 'boolean' }) })

test('malformed parameter metadata is rejected before controls are rendered', () => {
  assert.equal(supportsOperationSchema({ properties: { broken: null } }), false)
  assert.equal(supportsOperationSchema({ properties: { missing: {} } }), false)
  assert.equal(supportsOperationSchema({ properties: [] }), false)
})
