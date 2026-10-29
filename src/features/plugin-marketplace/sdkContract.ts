import contract from '../../../python/plugin-sdk.json'

export const pluginSdkVersion = contract.version
export const pluginSdkCapabilities: readonly string[] = Object.freeze(contract.capabilities)
const supportedCapabilities = new Set(pluginSdkCapabilities)

export function supportsPluginCapability(capability: string): boolean {
  return supportedCapabilities.has(capability)
}
