import {
  isSimpleAudioOperation,
  isSimpleProcessingAlgorithm,
  simpleProcessingInputLimits,
  type SimpleDraft,
  type SimpleEnsembleDraft,
  type SimpleStepDraft,
} from '@/utils/workflowSimple'

export type SimpleConnectionSource = 'input' | `${string}.${string}`
export type SimpleConnectionTarget =
  | `step:${string}`
  | `ensemble:${string}:${number}`
  | `save`
  | `save:${string}.${string}`

export type SimpleConnectionCheck =
  | { ok: true }
  | { ok: false; reason: 'missing-source' | 'missing-target' | 'invalid-source' | 'forward-link' | 'self-link' | 'duplicate-source' | 'invalid-save-target' }

export type SimpleDestructiveImpact = {
  connections: number
  savedOutputs: number
}

export function simpleStepInputTarget(stepId: string): `step:${string}` {
  return `step:${stepId}`
}

export function simpleSaveTarget(stepId: string, stem: string): `save:${string}.${string}` {
  return `save:${stepId}.${stem}`
}

export function simpleEnsembleInputTarget(ensembleId: string, index: number): `ensemble:${string}:${number}` {
  return `ensemble:${ensembleId}:${index}`
}

export function simpleOutputRef(stepId: string, stem: string): `${string}.${string}` {
  return `${stepId}.${stem}`
}

export function simpleSourceStepId(source: string): string {
  const separator = source.indexOf('.')
  return separator > 0 ? source.slice(0, separator) : ''
}

export function simpleSourceStem(source: string): string {
  const separator = source.indexOf('.')
  return separator > 0 ? source.slice(separator + 1) : ''
}

function countSourceReferences(draft: SimpleDraft, sources: Set<string>): number {
  if (!sources.size) return 0
  const matches = (source: string) => sources.has(source.trim().toLowerCase())
  return draft.steps.filter(step => matches(step.input)).length
    + draft.ensembles.reduce(
      (total, ensemble) => total + ensemble.inputs.filter(input => matches(input.source)).length,
      0,
    )
}

/** Describe collateral edits before a model change removes output stems. */
export function analyzeSimpleModelChangeImpact(
  draft: SimpleDraft,
  stepId: string,
  nextStems: string[],
): SimpleDestructiveImpact {
  const step = draft.steps.find(item => item.id === stepId)
  if (!step) return { connections: 0, savedOutputs: 0 }
  const retained = new Set(nextStems.map(stem => stem.trim().toLowerCase()))
  const removedStems = step.stems.filter(stem => !retained.has(stem.trim().toLowerCase()))
  const sources = new Set(removedStems.map(stem => simpleOutputRef(step.id, stem).toLowerCase()))
  const removed = new Set(removedStems.map(stem => stem.trim().toLowerCase()))
  return {
    connections: countSourceReferences(draft, sources),
    savedOutputs: Object.keys(step.save || {}).filter(stem => removed.has(stem.trim().toLowerCase())).length,
  }
}

/** Describe downstream links and selected outputs removed with a node. */
export function analyzeSimpleNodeRemovalImpact(
  draft: SimpleDraft,
  nodeId: string,
): SimpleDestructiveImpact {
  const step = draft.steps.find(item => item.id === nodeId)
  if (step) {
    const sources = new Set(step.stems.map(stem => simpleOutputRef(step.id, stem).toLowerCase()))
    return {
      connections: countSourceReferences(draft, sources),
      savedOutputs: Object.keys(step.save || {}).length,
    }
  }
  const ensemble = draft.ensembles.find(item => item.id === nodeId)
  if (!ensemble) return { connections: 0, savedOutputs: 0 }
  const stem = ensemble.outputStem.trim()
  const sources = new Set(stem ? [simpleOutputRef(ensemble.id, stem).toLowerCase()] : [])
  return {
    connections: countSourceReferences(draft, sources),
    savedOutputs: ensemble.save ? 1 : 0,
  }
}

export function updateSimpleEnsembleOutputStem(
  draft: SimpleDraft,
  ensemble: SimpleEnsembleDraft,
  value: string,
): void {
  const previousStem = ensemble.outputStem.trim()
  const nextStem = value.trim()
  ensemble.outputStem = value
  if (!nextStem) return

  const previousSource = previousStem ? simpleOutputRef(ensemble.id, previousStem) : ''
  const nextSource = simpleOutputRef(ensemble.id, nextStem)
  const referencesPreviousOutput = (source: string) => previousSource
    ? source.trim().toLowerCase() === previousSource.toLowerCase()
    : simpleSourceStepId(source.trim()).toLowerCase() === ensemble.id.toLowerCase()
  draft.steps.forEach((step) => {
    if (referencesPreviousOutput(step.input)) {
      step.input = nextSource
    }
  })
  draft.ensembles.forEach((target) => {
    if (target.id === ensemble.id) return
    target.inputs = target.inputs.map(input => referencesPreviousOutput(input.source)
      ? { ...input, source: nextSource }
      : input)
  })
}

type ResolvedSimpleSource =
  | { kind: 'step'; step: SimpleStepDraft; stem: string }
  | { kind: 'ensemble'; ensemble: SimpleEnsembleDraft; stem: string }

function resolveSource(draft: SimpleDraft, source: string): ResolvedSimpleSource | null {
  if (source.trim().toLowerCase() === 'input') return null
  const sourceId = simpleSourceStepId(source)
  const stem = simpleSourceStem(source)
  const sourceIdKey = sourceId.toLowerCase()
  const step = draft.steps.find(item => item.id.toLowerCase() === sourceIdKey)
  const stepStem = step?.stems.find(item => item.toLowerCase() === stem.toLowerCase())
  if (step && stepStem) {
    return { kind: 'step', step, stem: stepStem }
  }
  const ensemble = draft.ensembles.find(item => item.id.toLowerCase() === sourceIdKey)
  if (ensemble && stem && ensemble.outputStem.trim().toLowerCase() === stem.toLowerCase()) {
    return { kind: 'ensemble', ensemble, stem: ensemble.outputStem.trim() }
  }
  return null
}

function normalizeSimpleSource(draft: SimpleDraft, source: string): string {
  const trimmed = source.trim()
  if (trimmed.toLowerCase() === 'input') return 'input'
  const resolved = resolveSource(draft, trimmed)
  if (resolved?.kind === 'step') return simpleOutputRef(resolved.step.id, resolved.stem)
  if (resolved?.kind === 'ensemble') return simpleOutputRef(resolved.ensemble.id, resolved.stem)
  return trimmed
}

function ensembleTarget(draft: SimpleDraft, target: string) {
  const match = /^ensemble:(.+):(\d+)$/.exec(target)
  if (!match) return null
  const ensembleIndex = draft.ensembles.findIndex(item => item.id === match[1])
  const ensemble = draft.ensembles[ensembleIndex]
  const index = Number(match[2])
  return ensemble && Number.isInteger(index) && index >= 0 && index < ensemble.inputs.length
    ? { ensemble, ensembleIndex, index }
    : null
}

function ensembleStepDependencies(
  draft: SimpleDraft,
  ensemble: SimpleEnsembleDraft,
  visiting = new Set<string>(),
): SimpleStepDraft[] | null {
  const key = ensemble.id.toLowerCase()
  if (visiting.has(key)) return null
  const nextVisiting = new Set(visiting).add(key)
  const dependencies: SimpleStepDraft[] = []
  for (const input of ensemble.inputs) {
    if (input.source.trim().toLowerCase() === 'input') continue
    const source = resolveSource(draft, input.source.trim())
    if (!source) return null
    if (source.kind === 'step') {
      dependencies.push(source.step)
      continue
    }
    const nested = ensembleStepDependencies(draft, source.ensemble, nextVisiting)
    if (!nested) return null
    dependencies.push(...nested)
  }
  return dependencies
}

function simpleNodeKeyForSource(draft: SimpleDraft, source: string): string | null {
  const resolved = resolveSource(draft, source.trim())
  return resolved ? `${resolved.kind}:${resolved.kind === 'step' ? resolved.step.id : resolved.ensemble.id}` : null
}

function wouldCreateSimpleCycle(
  draft: SimpleDraft,
  source: string,
  targetKey: string,
  replacedTarget: SimpleConnectionTarget,
): boolean {
  const sourceKey = simpleNodeKeyForSource(draft, source)
  if (!sourceKey) return false
  if (sourceKey.toLowerCase() === targetKey.toLowerCase()) return true
  const edges = new Map<string, Set<string>>()
  const addEdge = (input: string, target: string) => {
    if (!input.trim() || input.trim() === 'input') return
    const inputKey = simpleNodeKeyForSource(draft, input)
    if (!inputKey) return
    const targets = edges.get(inputKey) || new Set<string>()
    targets.add(target)
    edges.set(inputKey, targets)
  }
  draft.steps.forEach((step) => {
    if (replacedTarget === simpleStepInputTarget(step.id)) return
    addEdge(step.input, `step:${step.id}`)
  })
  draft.ensembles.forEach((ensemble) => {
    ensemble.inputs.forEach((input, index) => {
      if (replacedTarget === simpleEnsembleInputTarget(ensemble.id, index)) return
      addEdge(input.source, `ensemble:${ensemble.id}`)
    })
  })
  const pending = [targetKey]
  const visited = new Set<string>()
  while (pending.length) {
    const current = pending.pop()!
    if (current.toLowerCase() === sourceKey.toLowerCase()) return true
    if (visited.has(current)) continue
    visited.add(current)
    pending.push(...(edges.get(current) || []))
  }
  return false
}

export function canConnectSimple(
  draft: SimpleDraft,
  source: string,
  target: SimpleConnectionTarget,
): SimpleConnectionCheck {
  const rawSource = source.trim()
  if (!rawSource) return { ok: false, reason: 'missing-source' }
  const normalizedSource = normalizeSimpleSource(draft, rawSource)
  const isInputSource = normalizedSource === 'input'
  const sourceValue = isInputSource ? null : resolveSource(draft, normalizedSource)
  if (!isInputSource && !sourceValue) return { ok: false, reason: 'invalid-source' }

  if (target === 'step:') return { ok: false, reason: 'missing-target' }
  if (target.startsWith('step:')) {
    const targetId = target.slice('step:'.length)
    const targetIndex = draft.steps.findIndex(step => step.id === targetId)
    if (targetIndex < 0) return { ok: false, reason: 'missing-target' }
    if (isInputSource) return { ok: true }
    if (sourceValue?.kind === 'ensemble') {
      const dependencies = ensembleStepDependencies(draft, sourceValue.ensemble)
      if (!dependencies) return { ok: false, reason: 'invalid-source' }
      for (const dependency of dependencies) {
        const dependencyIndex = draft.steps.findIndex(step => step.id === dependency.id)
        if (dependencyIndex < 0) return { ok: false, reason: 'invalid-source' }
        if (dependencyIndex >= targetIndex) return { ok: false, reason: 'forward-link' }
      }
      if (wouldCreateSimpleCycle(draft, normalizedSource, `step:${targetId}`, target)) return { ok: false, reason: 'forward-link' }
      return { ok: true }
    }
    if (sourceValue?.kind !== 'step') return { ok: false, reason: 'invalid-source' }
    const sourceId = sourceValue.step.id
    const sourceIndex = draft.steps.findIndex(step => step.id === sourceId)
    if (sourceIndex < 0) return { ok: false, reason: 'invalid-source' }
    if (sourceId.toLowerCase() === targetId.toLowerCase()) return { ok: false, reason: 'self-link' }
    if (sourceIndex >= targetIndex) return { ok: false, reason: 'forward-link' }
    if (wouldCreateSimpleCycle(draft, normalizedSource, `step:${targetId}`, target)) return { ok: false, reason: 'forward-link' }
    return { ok: true }
  }

  if (target.startsWith('ensemble:')) {
    const resolvedTarget = ensembleTarget(draft, target)
    if (!resolvedTarget) return { ok: false, reason: 'missing-target' }
    if (!isInputSource) {
      if (!sourceValue) return { ok: false, reason: 'invalid-source' }
      if (sourceValue.kind === 'ensemble') {
        const sourceIndex = draft.ensembles.findIndex(item => item.id === sourceValue.ensemble.id)
        if (sourceIndex === resolvedTarget.ensembleIndex) return { ok: false, reason: 'self-link' }
        if (sourceIndex < 0 || sourceIndex >= resolvedTarget.ensembleIndex) return { ok: false, reason: 'forward-link' }
      }
      if (wouldCreateSimpleCycle(
        draft,
        normalizedSource,
        `ensemble:${resolvedTarget.ensemble.id}`,
        target,
      )) return { ok: false, reason: 'forward-link' }
    }
    if (resolvedTarget.ensemble.inputs.some((input, index) => index !== resolvedTarget.index && normalizeSimpleSource(draft, input.source).toLowerCase() === normalizedSource.toLowerCase())) {
      return { ok: false, reason: 'duplicate-source' }
    }
    return { ok: true }
  }

  if (target === 'save') {
    if (isInputSource) return { ok: false, reason: 'invalid-save-target' }
    if (!sourceValue) return { ok: false, reason: 'invalid-save-target' }
    return { ok: true }
  }
  if (!target.startsWith('save:')) return { ok: false, reason: 'missing-target' }
  if (isInputSource) return { ok: false, reason: 'invalid-save-target' }
  const value = target.slice('save:'.length)
  if (!sourceValue || value.toLowerCase() !== normalizedSource.toLowerCase()) {
    return { ok: false, reason: 'invalid-save-target' }
  }
  return { ok: true }
}

export function canMoveSimpleStep(draft: SimpleDraft, stepId: string, offset: -1 | 1): boolean {
  const index = draft.steps.findIndex(step => step.id === stepId)
  const targetIndex = index + offset
  if (index < 0 || targetIndex < 0 || targetIndex >= draft.steps.length) return false
  const steps = [...draft.steps]
  ;[steps[index], steps[targetIndex]] = [steps[targetIndex], steps[index]]
  const candidate = { ...draft, steps }
  return steps.every((step) => (
    !step.input.trim()
    || canConnectSimple(candidate, step.input, simpleStepInputTarget(step.id)).ok
  ))
}

export function moveSimpleStep(draft: SimpleDraft, stepId: string, offset: -1 | 1): boolean {
  if (!canMoveSimpleStep(draft, stepId, offset)) return false
  const index = draft.steps.findIndex(step => step.id === stepId)
  const targetIndex = index + offset
  const steps = [...draft.steps]
  ;[steps[index], steps[targetIndex]] = [steps[targetIndex], steps[index]]
  draft.steps = steps
  return true
}

export function connectSimple(
  draft: SimpleDraft,
  source: string,
  target: SimpleConnectionTarget,
): SimpleConnectionCheck {
  const check = canConnectSimple(draft, source, target)
  if (!check.ok) return check
  const normalizedSource = normalizeSimpleSource(draft, source)
  if (target.startsWith('step:')) {
    const step = draft.steps.find(item => item.id === target.slice('step:'.length))
    if (step) step.input = normalizedSource
    return check
  }
  if (target.startsWith('ensemble:')) {
    const resolvedTarget = ensembleTarget(draft, target)
    if (resolvedTarget) resolvedTarget.ensemble.inputs[resolvedTarget.index].source = normalizedSource
    return check
  }
  const value = target === 'save' ? normalizedSource : target.slice('save:'.length)
  const sourceId = simpleSourceStepId(value)
  const stem = simpleSourceStem(value)
  const step = draft.steps.find(item => item.id === sourceId)
  if (step) {
    step.save = { ...step.save, [stem]: step.save[stem] || 'Default' }
    step.outputNames = { ...step.outputNames, [stem]: step.outputNames[stem] || '%filename%_%stem%_%model%' }
    return check
  }
  const ensemble = draft.ensembles.find(item => item.id === sourceId)
  if (ensemble && ensemble.outputStem.trim().toLowerCase() === stem.toLowerCase()) {
    ensemble.save = true
    if (!ensemble.outputName.trim()) ensemble.outputName = isSimpleAudioOperation(ensemble.algorithm)
      ? '%filename%_%stem%_%step%' : '%filename%_%stem%_Ensemble'
  }
  return check
}

export function disconnectSimple(draft: SimpleDraft, target: SimpleConnectionTarget): boolean {
  if (target.startsWith('step:')) {
    const step = draft.steps.find(item => item.id === target.slice('step:'.length))
    if (!step) return false
    step.input = ''
    return true
  }
  if (target.startsWith('ensemble:')) {
    const resolvedTarget = ensembleTarget(draft, target)
    if (!resolvedTarget) return false
    resolvedTarget.ensemble.inputs[resolvedTarget.index].source = ''
    return true
  }
  if (!target.startsWith('save:')) return false
  const value = target.slice('save:'.length)
  const sourceId = simpleSourceStepId(value)
  const step = draft.steps.find(item => item.id === sourceId)
  const stem = simpleSourceStem(value)
  if (step && stem && stem in step.save) {
    const nextSave = { ...step.save }
    delete nextSave[stem]
    step.save = nextSave
    return true
  }
  const ensemble = draft.ensembles.find(item => item.id === sourceId)
  if (!ensemble || !ensemble.save || ensemble.outputStem.trim().toLowerCase() !== stem.toLowerCase()) return false
  ensemble.save = false
  return true
}

export function cleanupSimpleDraft(draft: SimpleDraft): void {
  if (!Array.isArray(draft.ensembles)) draft.ensembles = []
  draft.ensembles.forEach((ensemble, ensembleIndex) => {
    if (!isSimpleProcessingAlgorithm(ensemble.algorithm)) ensemble.algorithm = 'avg_wave'
    const limits = simpleProcessingInputLimits(ensemble.algorithm)
    ensemble.outputStem = ensemble.outputStem.trim()
    ensemble.outputName = ensemble.outputName.trim() || (isSimpleAudioOperation(ensemble.algorithm)
      ? '%filename%_%stem%_%step%' : '%filename%_%stem%_Ensemble')
    const seen = new Set<string>()
    ensemble.inputs = ensemble.inputs.slice(0, limits.max).map((input) => {
      const source = input.source.trim()
      const resolved = resolveSource(draft, source)
      const normalizedSource = normalizeSimpleSource(draft, source)
      const sourceKey = normalizedSource.toLowerCase()
      const sourceEnsembleIndex = resolved?.kind === 'ensemble'
        ? draft.ensembles.findIndex(item => item.id === resolved.ensemble.id)
        : -1
      const validSource = normalizedSource === 'input'
        || resolved?.kind === 'step'
        || (resolved?.kind === 'ensemble' && sourceEnsembleIndex >= 0 && sourceEnsembleIndex < ensembleIndex)
      const valid = validSource && !seen.has(sourceKey)
      if (valid) seen.add(sourceKey)
      return {
        source: valid ? normalizedSource : '',
        weight: !isSimpleAudioOperation(ensemble.algorithm) && Number.isFinite(input.weight) && input.weight > 0 ? input.weight : 1,
      }
    })
    while (ensemble.inputs.length < limits.min) ensemble.inputs.push({ source: '', weight: 1 })
  })
  draft.steps.forEach((step) => {
    const input = normalizeSimpleSource(draft, step.input)
    step.input = input
    const sourceId = simpleSourceStepId(input)
    const hasPendingEnsembleSource = draft.ensembles.some(ensemble => (
      !ensemble.outputStem
      && ensemble.id.toLowerCase() === sourceId.toLowerCase()
    ))
    if (input !== 'input'
      && !hasPendingEnsembleSource
      && !canConnectSimple(draft, input, simpleStepInputTarget(step.id)).ok) {
      step.input = ''
    }
    const saveByStem = new Map(Object.entries(step.save || {}).map(([stem, value]) => [stem.toLowerCase(), value]))
    const nextSave: Record<string, string> = {}
    step.stems.forEach((stem) => {
      const value = saveByStem.get(stem.toLowerCase())
      if (value?.trim()) nextSave[stem] = value
    })
    step.save = nextSave
    const namesByStem = new Map(Object.entries(step.outputNames || {}).map(([stem, value]) => [stem.toLowerCase(), value]))
    const nextNames: Record<string, string> = {}
    step.stems.forEach((stem) => {
      const value = namesByStem.get(stem.toLowerCase())
      if (value?.trim()) nextNames[stem] = value
      else if (Object.prototype.hasOwnProperty.call(nextSave, stem)) nextNames[stem] = '%filename%_%stem%_%model%'
    })
    step.outputNames = nextNames
  })
}
