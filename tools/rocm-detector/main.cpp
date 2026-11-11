#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#define __HIP_PLATFORM_AMD__
#include <hip/hip_runtime_api.h>
#include <algorithm>
#include <iostream>
#include <string>

#define STUDIO_STRING_IMPL(x) #x
#define STUDIO_STRING(x) STUDIO_STRING_IMPL(x)

static std::string quote(const char* raw) {
    std::string result = "\"";
    for (const unsigned char ch : std::string(raw ? raw : "")) {
        if (ch == '"' || ch == '\\') { result += '\\'; result += ch; }
        else if (ch >= 32) result += ch;
    }
    return result + "\"";
}

template <typename T> static T symbol(HMODULE module, const char* name) {
    return reinterpret_cast<T>(GetProcAddress(module, name));
}

int wmain(int argc, wchar_t** argv) {
    // The controller verifies this absolute file and the complete adjacent DLL
    // closure before launch. No PATH or current-directory DLL fallback is used.
    if (argc != 2 || !SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) {
        std::cout << "{\"schemaVersion\":1,\"available\":false,\"error\":\"invalid_launch\",\"devices\":[]}";
        return 2;
    }
    HMODULE module = LoadLibraryExW(argv[1], nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!module) {
        std::cout << "{\"schemaVersion\":1,\"available\":false,\"error\":\"runtime_load_failed\",\"win32Error\":"
                  << GetLastError() << ",\"devices\":[]}";
        return 0;
    }
    auto count_fn = symbol<decltype(&hipGetDeviceCount)>(module, STUDIO_STRING(hipGetDeviceCount));
    auto props_fn = symbol<decltype(&hipGetDeviceProperties)>(module, STUDIO_STRING(hipGetDeviceProperties));
    auto pci_fn = symbol<decltype(&hipDeviceGetPCIBusId)>(module, STUDIO_STRING(hipDeviceGetPCIBusId));
    if (!count_fn || !props_fn || !pci_fn) {
        std::cout << "{\"schemaVersion\":1,\"available\":false,\"error\":\"runtime_api_mismatch\",\"devices\":[]}";
        FreeLibrary(module);
        return 0;
    }
    int count = 0;
    const auto status = count_fn(&count);
    if (status != hipSuccess || count <= 0 || count > 256) {
        std::cout << "{\"schemaVersion\":1,\"available\":false,\"error\":\"device_unavailable\",\"hipError\":"
                  << static_cast<int>(status) << ",\"devices\":[]}";
        FreeLibrary(module);
        return 0;
    }
    std::cout << "{\"schemaVersion\":1,\"available\":true,\"devices\":[";
    bool first = true;
    for (int index = 0; index < std::min(count, 32); ++index) {
        hipDeviceProp_t properties{};
        char pci[64]{};
        if (props_fn(&properties, index) != hipSuccess) continue;
        properties.name[sizeof(properties.name) - 1] = 0;
        properties.gcnArchName[sizeof(properties.gcnArchName) - 1] = 0;
        if (pci_fn(pci, sizeof(pci), index) != hipSuccess) pci[0] = 0;
        pci[sizeof(pci) - 1] = 0;
        if (!first) std::cout << ',';
        first = false;
        std::cout << "{\"nativeIndex\":" << index << ",\"name\":" << quote(properties.name)
                  << ",\"gfx\":" << quote(properties.gcnArchName) << ",\"pciBusId\":" << quote(pci)
                  << ",\"memoryBytes\":" << static_cast<unsigned long long>(properties.totalGlobalMem)
                  << ",\"isDiscrete\":" << (properties.integrated ? "false" : "true") << '}';
    }
    std::cout << "]}";
    FreeLibrary(module);
    return 0;
}
