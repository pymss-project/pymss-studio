import { watch, type DirectiveBinding, type ObjectDirective } from 'vue'
import { useI18n } from 'vue-i18n'

export function useNumberInputAccessibility(): ObjectDirective<HTMLElement, string> {
  const { t, locale } = useI18n()
  const controls = new Map<HTMLElement, string>()

  function labelControl(element: HTMLElement, label: string) {
    element.querySelector('input:not([type="hidden"])')?.setAttribute('aria-label', label)
    const buttons = element.querySelectorAll('button')
    // Naive UI renders the decrement button before the increment button.
    if (buttons.length === 2) {
      buttons[0]?.setAttribute('aria-label', t('common.decreaseValue', { parameter: label }))
      buttons[1]?.setAttribute('aria-label', t('common.increaseValue', { parameter: label }))
    }
  }

  watch(locale, () => {
    controls.forEach((label, element) => labelControl(element, label))
  }, { flush: 'post' })

  function update(element: HTMLElement, { value }: DirectiveBinding<string>) {
    controls.set(element, value)
    labelControl(element, value)
  }

  return {
    mounted: update,
    updated: update,
    unmounted: element => { controls.delete(element) },
  }
}
