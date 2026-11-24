"""Stage the console launcher with the default Windows executable icon."""

import argparse
from ctypes import WINFUNCTYPE, WinDLL, WinError, c_void_p, c_wchar_p, get_last_error, wstring_at
from ctypes.wintypes import BOOL, DWORD, HANDLE, HMODULE, LPARAM, LPCWSTR, WORD
from pathlib import Path
from shutil import copyfile


RT_ICON = 3
RT_GROUP_ICON = 14
LOAD_LIBRARY_AS_DATAFILE = 0x02
ERROR_RESOURCE_TYPE_NOT_FOUND = 1813

kernel32 = WinDLL("kernel32", use_last_error=True)
EnumNameCallback = WINFUNCTYPE(BOOL, HMODULE, c_void_p, c_void_p, LPARAM)
EnumLanguageCallback = WINFUNCTYPE(BOOL, HMODULE, c_void_p, c_void_p, WORD, LPARAM)


def bind(name, result, *parameters):
    function = getattr(kernel32, name)
    function.argtypes = parameters
    function.restype = result
    return function


load_library = bind("LoadLibraryExW", HMODULE, LPCWSTR, HANDLE, DWORD)
free_library = bind("FreeLibrary", BOOL, HMODULE)
enum_names = bind("EnumResourceNamesW", BOOL, HMODULE, c_void_p, EnumNameCallback, LPARAM)
enum_languages = bind("EnumResourceLanguagesW", BOOL, HMODULE, c_void_p, c_void_p, EnumLanguageCallback, LPARAM)
begin_update = bind("BeginUpdateResourceW", HANDLE, LPCWSTR, BOOL)
update_resource = bind("UpdateResourceW", BOOL, HANDLE, c_void_p, c_void_p, WORD, c_void_p, DWORD)
end_update = bind("EndUpdateResourceW", BOOL, HANDLE, BOOL)


def icon_resources(executable: Path) -> list[tuple[int, int | str, int]]:
    module = load_library(str(executable), None, LOAD_LIBRARY_AS_DATAFILE)
    if not module:
        raise WinError()

    resources = []
    try:
        for resource_type in (RT_GROUP_ICON, RT_ICON):
            names = []

            @EnumNameCallback
            def collect_name(_module, _type, name, _context):
                names.append(name if name <= 0xFFFF else wstring_at(name))
                return True

            if not enum_names(module, resource_type, collect_name, 0):
                error = get_last_error()
                if error == ERROR_RESOURCE_TYPE_NOT_FOUND:
                    continue
                raise WinError(error)

            for name in names:
                @EnumLanguageCallback
                def collect_language(_module, _type, _name, language, _context):
                    resources.append((resource_type, name, language))
                    return True

                resource_name = c_wchar_p(name) if isinstance(name, str) else name
                if not enum_languages(module, resource_type, resource_name, collect_language, 0):
                    raise WinError()
    finally:
        free_library(module)
    return resources


def stage_cli(source: Path, stage: Path) -> None:
    launcher = stage.resolve() / "pymss.exe"
    copyfile(source, launcher)
    icons = icon_resources(launcher)
    if not icons:
        return

    # Update only the icon resources so the manifest and version info are preserved.
    update = begin_update(str(launcher), False)
    if not update:
        raise WinError()
    discard = True
    try:
        for resource_type, name, language in icons:
            resource_name = c_wchar_p(name) if isinstance(name, str) else name
            if not update_resource(update, resource_type, resource_name, language, None, 0):
                raise WinError()
        discard = False
    finally:
        if not end_update(update, discard) and not discard:
            raise WinError()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("stage", type=Path)
    args = parser.parse_args()
    stage_cli(args.source, args.stage)
