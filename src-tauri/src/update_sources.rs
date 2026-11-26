use crate::error::AppError;
use reqwest::Client;
use semver::Version;
use serde::{Deserialize, Serialize};
use url::Url;

const MODELSCOPE_REPOSITORY: &str =
    "https://www.modelscope.cn/models/pymss-project/pymss-studio-releases";
const MODELSCOPE_ROOT: &str =
    "https://www.modelscope.cn/models/pymss-project/pymss-studio-releases/resolve/master";
const GITHUB_ROOT: &str = "https://github.com/pymss-project/pymss-studio/releases/download";

pub fn platform_slug() -> &'static str {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "macos-arm64"
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        "linux-x64"
    } else {
        "windows-x64"
    }
}

pub fn metadata_name(channel: &str, generation: &str) -> String {
    format!("{channel}-{generation}-{}-update.json", platform_slug())
}

#[cfg(any(all(target_os = "macos", target_arch = "aarch64"), test))]
pub fn repair_manifest_endpoints(version: &str, filename: &str, provider: UpdateProvider) -> crate::error::AppResult<Vec<Url>> {
    Version::parse(version)
        .map_err(|error| AppError::Worker(format!("Invalid repair version: {error}")))?;
    match provider {
        UpdateProvider::Github => [format!("v{version}"), version.to_string()]
            .into_iter()
            .map(|tag| Url::parse(&format!("{GITHUB_ROOT}/{tag}/{filename}"))
                .map_err(|error| AppError::Worker(error.to_string())))
            .collect(),
        UpdateProvider::Modelscope => Ok(vec![mirror_url(version, filename)]),
        UpdateProvider::Custom => Err(AppError::Worker("Application repair requires an official release source".into())),
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum UpdateSource {
    #[default]
    Auto,
    Github,
    Modelscope,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum UpdateProvider {
    Github,
    Modelscope,
    Custom,
}

impl UpdateSource {
    pub fn providers(self) -> &'static [UpdateProvider] {
        match self {
            Self::Auto => &[UpdateProvider::Github, UpdateProvider::Modelscope],
            Self::Github => &[UpdateProvider::Github],
            Self::Modelscope => &[UpdateProvider::Modelscope],
        }
    }
}

pub fn http_client_builder() -> reqwest::ClientBuilder {
    // ModelScope discovery can run before the updater plugin initializes TLS.
    if rustls::crypto::CryptoProvider::get_default().is_none() {
        let _ = rustls::crypto::ring::default_provider().install_default();
    }
    Client::builder()
}

pub(crate) fn metadata_request_error(error: reqwest::Error) -> AppError {
    let code = if error.is_timeout() {
        "UPDATE_CHECK_TIMEOUT"
    } else if error.status() == Some(reqwest::StatusCode::NOT_FOUND) {
        "UPDATE_HTTP_NOT_FOUND"
    } else {
        "UPDATE_REQUEST_FAILED"
    };
    AppError::Worker(format!("{code}: {}", error.without_url()))
}

/// ModelScope mirrors the GitHub `updater` release under a fixed directory. CI
/// uploads it after the release directory, so it never names a missing archive.
pub fn modelscope_metadata_url(filename: &str) -> Url {
    let mut url = Url::parse(MODELSCOPE_ROOT).unwrap();
    url.path_segments_mut().unwrap().push("updater").push(filename);
    url
}

pub fn archive_name(version: &str) -> String {
    archive_name_for(version, platform_slug())
}

fn archive_name_for(version: &str, platform: &str) -> String {
    if platform == "macos-arm64" {
        format!("Pymss-Studio-{version}-macos-arm64-update.app.tar.gz")
    } else if platform == "linux-x64" {
        format!("Pymss-Studio-{version}-linux-x64.AppImage")
    } else {
        format!("Pymss-Studio-{version}-windows-x64-update.zip")
    }
}

fn mirror_url(version: &str, filename: &str) -> Url {
    let mut url = Url::parse(MODELSCOPE_ROOT).unwrap();
    url.path_segments_mut()
        .unwrap()
        .push(version)
        .push(filename);
    url
}

pub fn release_page(version: &str) -> Option<String> {
    Version::parse(version).ok()?;
    let mut url = Url::parse(MODELSCOPE_REPOSITORY).ok()?;
    url.path_segments_mut().ok()?.extend(["tree", "master", version]);
    Some(url.to_string())
}

pub fn archive_url(original: &Url, version: &str, provider: UpdateProvider) -> Option<Url> {
    archive_url_for(original, version, provider, platform_slug())
}

fn archive_url_for(original: &Url, version: &str, provider: UpdateProvider, platform: &str) -> Option<Url> {
    Version::parse(version).ok()?;
    let filename = archive_name_for(version, platform);
    let github = Url::parse(&format!("{GITHUB_ROOT}/v{version}/{filename}")).ok()?;
    let github_without_v = Url::parse(&format!("{GITHUB_ROOT}/{version}/{filename}")).ok()?;
    let mirror = mirror_url(version, &filename);
    if original != &github && original != &github_without_v && original != &mirror {
        return None;
    }
    match provider {
        UpdateProvider::Github => Some(github),
        UpdateProvider::Modelscope => Some(mirror),
        UpdateProvider::Custom => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repair_metadata_is_pinned_to_the_official_installed_release() {
        let name = "stable-v1-macos-arm64-update.json";
        let github = repair_manifest_endpoints("1.2.3", name, UpdateProvider::Github).unwrap();
        assert_eq!(github[0].as_str(), format!("{GITHUB_ROOT}/v1.2.3/{name}"));
        assert_eq!(github[1].as_str(), format!("{GITHUB_ROOT}/1.2.3/{name}"));
        let mirror = repair_manifest_endpoints("1.2.3", name, UpdateProvider::Modelscope).unwrap();
        assert_eq!(mirror[0], mirror_url("1.2.3", name));
        assert!(repair_manifest_endpoints("../updater", name, UpdateProvider::Github).is_err());
        assert!(repair_manifest_endpoints("1.2.3", name, UpdateProvider::Custom).is_err());
    }

    #[test]
    fn modelscope_metadata_and_release_pages_use_the_release_repository() {
        assert_eq!(
            modelscope_metadata_url("stable-v2-windows-x64-update.json").as_str(),
            "https://www.modelscope.cn/models/pymss-project/pymss-studio-releases/resolve/master/updater/stable-v2-windows-x64-update.json"
        );
        assert_eq!(
            release_page("1.2.3-rc.1").unwrap(),
            "https://www.modelscope.cn/models/pymss-project/pymss-studio-releases/tree/master/1.2.3-rc.1"
        );
        assert!(release_page("../updater").is_none());
    }

    #[test]
    fn macos_update_archives_require_the_matching_architecture() {
        let github = Url::parse("https://github.com/pymss-project/pymss-studio/releases/download/v1.0.0/Pymss-Studio-1.0.0-macos-arm64-update.app.tar.gz").unwrap();
        let mirror = archive_url_for(&github, "1.0.0", UpdateProvider::Modelscope, "macos-arm64").unwrap();
        assert!(mirror.path().ends_with("/1.0.0/Pymss-Studio-1.0.0-macos-arm64-update.app.tar.gz"));
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Github, "windows-x64").is_none());
        assert!(archive_url_for(&github, "2.0.0", UpdateProvider::Github, "macos-arm64").is_none());
    }

    #[test]
    fn linux_update_archives_are_appimages_instead_of_windows_archives() {
        let github = Url::parse("https://github.com/pymss-project/pymss-studio/releases/download/v1.0.0/Pymss-Studio-1.0.0-linux-x64.AppImage").unwrap();
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Modelscope, "linux-x64").is_some());
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Github, "windows-x64").is_none());
    }

    #[test]
    fn only_the_official_archive_for_the_same_version_is_mirrored() {
        let archive_url = |original: &Url, version: &str, provider| {
            archive_url_for(original, version, provider, "windows-x64")
        };
        let github = Url::parse("https://github.com/pymss-project/pymss-studio/releases/download/v0.0.20/Pymss-Studio-0.0.20-windows-x64-update.zip").unwrap();
        let mirror = archive_url(&github, "0.0.20", UpdateProvider::Modelscope).unwrap();
        assert!(mirror
            .as_str()
            .ends_with("/0.0.20/Pymss-Studio-0.0.20-windows-x64-update.zip"));
        assert_eq!(
            archive_url(&mirror, "0.0.20", UpdateProvider::Github),
            Some(github.clone())
        );
        assert!(archive_url(&github, "0.0.21", UpdateProvider::Modelscope).is_none());
        assert!(archive_url(
            &Url::parse("https://example.com/update.zip").unwrap(),
            "0.0.20",
            UpdateProvider::Modelscope
        )
        .is_none());
        assert!(archive_url(&github, "../bad", UpdateProvider::Modelscope).is_none());
    }

    #[test]
    fn explicit_source_preferences_do_not_silently_change_provider() {
        assert_eq!(UpdateSource::default(), UpdateSource::Auto);
        assert_eq!(UpdateSource::Github.providers(), &[UpdateProvider::Github]);
        assert_eq!(
            UpdateSource::Modelscope.providers(),
            &[UpdateProvider::Modelscope]
        );
        assert_eq!(UpdateSource::Auto.providers().len(), 2);
    }
}
