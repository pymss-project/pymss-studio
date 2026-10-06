//! mo-build：读 installer.toml 清单，产出单文件 setup.exe。

use mo_core::Error;
use mo_core::manifest::Manifest;
use mo_core::overlay::PackageBuilder;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

/// 内置模板：由 build.rs 从 target/<profile>/mo-setup.exe 复制而来。
/// 首次全量构建前为占位符，此时必须显式指定 template。
pub const EMBEDDED_TEMPLATE: &[u8] =
    include_bytes!(concat!(env!("OUT_DIR"), "/mo-setup-template.exe"));

#[derive(Debug, Clone, Default)]
pub struct BuildOptions {
    /// 安装器模板 exe；None 时使用内置模板。
    pub template: Option<PathBuf>,
    /// 输出路径；None 时输出到清单所在目录：{name}-{version}-setup.exe
    pub out: Option<PathBuf>,
    /// 构建后执行的签名命令（{out} 占位替换为输出路径）。签名是最后一步：overlay 在 PE 证书表之后追加。
    pub sign: Option<String>,
}

#[derive(Debug)]
pub struct BuildStats {
    pub out_path: PathBuf,
    pub file_count: usize,
    pub total_input: u64,
    pub total_output: u64,
}

/// 构建 setup.exe。
pub fn build(manifest_path: &Path, opts: &BuildOptions) -> Result<BuildStats, Error> {
    let base_dir = manifest_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let manifest = Manifest::from_toml_str(&fs::read_to_string(manifest_path)?)?;
    manifest.validate()?;
    let compression = manifest.options.compression_level()?;

    let template: Vec<u8> = match &opts.template {
        Some(p) => fs::read(p)?,
        None => {
            if EMBEDDED_TEMPLATE.starts_with(b"MO-PLACEHOLDER") {
                return Err(Error::Overlay(
                    "内置模板尚未固化（首次 workspace 构建限制）。请先运行 cargo build --workspace 两次，或用 --template 显式指定 mo-setup.exe".into(),
                ));
            }
            EMBEDDED_TEMPLATE.to_vec()
        }
    };

    let mut builder = PackageBuilder::new(&manifest, compression)?;
    for (i, rule) in manifest.files.iter().enumerate() {
        let matched = collect_rule_files(&base_dir, &rule.src)?;
        if matched.is_empty() {
            return Err(Error::Glob(format!(
                "files.src 未匹配到任何文件: {:?}",
                rule.src
            )));
        }
        for (abs, rel) in matched {
            let content = fs::read(&abs)?;
            let md = fs::metadata(&abs);
            let attrs = md
                .as_ref()
                .ok()
                .filter(|m| m.permissions().readonly())
                .map(|_| mo_core::overlay::ATTR_READONLY)
                .unwrap_or(0);
            let mtime = md
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let inner = format!("f{i}/{rel}");
            builder.add_file(&inner, &content, attrs, mtime)?;
        }
    }

    // 内部资源：license 与主题图片（__mo__/ 命名空间）
    for (ref_path, inner) in [
        manifest
            .options
            .license
            .as_deref()
            .map(|l| (l, "__mo__/license")),
        manifest
            .theme
            .banner
            .as_deref()
            .map(|b| (b, "__mo__/theme/banner")),
        manifest
            .theme
            .sidebar
            .as_deref()
            .map(|s| (s, "__mo__/theme/sidebar")),
    ]
    .into_iter()
    .flatten()
    {
        let p = base_dir.join(ref_path);
        let content = fs::read(&p).map_err(|e| {
            Error::Io(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("读取资源 {ref_path:?} 失败: {e}"),
            ))
        })?;
        let mtime = fs::metadata(&p)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        builder.add_file(inner, &content, 0, mtime)?;
    }

    // L2 脚本：构建期预编译 + 打包（__mo__/setup.rhai）
    if let Some(script) = &manifest.script {
        let source = match (&script.file, &script.inline) {
            (Some(f), _) => {
                let p = base_dir.join(f);
                fs::read_to_string(&p).map_err(|e| {
                    Error::Io(std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        format!("读取脚本 {f:?} 失败: {e}"),
                    ))
                })?
            }
            (None, Some(inline)) => inline.clone(),
            _ => String::new(),
        };
        mo_script::compile_check(&source).map_err(Error::ManifestParse)?;
        if let Some(f) = &script.file {
            let sp = base_dir.join(f);
            let mtime = fs::metadata(&sp)
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            builder.add_file("__mo__/setup.rhai", source.as_bytes(), 0, mtime)?;
        }
    }
    let out_path = match &opts.out {
        Some(p) => p.clone(),
        None => base_dir.join(format!(
            "{}-{}-setup.exe",
            manifest.app.name, manifest.app.version
        )),
    };
    let mut out_file = fs::File::create(&out_path)?;
    let total_output = builder.write_to(&mut out_file, &template)?;
    out_file.flush()?;

    if let Some(sign_cmd) = &opts.sign {
        let cmd_line = sign_cmd.replace("{out}", &out_path.to_string_lossy());
        // 按 shell 方式执行（cmd /c）
        let status = std::process::Command::new("cmd")
            .arg("/c")
            .arg(&cmd_line)
            .status()
            .map_err(Error::Io)?;
        if !status.success() {
            return Err(Error::Overlay(format!(
                "签名命令失败（{}）: {cmd_line}",
                status.code().unwrap_or(-1)
            )));
        }
    }

    Ok(BuildStats {
        out_path,
        file_count: builder.entry_count(),
        total_input: builder.total_input_bytes(),
        total_output,
    })
}

/// 展开 glob 并返回 (绝对路径, 包内相对路径)，按路径排序。
///
/// 相对路径以 glob 的静态前缀目录为基准：'dist/**/*' 匹配
/// dist/bin/app.exe 时包内路径为 bin/app.exe（与 Inno 语义一致）。
fn collect_rule_files(base_dir: &Path, pattern: &str) -> Result<Vec<(PathBuf, String)>, Error> {
    let full = base_dir.join(pattern);
    let full_str = full.to_string_lossy().replace('\\', "/");
    let paths = glob::glob(&full_str)
        .map_err(|e| Error::Glob(format!("{pattern:?}: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| Error::Glob(format!("{pattern:?}: {e}")))?;

    // glob 静态前缀目录（第一个通配符之前的目录部分）
    let static_prefix = match pattern.find(['*', '?', '[']) {
        Some(i) => &pattern[..i],
        None => pattern,
    };
    let rel_base = match static_prefix.rfind('/') {
        Some(j) => &static_prefix[..j],
        None => "",
    };
    let anchor = base_dir.join(rel_base);

    let mut out = Vec::new();
    for p in paths {
        if !p.is_file() {
            continue;
        }
        let rel = p.strip_prefix(&anchor).map_err(|_| {
            Error::Glob(format!(
                "{pattern:?}: 无法计算相对路径（锚点 {}）",
                anchor.display()
            ))
        })?;
        out.push((p.clone(), rel.to_string_lossy().replace('\\', "/")));
    }
    out.sort_by(|a, b| a.1.cmp(&b.1));
    Ok(out)
}
