export type CategoryLikeModel = {
  category?: string
  categoryCn?: string
  primaryCategory?: string
  primaryCategoryCn?: string
  secondaryCategory?: string
  secondaryCategoryCn?: string
}

export type ModelCategoryOption = { label: string; value: string; children?: ModelCategoryOption[] }

// Presentation order only: the catalog remains the source of category membership.
const categoryOrder = ['vocal', 'instrumental', 'karaoke', 'music_stems', 'single_instrument',
  'reverb_echo_control', 'cleanup', 'spatial', 'special', 'experimental_general', 'legacy_models', 'legacy_vr', 'user']
const text = (value?: string) => String(value || '').trim()
const localized = (id: string, cn: string, locale: string) => locale === 'zh-CN' ? cn || id : id || cn

function categoryIds(model: CategoryLikeModel) {
  const [root = '', ...rest] = text(model.category).split('/')
  const primary = text(model.primaryCategory) || root
  return { primary, secondary: text(model.secondaryCategory) || (root === primary ? rest.join('/') : '') }
}

export function getModelCategoryParts(model: CategoryLikeModel | null | undefined, locale: string): string[] {
  if (!model) return []
  const { primary, secondary } = categoryIds(model)
  const legacyLabels = text(model.categoryCn).split(' / ')
  if (locale === 'zh-CN' && !model.primaryCategory && !model.secondaryCategory && legacyLabels.length === 1 && legacyLabels[0]) return legacyLabels
  const primaryCn = text(model.primaryCategoryCn) || (legacyLabels.length > 1 || !secondary ? legacyLabels[0] || '' : '')
  const secondaryCn = text(model.secondaryCategoryCn) || (legacyLabels.length > 1 ? legacyLabels.slice(1).join(' / ') : '')
  const parts = [localized(primary, primaryCn, locale)]
  const duplicate = (secondary && secondary === primary) || (!secondary && secondaryCn === primaryCn)
  if (!duplicate) parts.push(localized(secondary, secondaryCn, locale))
  const result = [...new Set(parts.filter(Boolean))]
  if (result.length) return result
  const fallback = localized(text(model.category), text(model.categoryCn), locale)
  return fallback ? [fallback] : []
}

export function getModelCategoryLabel(model: CategoryLikeModel | null | undefined, locale: string, fallback = '—') {
  return getModelCategoryParts(model, locale).join(' › ') || fallback
}

export function matchesModelCategory(model: CategoryLikeModel, value: string) {
  const selected = text(value).toLowerCase()
  if (!selected) return true
  const { primary, secondary } = categoryIds(model)
  return [model.category, primary, secondary, [primary, secondary].filter(Boolean).join('/')]
    .some(candidate => text(candidate).toLowerCase() === selected)
}

export function buildModelCategoryOptionsFromModels(
  models: CategoryLikeModel[],
  locale: string,
  allLabel: string,
): ModelCategoryOption[] {
  const groups = new Map<string, { label: string; children: Map<string, string> }>()
  for (const model of models) {
    const { primary, secondary } = categoryIds(model)
    if (!primary) continue
    const legacyLabels = text(model.categoryCn).split(' / ')
    let group = groups.get(primary)
    if (!group) {
      const cn = text(model.primaryCategoryCn) || (legacyLabels.length > 1 || !secondary ? legacyLabels[0] || '' : '')
      const label = localized(primary, cn, locale)
        || getModelCategoryLabel(model, locale, primary)
      group = { label, children: new Map() }
      groups.set(primary, group)
    }
    if (secondary && secondary !== primary) {
      const value = `${primary}/${secondary}`
      if (matchesModelCategory(model, value)) {
        const cn = text(model.secondaryCategoryCn) || (legacyLabels.length > 1 ? legacyLabels.slice(1).join(' / ') : '')
        group.children.set(value, localized(secondary, cn, locale))
      }
    }
  }
  const collator = new Intl.Collator(locale === 'zh-CN' ? 'zh-CN' : 'en', { numeric: true })
  const ordered = [...groups].sort(([idA, a], [idB, b]) => {
    const rank = (id: string) => categoryOrder.includes(id) ? categoryOrder.indexOf(id) : categoryOrder.length
    return rank(idA) - rank(idB) || collator.compare(a.label, b.label) || idA.localeCompare(idB)
  })
  return [{ label: allLabel, value: '' }, ...ordered.map(([value, group]): ModelCategoryOption => {
    if (!group.children.size) return { label: group.label, value }
    const children = [...group.children].map(([value, label]) => ({ value, label }))
      .sort((a, b) => collator.compare(a.label, b.label) || a.value.localeCompare(b.value))
    return { value, label: group.label, children }
  })]
}

export function hasModelCategoryOption(options: ModelCategoryOption[], value: string): boolean {
  return options.some(option => option.value === value || (option.children && hasModelCategoryOption(option.children, value)))
}
