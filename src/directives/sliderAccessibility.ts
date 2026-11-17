import type { Directive } from 'vue'

interface SliderAccessibility {
  label: string
  valueText: string
}

// Naive UI applies attributes to its root; the thumb owns keyboard focus.
export const vSliderA11y: Directive<HTMLElement, SliderAccessibility> = (element, { value }) => {
  const thumb = element.querySelector('[role="slider"]')
  thumb?.setAttribute('aria-label', value.label)
  thumb?.setAttribute('aria-valuetext', value.valueText)
}
