export type AsrPresetId =
  | 'paraformer-zh'
  | 'paraformer-en'
  | 'sensevoice-small'
  | 'fun-asr-nano'
  | 'fun-asr-mlt-nano'

export type AsrLanguageCode =
  | 'auto' | 'zh' | 'en' | 'yue' | 'ja' | 'ko' | 'vi' | 'id' | 'th' | 'ms'
  | 'fil' | 'ar' | 'hi' | 'bg' | 'hr' | 'cs' | 'da' | 'nl' | 'et' | 'fi'
  | 'el' | 'hu' | 'ga' | 'lv' | 'lt' | 'mt' | 'pl' | 'pt' | 'ro' | 'sk'
  | 'sl' | 'sv'

export type AsrPreset = {
  id: AsrPresetId
  modelId: string
  titleKey: string
  descriptionKey: string
  scaleKey: string
  deviceKey: string
  capabilityKeys: string[]
  languages: AsrLanguageCode[]
  defaultLanguage: AsrLanguageCode
  supportsHotwords: boolean
  supportsTimestamps: boolean
}

export const ASR_LANGUAGE_KEYS: Record<AsrLanguageCode, string> = {
  auto: 'tools.asrLanguageAuto',
  zh: 'tools.asrLanguageChinese',
  en: 'tools.asrLanguageEnglish',
  yue: 'tools.asrLanguageCantonese',
  ja: 'tools.asrLanguageJapanese',
  ko: 'tools.asrLanguageKorean',
  vi: 'tools.asrLanguageVietnamese',
  id: 'tools.asrLanguageIndonesian',
  th: 'tools.asrLanguageThai',
  ms: 'tools.asrLanguageMalay',
  fil: 'tools.asrLanguageFilipino',
  ar: 'tools.asrLanguageArabic',
  hi: 'tools.asrLanguageHindi',
  bg: 'tools.asrLanguageBulgarian',
  hr: 'tools.asrLanguageCroatian',
  cs: 'tools.asrLanguageCzech',
  da: 'tools.asrLanguageDanish',
  nl: 'tools.asrLanguageDutch',
  et: 'tools.asrLanguageEstonian',
  fi: 'tools.asrLanguageFinnish',
  el: 'tools.asrLanguageGreek',
  hu: 'tools.asrLanguageHungarian',
  ga: 'tools.asrLanguageIrish',
  lv: 'tools.asrLanguageLatvian',
  lt: 'tools.asrLanguageLithuanian',
  mt: 'tools.asrLanguageMaltese',
  pl: 'tools.asrLanguagePolish',
  pt: 'tools.asrLanguagePortuguese',
  ro: 'tools.asrLanguageRomanian',
  sk: 'tools.asrLanguageSlovak',
  sl: 'tools.asrLanguageSlovenian',
  sv: 'tools.asrLanguageSwedish',
}

const MULTILINGUAL_LANGUAGES: AsrLanguageCode[] = [
  'auto', 'zh', 'en', 'yue', 'ja', 'ko', 'vi', 'id', 'th', 'ms', 'fil', 'ar',
  'hi', 'bg', 'hr', 'cs', 'da', 'nl', 'et', 'fi', 'el', 'hu', 'ga', 'lv', 'lt',
  'mt', 'pl', 'pt', 'ro', 'sk', 'sl', 'sv',
]

export const ASR_PRESETS: AsrPreset[] = [
  {
    id: 'paraformer-zh',
    modelId: 'paraformer-zh',
    titleKey: 'tools.asrPresetParaformerZh',
    descriptionKey: 'tools.asrPresetParaformerZhDescription',
    scaleKey: 'tools.asrScaleParaformer',
    deviceKey: 'tools.asrDeviceCpuGpu',
    capabilityKeys: ['tools.asrCapabilityTimestamps', 'tools.asrCapabilityPunctuation', 'tools.asrCapabilityHotwords'],
    languages: ['zh'],
    defaultLanguage: 'zh',
    supportsHotwords: true,
    supportsTimestamps: true,
  },
  {
    id: 'paraformer-en',
    modelId: 'paraformer-en',
    titleKey: 'tools.asrPresetParaformerEn',
    descriptionKey: 'tools.asrPresetParaformerEnDescription',
    scaleKey: 'tools.asrScaleParaformer',
    deviceKey: 'tools.asrDeviceCpuGpu',
    capabilityKeys: ['tools.asrCapabilityTimestamps', 'tools.asrCapabilityPunctuation', 'tools.asrCapabilityHotwords'],
    languages: ['en'],
    defaultLanguage: 'en',
    supportsHotwords: true,
    supportsTimestamps: true,
  },
  {
    id: 'sensevoice-small',
    modelId: 'iic/SenseVoiceSmall',
    titleKey: 'tools.asrPresetSenseVoice',
    descriptionKey: 'tools.asrPresetSenseVoiceDescription',
    scaleKey: 'tools.asrScaleSenseVoice',
    deviceKey: 'tools.asrDeviceCpuGpu',
    capabilityKeys: ['tools.asrCapabilityFiveLanguages', 'tools.asrCapabilityTimestamps', 'tools.asrCapabilityEmotionEvents'],
    languages: ['auto', 'zh', 'en', 'yue', 'ja', 'ko'],
    defaultLanguage: 'auto',
    supportsHotwords: false,
    supportsTimestamps: true,
  },
  {
    id: 'fun-asr-nano',
    modelId: 'FunAudioLLM/Fun-ASR-Nano-2512',
    titleKey: 'tools.asrPresetNano',
    descriptionKey: 'tools.asrPresetNanoDescription',
    scaleKey: 'tools.asrScaleNano',
    deviceKey: 'tools.asrDeviceGpuRecommended',
    capabilityKeys: ['tools.asrCapabilityDialect', 'tools.asrCapabilityLyrics', 'tools.asrCapabilityHotwords'],
    languages: ['auto', 'zh', 'en', 'ja'],
    defaultLanguage: 'auto',
    supportsHotwords: true,
    supportsTimestamps: false,
  },
  {
    id: 'fun-asr-mlt-nano',
    modelId: 'FunAudioLLM/Fun-ASR-MLT-Nano-2512',
    titleKey: 'tools.asrPresetMltNano',
    descriptionKey: 'tools.asrPresetMltNanoDescription',
    scaleKey: 'tools.asrScaleNano',
    deviceKey: 'tools.asrDeviceGpuRecommended',
    capabilityKeys: ['tools.asrCapability31Languages', 'tools.asrCapabilityMixedLanguage', 'tools.asrCapabilityHotwords'],
    languages: MULTILINGUAL_LANGUAGES,
    defaultLanguage: 'auto',
    supportsHotwords: true,
    supportsTimestamps: false,
  },
]

export const DEFAULT_ASR_PRESET: AsrPresetId = 'paraformer-zh'

export function findAsrPreset(value: unknown) {
  return ASR_PRESETS.find(preset => preset.id === value)
}
