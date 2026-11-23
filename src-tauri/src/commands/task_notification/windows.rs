use crate::error::{AppError, AppResult};
use std::ffi::OsString;
use std::io::ErrorKind;
use std::os::windows::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use windows::core::{Error, Interface, Result, BSTR, GUID, HSTRING};
use windows::Win32::Foundation::E_FAIL;
use windows::Win32::Storage::EnhancedStorage::PKEY_AppUserModel_ID;
use windows::Win32::System::Com::StructuredStorage::{
    PropVariantChangeType, PROPVARIANT, PVCHF_DEFAULT,
};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, IPersistFile,
    CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, STGM_READ, STGM_READWRITE,
};
use windows::Win32::System::Variant::VT_LPWSTR;
use windows::Win32::UI::Shell::PropertiesSystem::IPropertyStore;
use windows::Win32::UI::Shell::{
    FOLDERID_CommonPrograms, FOLDERID_Programs, IShellLinkW, SHChangeNotify, SHGetKnownFolderPath,
    ShellLink, KF_FLAG_DEFAULT, SHCNE_CREATE, SHCNE_UPDATEITEM, SHCNF_FLUSH, SHCNF_PATHW,
    SLGP_RAWPATH,
};

// Matches the Start menu shortcut installed by Inno Setup.
const SHORTCUT_NAME: &str = "Pymss Studio.lnk";

pub(super) fn ensure_registered(app_id: &str) -> AppResult<()> {
    static REGISTERED: Mutex<bool> = Mutex::new(false);
    register_once(&REGISTERED, || register(app_id))
}

fn register_once(
    registered: &Mutex<bool>,
    register: impl FnOnce() -> AppResult<()>,
) -> AppResult<()> {
    let mut registered = registered.lock().map_err(|_| {
        AppError::Worker("The notification registration state is unavailable.".into())
    })?;
    if !*registered {
        register()?;
        *registered = true;
    }
    Ok(())
}

fn register(app_id: &str) -> AppResult<()> {
    let exe = std::env::current_exe()?;
    let operation = || -> Result<()> {
        let _apartment = ShellApartment::initialize()?;
        let user = programs_folder(&FOLDERID_Programs)?.join(SHORTCUT_NAME);
        let common = programs_folder(&FOLDERID_CommonPrograms)?.join(SHORTCUT_NAME);
        register_shortcut(&exe, app_id, &user, &common)
    };
    operation().map_err(|error| AppError::Worker(error.to_string()))
}

struct ShellApartment;

impl ShellApartment {
    fn initialize() -> Result<Self> {
        unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()? };
        Ok(Self)
    }
}

impl Drop for ShellApartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

fn programs_folder(id: &GUID) -> Result<PathBuf> {
    // The shell allocates the nul-terminated path with its COM allocator.
    unsafe {
        let raw = SHGetKnownFolderPath(id, KF_FLAG_DEFAULT, None)?;
        let path = PathBuf::from(OsString::from_wide(raw.as_wide()));
        CoTaskMemFree(Some(raw.0.cast()));
        Ok(path)
    }
}

fn register_shortcut(exe: &Path, app_id: &str, user: &Path, common: &Path) -> Result<()> {
    let exe_identity = exe.canonicalize()?;
    if let Some(link) = load_shortcut(common)? {
        if targets_exe(&link, &exe_identity)? && matches_app_id(&link, app_id)? {
            return Ok(());
        }
    }

    let path = HSTRING::from(user.as_os_str());
    let (link, is_new, edit) = match load_shortcut(user)? {
        Some(link) => {
            if shortcut_edit(&link, &exe_identity, app_id)?.is_none() {
                return Ok(());
            }
            let file: IPersistFile = link.cast()?;
            unsafe { file.Load(&path, STGM_READWRITE)? };
            // Revalidate ownership after reopening the file for writing.
            let Some(edit) = shortcut_edit(&link, &exe_identity, app_id)? else {
                return Ok(());
            };
            (link, false, edit)
        }
        None => (shell_link()?, true, ShortcutEdit::Retarget),
    };
    if matches!(edit, ShortcutEdit::Retarget) {
        unsafe { link.SetPath(&HSTRING::from(exe.as_os_str()))? };
    }

    let store: IPropertyStore = link.cast()?;
    let file: IPersistFile = link.cast()?;
    let mut value = PROPVARIANT::default();
    let app_id_value = PROPVARIANT::from(app_id);
    // The AppUserModelID property expects VT_LPWSTR, not the BSTR from From<&str>.
    unsafe {
        PropVariantChangeType(&mut value, &app_id_value, PVCHF_DEFAULT, VT_LPWSTR)?;
        store.SetValue(&PKEY_AppUserModel_ID, &value)?;
        store.Commit()?;
        file.Save(&path, false)?;
        let event = if is_new {
            SHCNE_CREATE
        } else {
            SHCNE_UPDATEITEM
        };
        // The first toast can be sent as soon as registration returns.
        SHChangeNotify(
            event,
            SHCNF_PATHW | SHCNF_FLUSH,
            Some(path.as_ptr().cast()),
            None,
        );
    }
    Ok(())
}

fn shell_link() -> Result<IShellLinkW> {
    unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER) }
}

enum ShortcutEdit {
    SetAppId,
    Retarget,
}

fn shortcut_edit(link: &IShellLinkW, exe: &Path, app_id: &str) -> Result<Option<ShortcutEdit>> {
    let owned = matches_app_id(link, app_id)?;
    if targets_exe(link, exe)? {
        return Ok((!owned).then_some(ShortcutEdit::SetAppId));
    }
    if owned && target_is_missing(link)? {
        return Ok(Some(ShortcutEdit::Retarget));
    }
    Err(Error::new(
        E_FAIL,
        "The Start menu shortcut points to a different application.",
    ))
}

fn load_shortcut(path: &Path) -> Result<Option<IShellLinkW>> {
    match path.symlink_metadata() {
        Ok(metadata) if metadata.file_type().is_file() => {}
        Ok(_) => {
            return Err(Error::new(
                E_FAIL,
                "The Start menu shortcut is not a regular file.",
            ));
        }
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    let link = shell_link()?;
    let file: IPersistFile = link.cast()?;
    unsafe { file.Load(&HSTRING::from(path.as_os_str()), STGM_READ)? };
    Ok(Some(link))
}

fn targets_exe(link: &IShellLinkW, exe_identity: &Path) -> Result<bool> {
    Ok(shortcut_target(link, 0)?
        .and_then(|target| target.canonicalize().ok())
        .is_some_and(|target| target == exe_identity))
}

fn target_is_missing(link: &IShellLinkW) -> Result<bool> {
    let Some(target) = shortcut_target(link, SLGP_RAWPATH.0 as u32)? else {
        return Ok(false);
    };
    Ok(target.is_absolute()
        && target
            .metadata()
            .is_err_and(|error| error.kind() == ErrorKind::NotFound))
}

fn shortcut_target(link: &IShellLinkW, flags: u32) -> Result<Option<PathBuf>> {
    let mut path = [0u16; 260];
    unsafe { link.GetPath(&mut path, std::ptr::null_mut(), flags)? };
    let end = path
        .iter()
        .position(|&unit| unit == 0)
        .unwrap_or(path.len());
    Ok((end != 0).then(|| PathBuf::from(OsString::from_wide(&path[..end]))))
}

fn matches_app_id(link: &IShellLinkW, app_id: &str) -> Result<bool> {
    let store: IPropertyStore = link.cast()?;
    let value = unsafe { store.GetValue(&PKEY_AppUserModel_ID)? };
    Ok(!value.is_empty() && BSTR::try_from(&value)?.to_string() == app_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    const APP_ID: &str = "studio.pymss.desktop";

    fn legacy_shortcut(exe: &Path, path: &Path) -> Result<()> {
        let link = shell_link()?;
        let file: IPersistFile = link.cast()?;
        unsafe {
            link.SetPath(&HSTRING::from(exe.as_os_str()))?;
            file.Save(&HSTRING::from(path.as_os_str()), false)
        }
    }

    #[test]
    fn registers_upgraded_installation_in_current_user_folder() -> Result<()> {
        let _apartment = ShellApartment::initialize()?;
        let fixture = tempfile::tempdir()?;
        let exe = fixture.path().join("Pymss Studio 测试.exe");
        std::fs::write(&exe, b"fixture")?;
        let user = fixture.path().join("user.lnk");
        let common = fixture.path().join("common.lnk");
        legacy_shortcut(&exe, &common)?;
        let old_common = std::fs::read(&common)?;

        register_shortcut(&exe, APP_ID, &user, &common)?;

        let link = load_shortcut(&user)?.unwrap();
        assert!(targets_exe(&link, &exe.canonicalize()?)?);
        assert!(matches_app_id(&link, APP_ID)?);
        assert_eq!(std::fs::read(&common)?, old_common);
        let registration = std::fs::read(&user)?;
        register_shortcut(&exe, APP_ID, &user, &common)?;
        assert_eq!(std::fs::read(&user)?, registration);
        Ok(())
    }

    #[test]
    fn updates_owned_user_shortcut_and_protects_other_targets() -> Result<()> {
        let _apartment = ShellApartment::initialize()?;
        let fixture = tempfile::tempdir()?;
        let exe = fixture.path().join("Studio.exe");
        let other = fixture.path().join("Other.exe");
        std::fs::write(&exe, b"fixture")?;
        std::fs::write(&other, b"fixture")?;
        let user = fixture.path().join("user.lnk");
        let common = fixture.path().join("common.lnk");
        legacy_shortcut(&other, &user)?;
        let unrelated = std::fs::read(&user)?;
        let error = register_shortcut(&exe, APP_ID, &user, &common).unwrap_err();
        assert!(error.message().contains("different application"));
        assert_eq!(std::fs::read(&user)?, unrelated);

        legacy_shortcut(&exe, &user)?;
        register_shortcut(&exe, APP_ID, &user, &common)?;
        assert!(matches_app_id(&load_shortcut(&user)?.unwrap(), APP_ID)?);

        std::fs::rename(&user, &common)?;
        register_shortcut(&exe, APP_ID, &user, &common)?;
        assert!(!user.exists());
        Ok(())
    }

    #[test]
    fn repairs_only_the_owned_shortcut_after_reinstalling_elsewhere() -> Result<()> {
        let _apartment = ShellApartment::initialize()?;
        let fixture = tempfile::tempdir()?;
        let old = fixture.path().join("Old Studio.exe");
        let current = fixture.path().join("New Studio.exe");
        let user = fixture.path().join("user.lnk");
        let common = fixture.path().join("common.lnk");
        std::fs::write(&old, b"fixture")?;
        std::fs::write(&current, b"fixture")?;
        legacy_shortcut(&old, &user)?;
        legacy_shortcut(&current, &common)?;
        register_shortcut(&old, APP_ID, &user, &common)?;
        let saved = std::fs::read(&user)?;
        let saved_common = std::fs::read(&common)?;

        assert!(register_shortcut(&current, APP_ID, &user, &common).is_err());
        assert_eq!(std::fs::read(&user)?, saved);
        std::fs::remove_file(&old)?;
        assert!(register_shortcut(&current, "another.app", &user, &common).is_err());
        assert_eq!(std::fs::read(&user)?, saved);

        register_shortcut(&current, APP_ID, &user, &common)?;
        let link = load_shortcut(&user)?.unwrap();
        assert!(targets_exe(&link, &current.canonicalize()?)?);
        assert!(matches_app_id(&link, APP_ID)?);
        assert_eq!(std::fs::read(&common)?, saved_common);
        Ok(())
    }

    #[test]
    fn valid_shortcuts_need_no_user_write_access() -> Result<()> {
        let _apartment = ShellApartment::initialize()?;
        let fixture = tempfile::tempdir()?;
        let exe = fixture.path().join("Studio.exe");
        let user = fixture.path().join("user.lnk");
        let common = fixture.path().join("common.lnk");
        std::fs::write(&exe, b"fixture")?;
        register_shortcut(&exe, APP_ID, &user, &common)?;
        let registered = std::fs::read(&user)?;
        let original_permissions = std::fs::metadata(&user)?.permissions();
        let mut readonly = original_permissions.clone();
        readonly.set_readonly(true);
        std::fs::set_permissions(&user, readonly)?;
        let user_only = register_shortcut(&exe, APP_ID, &user, &common);
        let common_also_registered = (|| -> Result<()> {
            std::fs::write(&common, &registered)?;
            register_shortcut(&exe, APP_ID, &user, &common)
        })();
        std::fs::set_permissions(&user, original_permissions)?;
        user_only?;
        common_also_registered?;
        assert_eq!(std::fs::read(&user)?, registered);

        std::fs::write(&user, b"invalid shortcut")?;
        register_shortcut(&exe, APP_ID, &user, &common)?;
        assert_eq!(std::fs::read(&user)?, b"invalid shortcut");
        Ok(())
    }

    #[test]
    fn session_remembers_only_successful_registration() -> AppResult<()> {
        let registered = Mutex::new(false);
        assert!(register_once(&registered, || Err(AppError::Worker("fixture".into()))).is_err());
        assert!(!*registered.lock().unwrap());
        register_once(&registered, || Ok(()))?;
        assert!(*registered.lock().unwrap());
        register_once(&registered, || panic!("registration already confirmed"))?;
        Ok(())
    }
}
