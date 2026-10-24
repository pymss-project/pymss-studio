use std::path::{Path, PathBuf};

/// Registered per-launch plugins root (marketplace managed dir), set via set_plugins_root.
static PLUGINS_ROOT: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

pub fn set_plugins_root(root: PathBuf) {
    let _ = PLUGINS_ROOT.set(root);
}

pub fn plugins_root() -> Option<&'static PathBuf> {
    PLUGINS_ROOT.get()
}

/// Serve `pymss-plugin://view/<plugin>/<view>/<relpath>` requests.
/// Containment is enforced twice: the manifest entry must stay inside the
/// plugin dir, and the requested file must resolve under that entry's dir.
pub fn serve_view(
    _app: &tauri::AppHandle,
    request: &tauri::http::Request<Vec<u8>>,
) -> tauri::http::Response<Vec<u8>> {
    use tauri::http::StatusCode;

    let failure = |status: u16, message: &str| {
        tauri::http::Response::builder()
            .status(StatusCode::from_u16(status).unwrap_or(StatusCode::NOT_FOUND))
            .body(message.as_bytes().to_vec())
            .unwrap()
    };
    let path = request.uri().path().trim_start_matches('/');
    let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    // Official SDK assets: pymss-plugin://sdk/pymss-sdk.css|js — served from the
    // bundled resources so plugin pages share Studio's design tokens without
    // shipping their own copy. Sandboxed iframes reference them by absolute
    // protocol URL, same as view files.
    if segments.len() == 2 && segments[0] == "sdk" {
        return serve_sdk_asset(_app, &segments[1]);
    }
    if segments.len() < 4 || segments[0] != "view" {
        return failure(404, "not found");
    }
    let (plugin_id, view_id) = (segments[1], segments[2]);
    let rel: Vec<&str> = segments[3..].to_vec();

    let root = match plugins_root() {
        Some(root) => root.clone(),
        None => return failure(503, "plugins root not ready"),
    };
    let entry = match resolve_view(&root, plugin_id, view_id) {
        Ok(entry) => entry,
        Err(error) => return failure(404, &error),
    };
    let base = match entry.parent() {
        Some(parent) => parent.to_path_buf(),
        None => return failure(500, "view entry has no parent"),
    };
    let joined = base.join(rel.join("/"));
    let resolved = match joined.canonicalize() {
        Ok(resolved) => resolved,
        Err(_) => return failure(404, "file not found"),
    };
    if !resolved.starts_with(&base.canonicalize().unwrap_or(base.clone())) {
        return failure(403, "path escapes the plugin view");
    }
    let bytes = match std::fs::read(&resolved) {
        Ok(bytes) => bytes,
        Err(_) => return failure(404, "file not found"),
    };
    let mime = match resolved.extension().and_then(|e| e.to_str()) {
        Some("html") => "text/html",
        Some("js") => "application/javascript",
        Some("css") => "text/css",
        Some("json") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("woff2") => "font/woff2",
        Some("wasm") => "application/wasm",
        _ => "application/octet-stream",
    };
    tauri::http::Response::builder()
        .header("Content-Type", mime)
        .body(bytes)
        .unwrap_or_else(|_| failure(500, "response build failed"))
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
    let root = root.canonicalize().map_err(|e| format!("plugins root unavailable: {e}"))?;
    // Plugin and view ids are restricted to filesystem-safe characters by the
    // manifest layer; reject anything exotic before touching the disk.
    for part in [plugin_id, view_id] {
        if part.is_empty()
            || !part.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
            || part.starts_with('.')
        {
            return Err(format!("invalid plugin view reference: {plugin_id}/{view_id}"));
        }
    }
    let plugin_dir = root.join(plugin_id);
    let manifest = plugin_dir.join("pyproject.toml");
    if !manifest.is_file() {
        return Err(format!("plugin {plugin_id} is not installed"));
    }
    // Reuse the Python manifest parser through the worker would round-trip;
    // here we parse only the views table we need with a minimal scan.
    let views = parse_views(&manifest)?;
    let view = views
        .into_iter()
        .find(|v| v.view_id == view_id)
        .ok_or_else(|| format!("plugin {plugin_id} has no view {view_id}"))?;
    let entry = plugin_dir.join(&view.entry);
    let resolved = entry
        .canonicalize()
        .map_err(|_| format!("view entry missing: {}", view.entry))?;
    if !resolved.starts_with(&root) || !resolved.is_file() {
        return Err(format!("view entry escapes the plugins root: {}", view.entry));
    }
    Ok(resolved)
}

struct RawView {
    view_id: String,
    entry: String,
}

/// Minimal TOML scan for [[tool.pymss.plugin.contributions.views]] id/entry pairs.
/// Full validation (permissions, placements, path containment) happens in the
/// Python manifest parser; this only needs id+entry to serve files safely.
fn parse_views(manifest: &Path) -> Result<Vec<RawView>, String> {
    let text = std::fs::read_to_string(manifest).map_err(|e| format!("cannot read manifest: {e}"))?;
    let mut views: Vec<RawView> = Vec::new();
    let mut current: Option<RawView> = None;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("[[tool.pymss.plugin.contributions.views]]") {
            if let Some(view) = current.take() {
                views.push(view);
            }
            current = Some(RawView { view_id: String::new(), entry: String::new() });
            continue;
        }
        let Some(view) = current.as_mut() else { continue };
        if let Some(value) = trimmed.strip_prefix("id") {
            if let Some(stripped) = value.trim_start().strip_prefix('=') {
                view.view_id = strip_quotes(stripped.trim());
            }
        } else if let Some(value) = trimmed.strip_prefix("entry") {
            if let Some(stripped) = value.trim_start().strip_prefix('=') {
                view.entry = strip_quotes(stripped.trim());
            }
        }
    }
    if let Some(view) = current {
        views.push(view);
    }
    views
        .into_iter()
        .filter(|v| !v.view_id.is_empty() && !v.entry.is_empty())
        .map(|v| {
            if v.entry.contains("..") || Path::new(&v.entry).is_absolute() {
                Err(format!("view entry path is not relative: {}", v.entry))
            } else {
                Ok(v)
            }
        })
        .collect()
}

fn strip_quotes(value: &str) -> String {
    let trimmed = value.trim();
    let trimmed = trimmed.split('#').next().unwrap_or(trimmed).trim();
    trimmed.trim_matches('"').trim_matches('\'').to_string()
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
        }

        impl TempDirGuard {
            pub fn new() -> Self {
                let path = std::env::temp_dir().join(format!(
                    "pymss-view-test-{}-{}",
                    std::process::id(),
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_nanos()
                ));
                std::fs::create_dir_all(&path).unwrap();
                Self { path }
            }
        }

        impl Drop for TempDirGuard {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.path);
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
            "[[tool.pymss.plugin.contributions.views]]\n",
            "id = \"editor\"\n",
            "entry = \"ui/index.html\"\n"
        ));
        let resolved = resolve_view(&root, "author.chain", "editor").unwrap();
        assert!(resolved.ends_with("ui/index.html"));
    }

    #[test]
    fn rejects_unknown_view_and_traversal() {
        let (_guard, root) = fixture(concat!(
            "[[tool.pymss.plugin.contributions.views]]\n",
            "id = \"editor\"\n",
            "entry = \"ui/index.html\"\n"
        ));
        assert!(resolve_view(&root, "author.chain", "missing").is_err());
        assert!(resolve_view(&root, "author.chain", "../secrets").is_err());
        assert!(resolve_view(&root, "unknown.plugin", "editor").is_err());
    }

    #[test]
    fn rejects_manifest_entry_that_escapes_the_plugin() {
        let guard = tempdir::TempDirGuard::new();
        let plugin = guard.path.join("author.evil");
        fs::create_dir_all(&plugin).unwrap();
        fs::write(guard.path.join("secret.html"), "x").unwrap();
        fs::write(
            plugin.join("pyproject.toml"),
            "[tool.pymss.plugin]\nid = \"author.evil\"\n[[tool.pymss.plugin.contributions.views]]\nid = \"v\"\nentry = \"../secret.html\"",
        )
        .unwrap();
        assert!(resolve_view(&guard.path, "author.evil", "v").is_err());
    }
}

/// Serve a bundled SDK asset (pymss-sdk.css / pymss-sdk.js).
/// Resolved from the app resource dir, one file, no traversal.
fn serve_sdk_asset(app: &tauri::AppHandle, name: &str) -> tauri::http::Response<Vec<u8>> {
    use tauri::http::StatusCode;
    use tauri::Manager;
    let failure = |status: u16, message: &str| {
        tauri::http::Response::builder()
            .status(StatusCode::from_u16(status).unwrap_or(StatusCode::NOT_FOUND))
            .body(message.as_bytes().to_vec())
            .unwrap()
    };
    let (file, mime) = match name {
        "pymss-sdk.css" => ("pymss-sdk.css", "text/css"),
        "pymss-sdk.js" => ("pymss-sdk.js", "application/javascript"),
        _ => return failure(404, "unknown sdk asset"),
    };
    let Ok(resource_dir) = app.path().resource_dir() else {
        return failure(503, "resource dir not available");
    };
    let path = resource_dir.join("plugin-sdk").join(file);
    match std::fs::read(&path) {
        Ok(bytes) => tauri::http::Response::builder()
            .header(tauri::http::header::CONTENT_TYPE, mime)
            .header(tauri::http::header::CACHE_CONTROL, "no-cache")
            .body(bytes)
            .unwrap_or_else(|_| failure(500, "response build failed")),
        Err(_) => failure(404, "sdk asset not bundled"),
    }
}
