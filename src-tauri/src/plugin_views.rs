use std::path::{Component, Path, PathBuf};
use std::sync::RwLock;

static PLUGINS_ROOT: RwLock<Option<PathBuf>> = RwLock::new(None);

pub fn set_plugins_root(root: PathBuf) {
    *PLUGINS_ROOT
        .write()
        .unwrap_or_else(|error| error.into_inner()) = Some(root);
}

pub fn plugins_root() -> Option<PathBuf> {
    PLUGINS_ROOT
        .read()
        .unwrap_or_else(|error| error.into_inner())
        .clone()
}

fn response(status: u16, mime: &str, bytes: Vec<u8>) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .status(status)
        .header("Content-Type", mime)
        .header("Access-Control-Allow-Origin", "*")
        .header("Cache-Control", "no-cache")
        .body(bytes)
        .expect("static protocol headers are valid")
}

fn failure(status: u16, message: &str) -> tauri::http::Response<Vec<u8>> {
    response(
        status,
        "text/plain; charset=utf-8",
        message.as_bytes().to_vec(),
    )
}

fn decode_path(path: &str) -> Result<Vec<String>, String> {
    path.split('/')
        .filter(|segment| !segment.is_empty())
        .map(|segment| {
            percent_encoding::percent_decode_str(segment)
                .decode_utf8()
                .map(|value| value.into_owned())
                .map_err(|_| "invalid UTF-8 path".into())
        })
        .collect()
}

/// Serve view files and the bundled SDK through Tauri's platform-specific protocol URL.
pub fn serve_view(
    app: &tauri::AppHandle,
    request: &tauri::http::Request<Vec<u8>>,
) -> tauri::http::Response<Vec<u8>> {
    let segments = match decode_path(request.uri().path()) {
        Ok(segments) => segments,
        Err(error) => return failure(400, &error),
    };
    if segments.len() == 2 && segments[0] == "sdk" {
        return serve_sdk_asset(app, &segments[1]);
    }
    if segments.len() < 4 || segments[0] != "view" {
        return failure(404, "not found");
    }
    let Some(root) = plugins_root() else {
        return failure(503, "plugins root not ready");
    };
    serve_view_file(&root, &segments[1], &segments[2], &segments[3..])
}

fn serve_view_file(
    root: &Path,
    plugin_id: &str,
    view_id: &str,
    relative: &[String],
) -> tauri::http::Response<Vec<u8>> {
    let entry = match resolve_view(root, plugin_id, view_id) {
        Ok(entry) => entry,
        Err(error) => return failure(404, &error),
    };
    let Some(base) = entry.parent() else {
        return failure(500, "view entry has no parent");
    };
    let resolved = match base.join(relative.join("/")).canonicalize() {
        Ok(resolved) => resolved,
        Err(_) => return failure(404, "file not found"),
    };
    if !resolved.starts_with(base) {
        return failure(403, "path escapes the plugin view");
    }
    match std::fs::read(&resolved) {
        Ok(bytes) => response(200, mime_type(&resolved), bytes),
        Err(_) => failure(404, "file not found"),
    }
}

fn mime_type(path: &Path) -> &'static str {
    match path.extension().and_then(|extension| extension.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("js") | Some("mjs") => "application/javascript",
        Some("css") => "text/css",
        Some("json") | Some("map") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("woff") => "font/woff",
        Some("woff2") => "font/woff2",
        Some("wasm") => "application/wasm",
        _ => "application/octet-stream",
    }
}

/// Resolved plugin view contribution served to the Studio frontend.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PluginView {
    pub plugin_id: String,
    pub view_id: String,
    pub title: String,
    /// Absolute filesystem path of the view entry (index.html) inside the plugin directory.
    pub entry_path: String,
    pub placements: Vec<String>,
    pub operation: Option<String>,
    pub permissions: Vec<String>,
}

/// Guarded resolution of a plugin view entry: the path must live inside the
/// given plugins root (no `..` traversal, no absolute override from the webview).
pub fn resolve_view(root: &Path, plugin_id: &str, view_id: &str) -> Result<PathBuf, String> {
    let root = root
        .canonicalize()
        .map_err(|e| format!("plugins root unavailable: {e}"))?;
    // Plugin and view ids are restricted to filesystem-safe characters by the
    // manifest layer; reject anything exotic before touching the disk.
    for part in [plugin_id, view_id] {
        if part.is_empty()
            || !part
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
            || part.starts_with('.')
        {
            return Err(format!(
                "invalid plugin view reference: {plugin_id}/{view_id}"
            ));
        }
    }
    let plugin_dir = root
        .join(plugin_id)
        .canonicalize()
        .map_err(|_| format!("plugin {plugin_id} is not installed"))?;
    if !plugin_dir.starts_with(&root) {
        return Err(format!("plugin {plugin_id} escapes the plugins root"));
    }
    let manifest = plugin_dir.join("pyproject.toml");
    if !manifest.is_file() {
        return Err(format!("plugin {plugin_id} is not installed"));
    }
    let views = parse_views(&manifest)?;
    let view = views
        .into_iter()
        .find(|v| v.view_id == view_id)
        .ok_or_else(|| format!("plugin {plugin_id} has no view {view_id}"))?;
    let entry = plugin_dir.join(&view.entry);
    let resolved = entry
        .canonicalize()
        .map_err(|_| format!("view entry missing: {}", view.entry))?;
    if !resolved.starts_with(&plugin_dir) || !resolved.is_file() {
        return Err(format!(
            "view entry escapes the plugin directory: {}",
            view.entry
        ));
    }
    Ok(resolved)
}

struct RawView {
    view_id: String,
    entry: String,
}

/// Read only view identities and entry paths from `[[tool.pymss.studio.views]]`; the worker validates the full declaration.
fn parse_views(manifest: &Path) -> Result<Vec<RawView>, String> {
    let text = std::fs::read_to_string(manifest)
        .map_err(|error| format!("cannot read manifest: {error}"))?;
    let document: toml::Value =
        toml::from_str(&text).map_err(|error| format!("invalid plugin manifest: {error}"))?;
    let Some(views) = document
        .get("tool")
        .and_then(|value| value.get("pymss"))
        .and_then(|value| value.get("studio"))
        .and_then(|value| value.get("views"))
    else {
        return Ok(Vec::new());
    };
    let views = views.as_array().ok_or("plugin views must be an array")?;
    views
        .iter()
        .map(|view| {
            let view_id = view
                .get("id")
                .and_then(toml::Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or("plugin view id is missing")?;
            let entry = view
                .get("entry")
                .and_then(toml::Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or("plugin view entry is missing")?;
            if Path::new(entry).components().any(|component| {
                matches!(
                    component,
                    Component::ParentDir | Component::RootDir | Component::Prefix(_)
                )
            }) {
                return Err(format!("view entry path is not relative: {entry}"));
            }
            Ok(RawView {
                view_id: view_id.to_string(),
                entry: entry.to_string(),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn fixture(views: &str) -> (tempdir::TempDirGuard, PathBuf) {
        let guard = tempdir::TempDirGuard::new();
        let root = guard.path.clone();
        let plugin = root.join("author.chain");
        fs::create_dir_all(plugin.join("ui")).unwrap();
        fs::write(plugin.join("ui/index.html"), "<html></html>").unwrap();
        fs::write(
            plugin.join("pyproject.toml"),
            format!("[tool.pymss.plugin]\nid = \"author.chain\"\n{views}"),
        )
        .unwrap();
        (guard, root)
    }

    mod tempdir {
        use std::path::{Path, PathBuf};

        pub struct TempDirGuard {
            pub path: PathBuf,
            _guard: tempfile::TempDir,
        }

        impl TempDirGuard {
            pub fn new() -> Self {
                let guard = tempfile::Builder::new()
                    .prefix("pymss-view-test-")
                    .tempdir()
                    .unwrap();
                Self {
                    path: guard.path().to_path_buf(),
                    _guard: guard,
                }
            }
        }

        impl AsRef<Path> for TempDirGuard {
            fn as_ref(&self) -> &Path {
                &self.path
            }
        }
    }

    #[test]
    fn resolves_a_declared_view_entry() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.studio.views]]\n",
            "id = \"editor\"\n",
            "entry = \"ui/index.html\"\n"
        ));
        let resolved = resolve_view(&root, "author.chain", "editor").unwrap();
        assert!(resolved.ends_with("ui/index.html"));
    }

    #[test]
    fn rejects_unknown_view_and_traversal() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.studio.views]]\n",
            "id = \"editor\"\n",
            "entry = \"ui/index.html\"\n"
        ));
        assert!(resolve_view(&root, "author.chain", "missing").is_err());
        assert!(resolve_view(&root, "author.chain", "../secrets").is_err());
        assert!(resolve_view(&root, "unknown.plugin", "editor").is_err());
    }

    #[test]
    fn ignores_views_declared_in_the_v1_manifest_location() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.plugin.contributions.views]]
",
            "id = \"editor\"
",
            "entry = \"ui/index.html\"
"
        ));
        assert!(resolve_view(&root, "author.chain", "editor").is_err());
    }

    #[test]
    fn rejects_manifest_entry_that_escapes_the_plugin() {
        let guard = tempdir::TempDirGuard::new();
        let plugin = guard.path.join("author.evil");
        fs::create_dir_all(&plugin).unwrap();
        fs::write(guard.path.join("secret.html"), "x").unwrap();
        fs::write(
            plugin.join("pyproject.toml"),
            "[tool.pymss.plugin]\nid = \"author.evil\"\n[[tool.pymss.studio.views]]\nid = \"v\"\nentry = \"../secret.html\"",
        )
        .unwrap();
        assert!(resolve_view(&guard.path, "author.evil", "v").is_err());
    }

    #[test]
    fn parses_quoted_paths_and_stops_at_other_contributions() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.studio.views]]\n",
            "id = \"editor\"\nentry = \"ui/panel #1.html\" # entry comment\n",
            "[[tool.pymss.plugin.contributions.graphs]]\n",
            "id = \"chain\"\nfile = \"graphs/chain.json\"\n",
        ));
        fs::write(root.join("author.chain/ui/panel #1.html"), "panel").unwrap();
        let entry = resolve_view(&root, "author.chain", "editor").unwrap();
        assert!(entry.ends_with("ui/panel #1.html"));
    }

    #[test]
    fn serves_encoded_entry_names_and_module_scripts_with_cors() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.studio.views]]\n",
            "id = \"editor\"\nentry = \"ui/panel #1.html\"\n",
        ));
        fs::write(root.join("author.chain/ui/panel #1.html"), "panel").unwrap();
        fs::write(
            root.join("author.chain/ui/main.mjs"),
            "export const value = 1",
        )
        .unwrap();
        let segments = decode_path("/view/author.chain/editor/panel%20%231.html").unwrap();
        let html = serve_view_file(&root, &segments[1], &segments[2], &segments[3..]);
        assert_eq!(html.status(), 200);
        assert_eq!(html.body(), b"panel");
        let module = serve_view_file(&root, "author.chain", "editor", &["main.mjs".into()]);
        assert_eq!(module.status(), 200);
        assert_eq!(module.headers()["Content-Type"], "application/javascript");
        assert_eq!(module.headers()["Access-Control-Allow-Origin"], "*");
        let escaped = serve_view_file(
            &root,
            "author.chain",
            "editor",
            &["..".into(), "pyproject.toml".into()],
        );
        assert_eq!(escaped.status(), 403);
    }

    #[test]
    fn reads_sdk_from_the_bundled_resource_layout() {
        let guard = tempdir::TempDirGuard::new();
        let sdk = guard.path.join("resources/plugin-sdk");
        fs::create_dir_all(&sdk).unwrap();
        fs::write(sdk.join("pymss-sdk.js"), "window.pymss = {}").unwrap();
        let result = read_sdk_asset(&guard.path, "pymss-sdk.js");
        assert_eq!(result.status(), 200);
        assert_eq!(result.headers()["Content-Type"], "application/javascript");
        assert_eq!(result.headers()["Access-Control-Allow-Origin"], "*");
        assert_eq!(read_sdk_asset(&guard.path, "../secret").status(), 404);
    }

    #[test]
    fn managed_update_sdk_takes_priority_over_legacy_resources() {
        let guard = tempdir::TempDirGuard::new();
        let managed = guard.path.join("python/plugin-sdk");
        let legacy = guard.path.join("resources/plugin-sdk");
        fs::create_dir_all(&managed).unwrap();
        fs::create_dir_all(&legacy).unwrap();
        fs::write(managed.join("pymss-sdk.js"), "current sdk").unwrap();
        fs::write(legacy.join("pymss-sdk.js"), "old sdk").unwrap();
        fs::write(legacy.join("pymss-sdk.css"), "bundled css").unwrap();
        assert_eq!(
            read_sdk_asset(&guard.path, "pymss-sdk.js").body(),
            b"current sdk"
        );
        assert_eq!(
            read_sdk_asset(&guard.path, "pymss-sdk.css").body(),
            b"bundled css"
        );
    }

    #[test]
    fn switches_the_plugins_root_between_runtimes() {
        let first = tempdir::TempDirGuard::new();
        let second = tempdir::TempDirGuard::new();
        set_plugins_root(first.path.clone());
        assert_eq!(plugins_root(), Some(first.path.clone()));
        set_plugins_root(second.path.clone());
        assert_eq!(plugins_root(), Some(second.path.clone()));
    }

    #[cfg(unix)]
    #[test]
    fn rejects_an_entry_symlink_into_another_plugin() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.studio.views]]\n",
            "id = \"editor\"\nentry = \"ui/foreign.html\"\n",
        ));
        let foreign = root.join("other.plugin/ui");
        fs::create_dir_all(&foreign).unwrap();
        fs::write(foreign.join("index.html"), "foreign").unwrap();
        std::os::unix::fs::symlink(
            foreign.join("index.html"),
            root.join("author.chain/ui/foreign.html"),
        )
        .unwrap();
        assert!(resolve_view(&root, "author.chain", "editor").is_err());
    }
}

fn sdk_asset_path(resource_dir: &Path, name: &str) -> Option<PathBuf> {
    match name {
        "pymss-sdk.css" | "pymss-sdk.js" => {
            // Windows managed updates already replace the python directory.
            let managed = resource_dir.join("python").join("plugin-sdk").join(name);
            Some(if managed.is_file() {
                managed
            } else {
                resource_dir.join("resources").join("plugin-sdk").join(name)
            })
        }
        _ => None,
    }
}

fn read_sdk_asset(resource_dir: &Path, name: &str) -> tauri::http::Response<Vec<u8>> {
    let Some(path) = sdk_asset_path(resource_dir, name) else {
        return failure(404, "unknown sdk asset");
    };
    match std::fs::read(&path) {
        Ok(bytes) => response(200, mime_type(&path), bytes),
        Err(_) => failure(404, "sdk asset not bundled"),
    }
}

fn serve_sdk_asset(app: &tauri::AppHandle, name: &str) -> tauri::http::Response<Vec<u8>> {
    use tauri::Manager;
    match app.path().resource_dir() {
        Ok(root) => read_sdk_asset(&root, name),
        Err(_) => failure(503, "resource dir not available"),
    }
}
