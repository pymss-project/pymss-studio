use crate::error::{AppError, AppResult};
use reqwest::Client;
use semver::Version;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use url::Url;

const MODELSCOPE_TREE: &str =
    "https://www.modelscope.cn/api/v1/datasets/huanlinoto/Pymss-Studio-Dist/repo/tree";
const MODELSCOPE_ROOT: &str =
    "https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master";
const GITHUB_ROOT: &str = "https://github.com/pymss-project/pymss-studio/releases/download";
const CATALOG_PAGE_SIZE: usize = 200;
const MAX_CATALOG_PAGES: usize = 20;
const MAX_CATALOG_PAGE_BYTES: usize = 1024 * 1024;

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

pub fn repair_manifest_endpoints(version: &str, filename: &str, provider: UpdateProvider) -> AppResult<Vec<Url>> {
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

pub struct MirrorManifest {
    pub endpoint: Url,
    pub version: String,
}

pub fn http_client_builder() -> reqwest::ClientBuilder {
    // ModelScope discovery can run before the updater plugin initializes TLS.
    if rustls::crypto::CryptoProvider::get_default().is_none() {
        let _ = rustls::crypto::ring::default_provider().install_default();
    }
    Client::builder()
}

pub async fn modelscope_manifest(
    client: &Client,
    filename: &str,
) -> AppResult<MirrorManifest> {
    modelscope_manifest_at(client, &Url::parse(MODELSCOPE_TREE).unwrap(), filename).await
}

async fn modelscope_manifest_at(
    client: &Client,
    tree: &Url,
    filename: &str,
) -> AppResult<MirrorManifest> {
    let mut paths = HashSet::new();
    let mut received = 0;
    for page in 1..=MAX_CATALOG_PAGES {
        let mut endpoint = tree.clone();
        endpoint.query_pairs_mut().extend_pairs([
            ("Revision", "master".to_string()),
            ("Recursive", "True".to_string()),
            ("Root", "/".to_string()),
            ("PageNumber", page.to_string()),
            ("PageSize", CATALOG_PAGE_SIZE.to_string()),
        ]);
        let mut response = client
            .get(endpoint)
            .send()
            .await
            .map_err(|error| {
                AppError::Worker(format!("ModelScope catalog request failed: {error}"))
            })?
            .error_for_status()
            .map_err(|error| {
                AppError::Worker(format!("ModelScope catalog request failed: {error}"))
            })?;
        let mut body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| AppError::Worker(error.to_string()))?
        {
            if body.len().saturating_add(chunk.len()) > MAX_CATALOG_PAGE_BYTES {
                return Err(AppError::Worker(
                    "ModelScope update catalog exceeds the size limit".into(),
                ));
            }
            body.extend_from_slice(&chunk);
        }
        let data: serde_json::Value = serde_json::from_slice(&body)?;
        if data.get("Code").and_then(|value| value.as_u64()) != Some(200) {
            return Err(AppError::Worker(
                "ModelScope returned an invalid update catalog".into(),
            ));
        }
        let files = data
            .pointer("/Data/Files")
            .and_then(|value| value.as_array())
            .ok_or_else(|| {
                AppError::Worker("ModelScope returned an invalid update file list".into())
            })?;
        received += files.len();
        for file in files {
            if file.get("Type").and_then(|value| value.as_str()) == Some("blob") {
                if let Some(path) = file.get("Path").and_then(|value| value.as_str()) {
                    paths.insert(path.to_string());
                }
            }
        }
        let total = data
            .pointer("/Data/TotalCount")
            .and_then(|value| value.as_u64());
        if total.map_or(files.len() < CATALOG_PAGE_SIZE, |total| {
            received as u64 >= total
        }) {
            return latest_mirror_manifest(&paths, filename).ok_or_else(|| {
                AppError::Worker(format!(
                    "ModelScope has no complete update release for {filename}"
                ))
            });
        }
        if files.is_empty() {
            return Err(AppError::Worker(
                "ModelScope update catalog pagination is incomplete".into(),
            ));
        }
    }
    Err(AppError::Worker(
        "ModelScope update catalog exceeds the supported page limit".into(),
    ))
}

fn latest_mirror_manifest(paths: &HashSet<String>, filename: &str) -> Option<MirrorManifest> {
    paths
        .iter()
        .filter_map(|path| {
            let (folder, file) = path.split_once('/')?;
            if file != filename {
                return None;
            }
            let version = Version::parse(folder).ok()?;
            // A partially uploaded release must not advertise an unavailable update archive.
            let archive = if filename.ends_with("-macos-arm64-update.json") {
                archive_name_for(folder, "macos-arm64")
            } else if filename.ends_with("-linux-x64-update.json") {
                archive_name_for(folder, "linux-x64")
            } else {
                archive_name_for(folder, "windows-x64")
            };
            if !paths.contains(&format!("{folder}/{archive}")) {
                return None;
            }
            Some((version, folder))
        })
        .max_by(|left, right| left.0.cmp(&right.0))
        .map(|(_, folder)| MirrorManifest {
            endpoint: mirror_url(folder, filename),
            version: folder.to_string(),
        })
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
    let mut url =
        Url::parse("https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/tree/master")
            .ok()?;
    url.path_segments_mut().ok()?.push(version);
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
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::{Duration, Instant};

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
    fn macos_update_sources_require_the_matching_archive_and_architecture() {
        let files = paths(&[
            "1.0.0/stable-v1-macos-arm64-update.json",
            "1.0.0/Pymss-Studio-1.0.0-macos-arm64-update.app.tar.gz",
            "2.0.0/stable-v1-macos-arm64-update.json",
            "2.0.0/Pymss-Studio-2.0.0-windows-x64-update.zip",
        ]);
        assert_eq!(latest_mirror_manifest(&files, "stable-v1-macos-arm64-update.json").unwrap().version, "1.0.0");
        let github = Url::parse("https://github.com/pymss-project/pymss-studio/releases/download/v1.0.0/Pymss-Studio-1.0.0-macos-arm64-update.app.tar.gz").unwrap();
        let mirror = archive_url_for(&github, "1.0.0", UpdateProvider::Modelscope, "macos-arm64").unwrap();
        assert!(mirror.path().ends_with("/1.0.0/Pymss-Studio-1.0.0-macos-arm64-update.app.tar.gz"));
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Github, "windows-x64").is_none());
        assert!(archive_url_for(&github, "2.0.0", UpdateProvider::Github, "macos-arm64").is_none());
    }

    fn paths(values: &[&str]) -> HashSet<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn linux_update_sources_require_an_appimage_instead_of_a_windows_archive() {
        let files = paths(&[
            "1.0.0/stable-v1-linux-x64-update.json",
            "1.0.0/Pymss-Studio-1.0.0-linux-x64.AppImage",
            "2.0.0/stable-v1-linux-x64-update.json",
            "2.0.0/Pymss-Studio-2.0.0-windows-x64-update.zip",
        ]);
        assert_eq!(latest_mirror_manifest(&files, "stable-v1-linux-x64-update.json").unwrap().version, "1.0.0");
        let github = Url::parse("https://github.com/pymss-project/pymss-studio/releases/download/v1.0.0/Pymss-Studio-1.0.0-linux-x64.AppImage").unwrap();
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Modelscope, "linux-x64").is_some());
        assert!(archive_url_for(&github, "1.0.0", UpdateProvider::Github, "windows-x64").is_none());
    }

    #[test]
    fn discovery_respects_channel_versions_and_complete_uploads() {
        let files = paths(&[
            "0.0.19/stable-v1-windows-x64-update.json",
            "0.0.19/Pymss-Studio-0.0.19-windows-x64-update.zip",
            "0.0.20/prerelease-v1-windows-x64-update.json",
            "0.0.20/Pymss-Studio-0.0.20-windows-x64-update.zip",
            "0.0.21/stable-v1-windows-x64-update.json",
            "../stable-v1-windows-x64-update.json",
        ]);
        assert_eq!(
            latest_mirror_manifest(&files, "stable-v1-windows-x64-update.json")
                .unwrap()
                .version,
            "0.0.19"
        );
        let latest =
            latest_mirror_manifest(&files, "prerelease-v1-windows-x64-update.json").unwrap();
        assert_eq!(latest.version, "0.0.20");
        assert!(latest
            .endpoint
            .as_str()
            .contains("/resolve/master/0.0.20/prerelease-"));
        assert!(latest_mirror_manifest(&files, "unknown.json").is_none());
    }

    #[test]
    fn discovery_supports_semver_prereleases_and_build_metadata() {
        let files = paths(&[
            "1.0.0-rc.1/prerelease.json",
            "1.0.0-rc.1/Pymss-Studio-1.0.0-rc.1-windows-x64-update.zip",
            "1.0.0+build.2/prerelease.json",
            "1.0.0+build.2/Pymss-Studio-1.0.0+build.2-windows-x64-update.zip",
        ]);
        assert_eq!(
            latest_mirror_manifest(&files, "prerelease.json")
                .unwrap()
                .version,
            "1.0.0+build.2"
        );
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

    fn catalog_server(responses: Vec<String>) -> (Url, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint =
            Url::parse(&format!("http://{}/tree", listener.local_addr().unwrap())).unwrap();
        listener.set_nonblocking(true).unwrap();
        let server = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut requests = Vec::new();
            for body in responses {
                let (mut stream, _) = loop {
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(error)
                            if error.kind() == std::io::ErrorKind::WouldBlock
                                && Instant::now() < deadline =>
                        {
                            thread::sleep(Duration::from_millis(5))
                        }
                        Err(error) => panic!("Catalog fixture failed: {error}"),
                    }
                };
                stream.set_nonblocking(false).unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut request = vec![0; 8192];
                let size = stream.read(&mut request).unwrap();
                requests.push(String::from_utf8_lossy(&request[..size]).to_string());
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
            }
            requests
        });
        (endpoint, server)
    }

    #[test]
    fn catalog_fetch_uses_documented_parameters_and_follows_pagination() {
        let (endpoint, server) = catalog_server(vec![
            serde_json::json!({"Code": 200, "Data": {"TotalCount": 3, "Files": [
                {"Type":"blob", "Path":"0.0.20/prerelease.json"},
            ]}})
            .to_string(),
            serde_json::json!({"Code": 200, "Data": {"TotalCount": 3, "Files": [
                {"Type":"blob", "Path":"0.0.20/Pymss-Studio-0.0.20-windows-x64-update.zip"},
                {"Type":"blob", "Path":"0.0.21/prerelease.json"},
            ]}})
            .to_string(),
        ]);
        let selected = tauri::async_runtime::block_on(async {
            let client = http_client_builder()
                .no_proxy()
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap();
            modelscope_manifest_at(&client, &endpoint, "prerelease.json")
                .await
                .unwrap()
        });
        assert_eq!(selected.version, "0.0.20");
        let requests = server.join().unwrap();
        assert_eq!(requests.len(), 2);
        assert!(requests[0].contains("Revision=master"));
        assert!(requests[0].contains("Recursive=True"));
        assert!(requests[0].contains("PageNumber=1"));
        assert!(requests[1].contains("PageNumber=2"));
    }

    #[test]
    fn catalog_missing_channel_is_unavailable() {
        for files in [
            serde_json::json!([]),
            serde_json::json!([
                {"Type":"blob", "Path":"0.0.20/prerelease-v1-windows-x64-update.json"},
                {"Type":"blob", "Path":"0.0.20/Pymss-Studio-0.0.20-windows-x64-update.zip"},
            ]),
            serde_json::json!([
                {"Type":"blob", "Path":"0.0.21/stable-v1-windows-x64-update.json"},
            ]),
        ] {
            let (endpoint, server) = catalog_server(vec![serde_json::json!({
                "Code": 200, "Data": {"TotalCount": files.as_array().unwrap().len(), "Files": files},
            }).to_string()]);
            let result = tauri::async_runtime::block_on(async {
                let client = http_client_builder()
                    .no_proxy()
                    .timeout(Duration::from_secs(2))
                    .build()
                    .unwrap();
                modelscope_manifest_at(&client, &endpoint, "stable-v1-windows-x64-update.json").await
            });
            server.join().unwrap();
            assert!(result.is_err(), "Missing channel metadata was treated as a completed update check");
        }
    }

    #[test]
    fn catalog_errors_do_not_turn_into_a_successful_no_update_result() {
        for body in [
            r#"{"Code":500,"Data":{}}"#,
            r#"{"Code":200,"Data":{"TotalCount":1,"Files":[]}}"#,
            "<html>Unavailable</html>",
        ] {
            let (endpoint, server) = catalog_server(vec![body.to_string()]);
            let failed = tauri::async_runtime::block_on(async {
                let client = http_client_builder()
                    .no_proxy()
                    .timeout(Duration::from_secs(2))
                    .build()
                    .unwrap();
                modelscope_manifest_at(&client, &endpoint, "stable.json")
                    .await
                    .is_err()
            });
            server.join().unwrap();
            assert!(failed, "Invalid catalog was accepted: {body}");
        }
    }

    #[test]
    #[ignore = "Read-only check against the public ModelScope update catalog"]
    fn live_modelscope_update_catalog() {
        let manifest = tauri::async_runtime::block_on(async {
            let client = http_client_builder()
                .user_agent("Pymss-Studio-Updater")
                .timeout(Duration::from_secs(10))
                .build()
                .unwrap();
            modelscope_manifest(&client, "prerelease-v1-windows-x64-update.json")
                .await
                .unwrap()
        });
        println!(
            "ModelScope prerelease: {} ({})",
            manifest.version, manifest.endpoint
        );
        assert!(Version::parse(&manifest.version).is_ok());
    }
}
