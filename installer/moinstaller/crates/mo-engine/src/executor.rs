//! 安装/卸载执行器：事件发射 + 动作日志 + 失败回滚 + 卸载键管理。
#![allow(clippy::permissions_set_readonly_false)] // Windows 专用：覆盖只读文件前清只读位

use crate::actions::{ActionLog, ActionRecord};
use crate::event::{Event, EventBus, Flow, Step};
use crate::win::{env as wine, misc, registry as winreg, shortcut as winshortcut};
use mo_core::manifest::{EnvScope, Manifest, Overwrite, RegRoot, RegValueType, ShortcutDest};
use mo_core::overlay::{ATTR_READONLY, Package};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// 引擎错误（对应退出码语义）。
#[derive(Debug)]
pub enum EngineError {
    /// 致命错误（退出码 1）
    Fatal(String),
    /// 安装失败，已回滚（退出码 2）
    InstallFailed(String),
    /// 磁盘不足（退出码 3）
    DiskFull { need: u64, free: u64 },
    /// 钩子/订阅者中止，已回滚（退出码 4）
    HookAborted(String),
}

impl EngineError {
    pub fn exit_code(&self) -> u8 {
        match self {
            EngineError::Fatal(_) => 1,
            EngineError::InstallFailed(_) => 2,
            EngineError::DiskFull { .. } => 3,
            EngineError::HookAborted(_) => 4,
        }
    }

    pub fn message(&self) -> String {
        match self {
            EngineError::Fatal(m) | EngineError::InstallFailed(m) | EngineError::HookAborted(m) => {
                m.clone()
            }
            EngineError::DiskFull { need, free } => format!(
                "磁盘空间不足：需要 {}，剩余 {}",
                misc_human(*need),
                misc_human(*free)
            ),
        }
    }
}

fn misc_human(n: u64) -> String {
    if n >= 1024 * 1024 * 1024 {
        format!("{:.1} GB", n as f64 / 1073741824.0)
    } else if n >= 1024 * 1024 {
        format!("{:.1} MB", n as f64 / 1048576.0)
    } else {
        format!("{n} B")
    }
}

/// Uninstall 注册表键路径。
pub fn uninstall_key_path(app_id: &str) -> String {
    format!(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id}")
}

/// 卸载键所在 hive：require_admin=true 用 HKLM，否则 HKCU。
pub fn uninstall_root(require_admin: bool) -> RegRoot {
    if require_admin {
        RegRoot::Hklm
    } else {
        RegRoot::Hkcu
    }
}

pub struct Executor {
    pub bus: EventBus,
    pub ctx: crate::event::EngineCtx,
    log: ActionLog,
    /// install.log 路径（{app}/mo-install.log）。
    pub log_path: PathBuf,
    /// 失败后是否回滚（测试可关闭以检验部分安装状态）。
    pub rollback_on_failure: bool,
}

impl Executor {
    pub fn new(bus: EventBus, ctx: crate::event::EngineCtx, log_path: PathBuf) -> Self {
        Self {
            bus,
            ctx,
            log: ActionLog::new(),
            log_path,
            rollback_on_failure: true,
        }
    }

    /// 完整安装流程。
    pub fn install(
        &mut self,
        manifest: &Manifest,
        pkg: &mut Package,
        self_exe: &Path,
    ) -> Result<(), EngineError> {
        match self.install_inner(manifest, pkg, self_exe) {
            Ok(()) => {
                let _ = self.bus.emit(&Event::Exit { code: 0 }, &self.ctx);
                Ok(())
            }
            Err(e) => {
                if self.rollback_on_failure {
                    let keep = BTreeSet::new();
                    self.undo_all(&keep);
                }
                let code = i32::from(e.exit_code());
                let _ = self.bus.emit(&Event::Exit { code }, &self.ctx);
                Err(e)
            }
        }
    }

    fn install_inner(
        &mut self,
        manifest: &Manifest,
        pkg: &mut Package,
        self_exe: &Path,
    ) -> Result<(), EngineError> {
        self.bus
            .emit(&Event::Init, &self.ctx)
            .map_err(EngineError::HookAborted)?;
        self.bus
            .emit(
                &Event::DirChosen {
                    dir: self.ctx.app_dir.clone(),
                },
                &self.ctx,
            )
            .map_err(EngineError::HookAborted)?;

        // 目标目录先落地（GetDiskFreeSpaceExW 需要存在的路径；回滚时逆序删除）。
        // 升级场景目录已存在，仍要记录：否则按本次 log 回放的卸载不会清理它。
        if !self.ctx.app_dir.exists() {
            std::fs::create_dir_all(&self.ctx.app_dir).map_err(|e| {
                EngineError::InstallFailed(format!(
                    "创建目标目录 {}: {e}",
                    self.ctx.app_dir.display()
                ))
            })?;
        }
        self.log.push(ActionRecord::CreatedDir {
            path: self.ctx.app_dir.clone(),
        });

        // 磁盘预检：payload + 卸载程序副本
        let need: u64 = pkg.entries.iter().map(|e| e.orig_size).sum::<u64>()
            + std::fs::metadata(self_exe)
                .map(|m| m.len())
                .unwrap_or(4 * 1024 * 1024)
            + 64 * 1024; // install.log 等杂项余量
        let free = misc::disk_free_bytes(&self.ctx.app_dir).map_err(EngineError::Fatal)?;
        if free < need {
            return Err(EngineError::DiskFull { need, free });
        }

        // ---- Step: files ----
        self.step_begin(Step::Files)?;
        for (i, rule) in manifest.files.iter().enumerate() {
            if !self.ctx.component_selected(rule.component.as_deref()) {
                continue;
            }
            let dst_root = self
                .ctx
                .env
                .expand(&rule.dst)
                .map_err(|e| EngineError::Fatal(format!("files.dst 展开: {e}")))?;
            let prefix = format!("f{i}/");
            for entry in pkg.entries_with_prefix(&prefix) {
                let rel = entry.path[prefix.len()..].to_string();
                let flow = self
                    .bus
                    .emit(&Event::BeforeFile { path: rel.clone() }, &self.ctx)
                    .map_err(EngineError::HookAborted)?;
                let dst = Path::new(&dst_root).join(&rel);
                let skipped = self.apply_overwrite(&dst, rule.overwrite, entry.mtime)
                    || flow == Flow::SkipFile;
                if !skipped {
                    let data = pkg
                        .read_entry(&entry)
                        .map_err(|e| EngineError::InstallFailed(e.to_string()))?;
                    self.write_file(&dst, &data, entry.attrs & ATTR_READONLY != 0)?;
                }
                self.bus
                    .emit(&Event::AfterFile { path: rel }, &self.ctx)
                    .map_err(EngineError::HookAborted)?;
            }
        }
        self.step_end(Step::Files)?;

        // ---- Step: registry ----
        self.step_begin(Step::Registry)?;
        for r in &manifest.registry {
            if !self.ctx.component_selected(r.component.as_deref()) {
                continue;
            }
            let key = self
                .ctx
                .env
                .expand(&r.key)
                .map_err(|e| EngineError::Fatal(format!("registry.key 展开: {e}")))?;
            let value = self
                .ctx
                .env
                .expand(&r.value)
                .map_err(|e| EngineError::Fatal(format!("registry.value 展开: {e}")))?;
            let prev =
                winreg::set_value(r.root, &key, &r.name, &value, r.value_type).map_err(|e| {
                    EngineError::InstallFailed(format!("注册表写入 {key}\\{}: {e}", r.name))
                })?;
            self.log.push(ActionRecord::SetReg {
                root: root_str(r.root).into(),
                key,
                name: r.name.clone(),
                prev,
            });
        }
        self.step_end(Step::Registry)?;

        // ---- Step: env ----
        let mut env_changed = false;
        self.step_begin(Step::Env)?;
        for e in &manifest.env {
            if !self.ctx.component_selected(e.component.as_deref()) {
                continue;
            }
            let value = self
                .ctx
                .env
                .expand(&e.value)
                .map_err(|er| EngineError::Fatal(format!("env.value 展开: {er}")))?;
            let current = wine::read_env(e.scope, &e.name);
            if let Some(new_val) = wine::apply_op(current.as_deref(), &value, e.op) {
                let prev = wine::write_env(e.scope, &e.name, &new_val);
                self.log.push(ActionRecord::SetEnv {
                    scope: scope_str(e.scope).into(),
                    name: e.name.clone(),
                    prev,
                });
                env_changed = true;
            }
        }
        if env_changed {
            wine::broadcast_env_change();
        }
        self.step_end(Step::Env)?;

        // ---- Step: shortcuts ----
        self.step_begin(Step::Shortcuts)?;
        for s in &manifest.shortcuts {
            if !self.ctx.component_selected(s.component.as_deref()) {
                continue;
            }
            let target = self
                .ctx
                .env
                .expand(&s.target)
                .map_err(|er| EngineError::Fatal(format!("shortcuts.target 展开: {er}")))?;
            let dir = match s.dest {
                ShortcutDest::Desktop => self
                    .ctx
                    .env
                    .expand("{userdesktop}")
                    .map_err(|e| EngineError::Fatal(e.to_string()))?,
                ShortcutDest::StartMenu => self
                    .ctx
                    .env
                    .expand("{group}")
                    .map_err(|e| EngineError::Fatal(e.to_string()))?,
            };
            let lnk = Path::new(&dir).join(format!("{}.lnk", s.name));
            if let Some(parent) = lnk.parent() {
                std::fs::create_dir_all(parent).map_err(|er| {
                    EngineError::InstallFailed(format!("创建目录 {}: {er}", parent.display()))
                })?;
                // 升级时组目录已存在也记录，保证卸载回放能清理
                self.log.push(ActionRecord::CreatedDir {
                    path: parent.to_path_buf(),
                });
            }
            winshortcut::create_lnk(
                &lnk,
                Path::new(&target),
                Some(&self.ctx.app_dir),
                &self.ctx.app_name,
            )
            .map_err(|er| {
                EngineError::InstallFailed(format!("创建快捷方式 {}: {er}", lnk.display()))
            })?;
            self.log.push(ActionRecord::CreatedShortcut { path: lnk });
        }
        self.step_end(Step::Shortcuts)?;

        // ---- Step: finalize ----
        self.step_begin(Step::Finalize)?;
        let uninstall_exe = self.ctx.app_dir.join("mo-uninstall.exe");
        std::fs::copy(self_exe, &uninstall_exe)
            .map_err(|e| EngineError::InstallFailed(format!("复制卸载程序: {e}")))?;
        self.log.push(ActionRecord::WroteFile {
            path: uninstall_exe.clone(),
        });

        let uroot = uninstall_root(manifest.options.require_admin);
        let ukey = uninstall_key_path(&manifest.app.id);
        let iname = winreg::set_value(
            uroot,
            &ukey,
            "DisplayName",
            &manifest.app.name,
            RegValueType::String,
        )
        .map_err(|e| EngineError::InstallFailed(format!("写 Uninstall 键: {e}")))?;
        let _ = iname;
        let _ = winreg::set_value(
            uroot,
            &ukey,
            "DisplayVersion",
            &manifest.app.version,
            RegValueType::String,
        );
        let _ = winreg::set_value(
            uroot,
            &ukey,
            "Publisher",
            &manifest.app.publisher,
            RegValueType::String,
        );
        let _ = winreg::set_value(
            uroot,
            &ukey,
            "InstallLocation",
            &self.ctx.app_dir.to_string_lossy(),
            RegValueType::String,
        );
        let _ = winreg::set_value(
            uroot,
            &ukey,
            "UninstallString",
            &format!("\"{}\"", uninstall_exe.display()),
            RegValueType::String,
        );
        if let Some(icon_tpl) = &manifest.uninstall.display_icon {
            let icon = self
                .ctx
                .env
                .expand(icon_tpl)
                .map_err(|e| EngineError::Fatal(format!("uninstall.display_icon 展开: {e}")))?;
            let _ = winreg::set_value(uroot, &ukey, "DisplayIcon", &icon, RegValueType::String);
        }
        self.log.push(ActionRecord::WroteUninstallKey {
            root: root_str(uroot).into(),
            key: ukey,
        });

        // install.log 落盘
        if let Some(parent) = self.log_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        std::fs::write(&self.log_path, self.log.to_jsonl())
            .map_err(|e| EngineError::InstallFailed(format!("写 install.log: {e}")))?;
        self.step_end(Step::Finalize)?;

        self.bus
            .emit(&Event::AfterInstall, &self.ctx)
            .map_err(EngineError::HookAborted)?;
        Ok(())
    }

    fn step_begin(&mut self, step: Step) -> Result<(), EngineError> {
        self.bus
            .emit(&Event::BeforeStep(step), &self.ctx)
            .map_err(EngineError::HookAborted)?;
        Ok(())
    }

    fn step_end(&mut self, step: Step) -> Result<(), EngineError> {
        self.bus
            .emit(&Event::AfterStep(step), &self.ctx)
            .map_err(EngineError::HookAborted)?;
        Ok(())
    }

    /// 写单文件（含父目录创建、只读清位、占用重试）。
    fn write_file(&mut self, dst: &Path, data: &[u8], readonly: bool) -> Result<(), EngineError> {
        if let Some(parent) = dst.parent()
            && !parent.exists()
        {
            std::fs::create_dir_all(parent).map_err(|e| {
                EngineError::InstallFailed(format!("创建目录 {}: {e}", parent.display()))
            })?;
            self.log.push(ActionRecord::CreatedDir {
                path: parent.to_path_buf(),
            });
        }
        if dst.exists()
            && let Ok(md) = std::fs::metadata(dst)
        {
            let mut perm = md.permissions();
            perm.set_readonly(false);
            let _ = std::fs::set_permissions(dst, perm);
        }
        // 文件占用重试（静默语义：重试 3 次）
        let mut last_err = None;
        for attempt in 0..3 {
            let r = std::fs::write(dst, data);
            match r {
                Ok(()) => {
                    last_err = None;
                    break;
                }
                Err(e) => {
                    last_err = Some(e);
                    if attempt < 2 {
                        std::thread::sleep(std::time::Duration::from_millis(500));
                    }
                }
            }
        }
        if let Some(e) = last_err {
            return Err(EngineError::InstallFailed(format!(
                "写文件 {}: {e}",
                dst.display()
            )));
        }
        if readonly && let Ok(md) = std::fs::metadata(dst) {
            let mut perm = md.permissions();
            perm.set_readonly(true);
            let _ = std::fs::set_permissions(dst, perm);
        }
        self.log.push(ActionRecord::WroteFile {
            path: dst.to_path_buf(),
        });
        Ok(())
    }

    /// 应用覆盖策略。返回 true = 跳过。
    fn apply_overwrite(&self, dst: &Path, policy: Overwrite, src_mtime: u64) -> bool {
        match policy {
            Overwrite::Overwrite => false,
            Overwrite::SkipIfExist => dst.exists(),
            Overwrite::SkipIfNewer => {
                let Ok(md) = std::fs::metadata(dst) else {
                    return false;
                };
                let dst_mtime = md
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                // 目标不比源旧 → 跳过
                dst_mtime >= src_mtime
            }
        }
    }

    /// 完整卸载流程（self_exe 即 {app}/mo-uninstall.exe）。
    pub fn uninstall(&mut self, manifest: &Manifest, self_exe: &Path) -> Result<(), EngineError> {
        self.bus
            .emit(&Event::BeforeUninstall, &self.ctx)
            .map_err(EngineError::HookAborted)?;

        // 读 install.log
        let text = std::fs::read_to_string(&self.log_path)
            .map_err(|e| EngineError::Fatal(format!("读 install.log 失败: {e}")))?;
        let log = ActionLog::parse_jsonl(&text)
            .map_err(|e| EngineError::Fatal(format!("install.log: {e}")))?;
        self.log = log;

        // keep 集合（常量展开后归一）
        let mut keep = BTreeSet::new();
        for k in &manifest.uninstall.keep {
            let p = self
                .ctx
                .env
                .expand(k)
                .map_err(|e| EngineError::Fatal(format!("uninstall.keep 展开: {e}")))?;
            keep.insert(norm_path(&p));
        }
        keep.insert(norm_path(&self.log_path.to_string_lossy()));

        self.undo_all(&keep);

        // 删除 Uninstall 键
        let uroot = uninstall_root(manifest.options.require_admin);
        let ukey = uninstall_key_path(&manifest.app.id);
        winreg::delete_tree(uroot, &ukey);

        // 删除 install.log 自身（keep 集合中排除自己）
        let _ = std::fs::remove_file(&self.log_path);
        if manifest.uninstall.remove_app_dir {
            // Inno 语义：递归删除整个 {app}（keep 除外；{app} 本身在 keep 中则不动）。
            // 卸载程序自身正在运行会被锁：跳过自身逐项删除，收尾 self_delete
            // 延迟删自身后再 rmdir 清目录。
            let app_norm = norm_path(&self.ctx.app_dir.to_string_lossy());
            let keep_root = keep.contains(&app_norm);
            if !keep_root {
                if let Ok(entries) = std::fs::read_dir(&self.ctx.app_dir) {
                    for e in entries.flatten() {
                        let p = e.path();
                        if p == self_exe {
                            continue;
                        }
                        if p.is_dir() {
                            let _ = std::fs::remove_dir_all(&p);
                        } else {
                            let _ = std::fs::remove_file(&p);
                        }
                    }
                }
            }
        } else {
            // 保守语义：仅删除已空目录
            let _ = std::fs::remove_dir(&self.ctx.app_dir);
        }

        self.bus
            .emit(&Event::AfterUninstall, &self.ctx)
            .map_err(EngineError::HookAborted)?;

        // Shell 目录（开始菜单组等）可能被 explorer 瞬时句柄占用：
        // 对安装期创建的目录做延迟 rmdir（仅删空目录，安全）。
        let created_dirs: Vec<String> = self
            .log
            .iter_rollback()
            .filter_map(|r| match r {
                ActionRecord::CreatedDir { path } => Some(path.to_string_lossy().into_owned()),
                _ => None,
            })
            .collect();
        // 每目录一条独立命令：cmd 的 || 右侧会吞掉后续 & 子句，
        // 链式拼接会在首目录删除成功时短路跳过其余目录。
        use std::os::windows::process::CommandExt;
        for d in &created_dirs {
            let cmd = format!(
                "/c ping -n 3 127.0.0.1 >nul & rmdir \"{d}\" & ping -n 2 127.0.0.1 >nul & rmdir \"{d}\""
            );
            let _ = std::process::Command::new("cmd").raw_arg(cmd).spawn();
        }

        // 自删（延迟）
        misc::self_delete(self_exe);
        let _ = self.bus.emit(&Event::Exit { code: 0 }, &self.ctx);
        Ok(())
    }

    /// 逆序撤销日志中的全部动作（keep 集内的文件路径跳过）。
    fn undo_all(&mut self, keep: &BTreeSet<String>) {
        let records: Vec<ActionRecord> = self.log.iter_rollback().cloned().collect();
        let mut env_touched = false;
        for r in records {
            match r {
                ActionRecord::WroteFile { path } | ActionRecord::CreatedShortcut { path } => {
                    if keep.contains(&norm_path(&path.to_string_lossy())) {
                        continue;
                    }
                    if path.exists() {
                        if let Ok(md) = std::fs::metadata(&path) {
                            let mut perm = md.permissions();
                            perm.set_readonly(false);
                            let _ = std::fs::set_permissions(&path, perm);
                        }
                        let _ = std::fs::remove_file(&path);
                    }
                }
                ActionRecord::CreatedDir { path } => {
                    // 非空则失败忽略；瞬时句柄（索引/杀软）短暂重试
                    for attempt in 0..3 {
                        if std::fs::remove_dir(&path).is_ok() {
                            break;
                        }
                        if attempt < 2 {
                            std::thread::sleep(std::time::Duration::from_millis(100));
                        }
                    }
                }
                ActionRecord::SetReg {
                    root,
                    key,
                    name,
                    prev,
                } => {
                    let Some(root) = winreg::root_from_str(&root) else {
                        continue;
                    };
                    match prev {
                        Some(v) => {
                            let _ = winreg::set_value(root, &key, &name, &v, RegValueType::String);
                        }
                        None => {
                            let _ = winreg::delete_value(root, &key, &name);
                        }
                    }
                }
                ActionRecord::SetEnv { scope, name, prev } => {
                    let Some(scope) = scope_from_str(&scope) else {
                        continue;
                    };
                    match prev {
                        Some(v) => {
                            wine::write_env(scope, &name, &v);
                        }
                        None => {
                            wine::delete_env(scope, &name);
                        }
                    }
                    env_touched = true;
                }
                ActionRecord::WroteUninstallKey { root, key } => {
                    if let Some(root) = winreg::root_from_str(&root) {
                        winreg::delete_tree(root, &key);
                    }
                }
            }
        }
        if env_touched {
            wine::broadcast_env_change();
        }
    }
}

fn norm_path(s: &str) -> String {
    s.to_ascii_lowercase().replace('\\', "/")
}

fn root_str(r: RegRoot) -> &'static str {
    match r {
        RegRoot::Hkcu => "hkcu",
        RegRoot::Hklm => "hklm",
    }
}

fn scope_str(s: EnvScope) -> &'static str {
    match s {
        EnvScope::User => "user",
        EnvScope::Machine => "machine",
    }
}

fn scope_from_str(s: &str) -> Option<EnvScope> {
    match s {
        "user" => Some(EnvScope::User),
        "machine" => Some(EnvScope::Machine),
        _ => None,
    }
}
