//! egui 安装向导：欢迎 → 许可 → 目录 → 组件 → 安装 → 完成。
//!
//! 引擎跑在工作线程，经 channel 推送进度；GUI 是事件总线的另一个订阅者视角。

use crate::picker;
use crate::strings::Lang;
use crate::theme::{Page, ThemeRuntime};
use egui::{Layout, RichText};
use mo_core::constants::ConstEnv;
use mo_core::manifest::Manifest;
use mo_core::overlay::Package;
use mo_engine::event::{Decision, EngineCtx, Event, EventBus, Subscriber};
use mo_engine::executor::{EngineError, Executor};
use mo_engine::hook::HookRunner;
use mo_engine::win::misc;
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender};

/// 安装线程 → UI 的消息。
pub enum UiMsg {
    Step(String),
    File {
        path: String,
        done: usize,
        total: usize,
    },
    Done(Result<(), EngineError>),
}

pub struct WizardApp {
    pub manifest: Manifest,
    pub self_exe: PathBuf,
    pub theme: ThemeRuntime,
    pub banner_bytes: Option<Vec<u8>>,
    pub sidebar_bytes: Option<Vec<u8>>,
    pub dir: String,
    pub components: BTreeMap<String, bool>,
    pub license_text: String,
    pub license_accepted: bool,
    pub page: Page,
    pub install_rx: Option<Receiver<UiMsg>>,
    pub progress: Progress,
    pub result: Option<Result<(), EngineError>>,
    pub run_after: bool,
    pub closed: bool,
    /// 卸载模式
    pub uninstall_mode: bool,
    pub uninstall_keep: bool,
}

#[derive(Default, Clone)]
pub struct Progress {
    pub step: String,
    pub current_file: String,
    pub files_done: usize,
    pub files_total: usize,
    pub finished: bool,
}

impl WizardApp {
    /// GUI 安装向导。
    #[allow(clippy::too_many_arguments)]
    pub fn install_wizard(
        manifest: Manifest,
        self_exe: PathBuf,
        default_dir: String,
        banner: Option<Vec<u8>>,
        sidebar: Option<Vec<u8>>,
        license_text: String,
    ) -> Self {
        let theme = ThemeRuntime::new(
            &manifest,
            &egui::Context::default(),
            Lang::detect(),
            None,
            None,
        );
        let components = manifest
            .components
            .iter()
            .map(|c| (c.id.clone(), c.default_selected()))
            .collect();
        Self {
            page: theme.pages.first().copied().unwrap_or(Page::Install),
            theme,
            manifest,
            self_exe,
            banner_bytes: banner,
            sidebar_bytes: sidebar,
            dir: default_dir,
            components,
            license_text,
            license_accepted: false,
            install_rx: None,
            progress: Progress::default(),
            result: None,
            run_after: false,
            closed: false,
            uninstall_mode: false,
            uninstall_keep: true,
        }
    }

    /// GUI 卸载确认。
    pub fn uninstaller(manifest: Manifest, self_exe: PathBuf) -> Self {
        let theme = ThemeRuntime::new(
            &manifest,
            &egui::Context::default(),
            Lang::detect(),
            None,
            None,
        );
        Self {
            page: Page::Finish,
            theme,
            manifest,
            self_exe,
            banner_bytes: None,
            sidebar_bytes: None,
            dir: String::new(),
            components: BTreeMap::new(),
            license_text: String::new(),
            license_accepted: false,
            install_rx: None,
            progress: Progress::default(),
            result: None,
            run_after: false,
            closed: false,
            uninstall_mode: true,
            uninstall_keep: true,
        }
    }

    fn next_page(&self) -> Option<Page> {
        let idx = self.theme.pages.iter().position(|p| *p == self.page)?;
        self.theme.pages.get(idx + 1).copied()
    }

    fn prev_page(&self) -> Option<Page> {
        let idx = self.theme.pages.iter().position(|p| *p == self.page)?;
        if idx == 0 {
            None
        } else {
            self.theme.pages.get(idx - 1).copied()
        }
    }

    fn start_install(&mut self) {
        let (tx, rx) = std::sync::mpsc::channel();
        self.install_rx = Some(rx);
        self.page = Page::Install;

        let manifest = self.manifest.clone();
        let exe = self.self_exe.clone();
        let target = PathBuf::from(self.dir.trim().trim_matches('"').to_string());
        let selected: BTreeSet<String> = self
            .components
            .iter()
            .filter(|(id, on)| {
                **on || manifest
                    .components
                    .iter()
                    .any(|c| &c.id == *id && c.required)
            })
            .map(|(id, _)| id.clone())
            .collect();
        std::thread::spawn(move || {
            if let Err(e) = run_install_thread(manifest, exe, target, selected, &tx) {
                let _ = tx.send(UiMsg::Done(Err(e)));
            }
        });
    }

    fn start_uninstall(&mut self) {
        let (tx, rx) = std::sync::mpsc::channel();
        self.install_rx = Some(rx);
        let mut manifest = self.manifest.clone();
        if !self.uninstall_keep {
            manifest.uninstall.keep.clear();
        }
        let exe = self.self_exe.clone();
        std::thread::spawn(move || {
            let r = run_uninstall_thread(&manifest, &exe, &tx);
            let _ = tx.send(UiMsg::Done(r));
        });
    }

    fn poll_install(&mut self) {
        let Some(rx) = &self.install_rx else { return };
        while let Ok(msg) = rx.try_recv() {
            match msg {
                UiMsg::Step(s) => self.progress.step = s,
                UiMsg::File { path, done, total } => {
                    self.progress.current_file = path;
                    self.progress.files_done = done;
                    self.progress.files_total = total;
                }
                UiMsg::Done(r) => {
                    self.progress.finished = true;
                    self.result = Some(r);
                    self.page = Page::Finish;
                }
            }
        }
    }
}

/// 进度订阅者：把事件压缩成 UiMsg 发给 UI。
struct UiProgress {
    tx: Sender<UiMsg>,
    files_total: usize,
    files_done: usize,
}

impl Subscriber for UiProgress {
    fn id(&self) -> &str {
        "ui-progress"
    }
    fn on_event(&mut self, event: &Event, _ctx: &EngineCtx) -> Decision {
        match event {
            Event::BeforeStep(s) => {
                let _ = self.tx.send(UiMsg::Step(s.name().to_string()));
            }
            Event::AfterFile { .. } => {
                self.files_done += 1;
                let _ = self.tx.send(UiMsg::File {
                    path: String::new(),
                    done: self.files_done,
                    total: self.files_total,
                });
            }
            Event::BeforeFile { path } => {
                let _ = self.tx.send(UiMsg::File {
                    path: path.clone(),
                    done: self.files_done,
                    total: self.files_total,
                });
            }
            _ => {}
        }
        Decision::Continue
    }
}

fn run_install_thread(
    manifest: Manifest,
    exe: PathBuf,
    target: PathBuf,
    selected: BTreeSet<String>,
    tx: &Sender<UiMsg>,
) -> Result<(), EngineError> {
    let mut pkg =
        Package::open(&exe).map_err(|e| EngineError::Fatal(format!("解析安装包: {e}")))?;

    let _mutex = misc::SingleInstance::new(&format!("MoInstaller.{}", manifest.app.id))
        .ok_or_else(|| EngineError::Fatal("安装程序已在运行".into()))?;

    let base_env = ConstEnv::from_process_env();
    let env = base_env.with_app(&target, &manifest.app.name);
    let total: usize = pkg.entries.len();
    let selected_vec: Vec<String> = selected.iter().cloned().collect();
    let ctx = EngineCtx {
        app_dir: target.clone(),
        app_name: manifest.app.name.clone(),
        app_id: manifest.app.id.clone(),
        version: manifest.app.version.clone(),
        silent: false,
        selected_components: selected,
        env,
    };
    // 与静默路径一致：L1 钩子 + L2 脚本 + UI 进度
    let script = super::script_source(&manifest, &mut pkg);
    let mut bus = super::build_bus(&manifest, false, selected_vec, script);
    bus.subscribe(Box::new(UiProgress {
        tx: tx.clone(),
        files_total: total,
        files_done: 0,
    }));
    let mut executor = Executor::new(bus, ctx, target.join("mo-install.log"));
    executor.install(&manifest, &mut pkg, &exe)
}

fn run_uninstall_thread(
    manifest: &Manifest,
    exe: &std::path::Path,
    tx: &Sender<UiMsg>,
) -> Result<(), EngineError> {
    let app_dir = exe
        .parent()
        .ok_or_else(|| EngineError::Fatal("无法定位安装目录".into()))?
        .to_path_buf();
    let env = ConstEnv::from_process_env().with_app(&app_dir, &manifest.app.name);
    let ctx = EngineCtx {
        app_dir: app_dir.clone(),
        app_name: manifest.app.name.clone(),
        app_id: manifest.app.id.clone(),
        version: manifest.app.version.clone(),
        silent: false,
        selected_components: BTreeSet::new(),
        env,
    };
    let mut bus = EventBus::new();
    if !manifest.hooks.is_empty() {
        bus.subscribe(Box::new(HookRunner::new(manifest.hooks.clone())));
    }
    let _ = tx.send(UiMsg::Step("uninstall".to_string()));
    let mut executor = Executor::new(bus, ctx, app_dir.join("mo-install.log"));
    executor.uninstall(manifest, exe)
}

/// 运行安装 GUI。返回 (是否成功, 成功后要运行的程序（已展开）)。
pub fn run_install_gui(mut app: WizardApp) -> (bool, Option<String>) {
    let mut native = eframe::NativeOptions::default();
    let (w, h) = app
        .manifest
        .theme
        .window
        .as_ref()
        .map(|w| (w.width as f32, w.height as f32))
        .unwrap_or((660.0, 460.0));
    native.viewport.inner_size = Some(egui::vec2(w, h));
    let outcome = std::rc::Rc::new(std::cell::RefCell::new((false, None::<String>)));
    let out2 = outcome.clone();
    let _ = eframe::run_native(
        "mo-setup",
        native,
        Box::new(move |cc| {
            let ctx = cc.egui_ctx.clone();
            // 用真实 ctx 重建主题（纹理）
            let theme = ThemeRuntime::new(
                &app.manifest,
                &ctx,
                app.theme.lang,
                app.banner_bytes.take(),
                app.sidebar_bytes.take(),
            );
            let pages = theme.pages.clone();
            let lang = theme.lang;
            app.theme = theme;
            if !app.uninstall_mode && !pages.contains(&app.page) {
                app.page = pages.first().copied().unwrap_or(Page::Install);
            }
            let _ = lang;
            Ok(Box::new(GuiWrap {
                app,
                outcome: GuiOutcome::Install(out2),
            }))
        }),
    );
    let (ok, prog) = outcome.borrow().clone();
    (ok, prog)
}

/// 运行卸载 GUI。返回是否成功。
pub fn run_uninstall_gui(mut app: WizardApp) -> bool {
    let mut native = eframe::NativeOptions::default();
    native.viewport.inner_size = Some(egui::vec2(520.0, 260.0));
    let outcome = std::rc::Rc::new(std::cell::RefCell::new(false));
    let out2 = outcome.clone();
    let _ = eframe::run_native(
        "mo-uninstall",
        native,
        Box::new(move |cc| {
            let ctx = cc.egui_ctx.clone();
            let theme = ThemeRuntime::new(
                &app.manifest,
                &ctx,
                app.theme.lang,
                app.banner_bytes.take(),
                app.sidebar_bytes.take(),
            );
            app.theme = theme;
            Ok(Box::new(GuiWrap {
                app,
                outcome: GuiOutcome::Uninstall(out2),
            }))
        }),
    );
    *outcome.borrow()
}

enum GuiOutcome {
    Install(std::rc::Rc<std::cell::RefCell<(bool, Option<String>)>>),
    Uninstall(std::rc::Rc<std::cell::RefCell<bool>>),
}

struct GuiWrap {
    app: WizardApp,
    outcome: GuiOutcome,
}

impl eframe::App for GuiWrap {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.app.poll_install();
        self.app.theme.apply_style(ctx);
        egui::CentralPanel::default().show(ctx, |ui| {
            self.app.ui(ui);
        });
        if self.app.closed {
            match &self.outcome {
                GuiOutcome::Install(o) => {
                    let ok = matches!(self.app.result, Some(Ok(())));
                    let prog = if ok && self.app.run_after {
                        self.app
                            .manifest
                            .run
                            .after
                            .as_deref()
                            .and_then(|p| self.app.theme_env().expand(p).ok())
                    } else {
                        None
                    };
                    *o.borrow_mut() = (ok, prog);
                }
                GuiOutcome::Uninstall(o) => {
                    *o.borrow_mut() = matches!(self.app.result, Some(Ok(())));
                }
            }
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }
}

impl WizardApp {
    fn theme_env(&self) -> ConstEnv {
        ConstEnv::from_process_env()
            .with_app(std::path::Path::new(&self.dir), &self.manifest.app.name)
    }

    fn ui(&mut self, ui: &mut egui::Ui) {
        let banner_h = self.theme.banner.as_ref().map(|t| {
            let size = t.size_vec2();
            let scale = ui.available_width() / size.x;
            size.y * scale
        });
        let has_sidebar = self.theme.sidebar.is_some();
        egui::SidePanel::left("mo-sidebar")
            .min_width(0.0)
            .max_width(if has_sidebar { 160.0 } else { 0.0 })
            .show_inside(ui, |ui| {
                if let Some(tex) = &self.theme.sidebar {
                    let size = tex.size_vec2();
                    let scale = (ui.available_width() / size.x).min(ui.available_height() / size.y);
                    ui.image((tex.id(), size * scale));
                }
            });
        egui::TopBottomPanel::top("mo-banner")
            .exact_height(banner_h.unwrap_or(0.0))
            .show_inside(ui, |ui| {
                if let Some(tex) = &self.theme.banner {
                    let size = tex.size_vec2();
                    let scale = ui.available_width() / size.x;
                    ui.image((tex.id(), size * scale));
                }
            });
        egui::TopBottomPanel::bottom("mo-buttons")
            .exact_height(44.0)
            .show_inside(ui, |ui| {
                ui.with_layout(Layout::right_to_left(egui::Align::Center), |ui| {
                    self.buttons(ui);
                });
            });
        egui::CentralPanel::default().show_inside(ui, |ui| {
            self.page_body(ui);
        });
    }

    fn buttons(&mut self, ui: &mut egui::Ui) {
        let cancel_label = self.theme.tr("wizard.btn.cancel");
        let back_label = self.theme.tr("wizard.btn.back");
        let next_label_next = self.theme.tr("wizard.btn.next");
        let next_label_install = self.theme.tr("wizard.btn.install");
        let next_label_uninstall = self.theme.tr("wizard.btn.uninstall");
        match self.page {
            Page::Install if !self.uninstall_mode => {
                if ui.button(&cancel_label).clicked() {
                    self.closed = true;
                }
            }
            Page::Finish => {
                if self.uninstall_mode && self.result.is_none() {
                    // 卸载确认页：卸载 / 取消
                    if ui
                        .button(egui::RichText::new(&next_label_uninstall).strong())
                        .clicked()
                    {
                        self.start_uninstall();
                    }
                    if ui.button(&cancel_label).clicked() {
                        self.closed = true;
                    }
                } else {
                    let close_label = self.theme.tr("wizard.btn.close");
                    if ui.button(&close_label).clicked() {
                        self.closed = true;
                    }
                }
            }
            _ => {
                let next_label = match self.next_page() {
                    Some(Page::Install) => {
                        if self.uninstall_mode {
                            next_label_uninstall.clone()
                        } else {
                            next_label_install.clone()
                        }
                    }
                    _ => next_label_next.clone(),
                };
                let can_next = self.can_proceed();
                if ui
                    .add_enabled(
                        can_next,
                        egui::Button::new(RichText::new(&next_label).strong()),
                    )
                    .clicked()
                {
                    if self.next_page() == Some(Page::Install) && !self.uninstall_mode {
                        self.start_install();
                    } else if let Some(p) = self.next_page() {
                        self.page = p;
                    }
                }
                if ui.button(&back_label).clicked()
                    && let Some(p) = self.prev_page()
                {
                    self.page = p;
                }
                if ui.button(&cancel_label).clicked() {
                    self.closed = true;
                }
            }
        }
    }

    fn can_proceed(&self) -> bool {
        match self.page {
            Page::License => self.license_accepted,
            Page::Dir => !self.dir.trim().is_empty(),
            _ => true,
        }
    }

    fn page_body(&mut self, ui: &mut egui::Ui) {
        let t = &self.theme;
        match self.page {
            Page::Welcome => {
                ui.heading(t.tr("wizard.welcome.title"));
                ui.add_space(8.0);
                ui.label(
                    RichText::new(format!(
                        "{} {} ({})",
                        self.manifest.app.name,
                        self.manifest.app.version,
                        self.manifest.app.publisher
                    ))
                    .strong(),
                );
                ui.add_space(6.0);
                ui.label(t.tr("wizard.welcome.text"));
            }
            Page::License => {
                ui.heading(t.tr("wizard.license.title"));
                ui.add_space(6.0);
                ui.label(t.tr("wizard.license.prompt"));
                ui.add_space(4.0);
                egui::ScrollArea::vertical()
                    .max_height(ui.available_height() - 34.0)
                    .show(ui, |ui| {
                        ui.add(
                            egui::TextEdit::multiline(&mut self.license_text.as_str())
                                .desired_width(f32::INFINITY)
                                .interactive(false),
                        );
                    });
                ui.checkbox(&mut self.license_accepted, t.tr("wizard.license.accept"));
            }
            Page::Dir => {
                ui.heading(t.tr("wizard.dir.title"));
                ui.add_space(6.0);
                ui.label(t.tr("wizard.dir.prompt"));
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    let edit = egui::TextEdit::singleline(&mut self.dir)
                        .desired_width(ui.available_width() - 90.0);
                    ui.add(edit);
                    if ui.button(t.tr("wizard.dir.browse")).clicked()
                        && let Some(p) = picker::pick_folder()
                    {
                        self.dir = p.to_string_lossy().into_owned();
                    }
                });
                if let Ok(free) = misc::disk_free_bytes(std::path::Path::new(&self.dir)) {
                    ui.add_space(8.0);
                    ui.label(format!("{}: {}", t.tr("wizard.dir.freespace"), human(free)));
                }
            }
            Page::Components => {
                ui.heading(t.tr("wizard.components.title"));
                ui.add_space(6.0);
                ui.label(t.tr("wizard.components.prompt"));
                ui.add_space(8.0);
                let comps = self.manifest.components.clone();
                for c in comps {
                    let on = self
                        .components
                        .entry(c.id.clone())
                        .or_insert(c.default_selected());
                    let mut v = *on;
                    ui.add_enabled(!c.required, egui::Checkbox::new(&mut v, &c.name));
                    *on = v || c.required;
                }
            }
            Page::Install => {
                ui.heading(t.tr("wizard.install.title"));
                ui.add_space(6.0);
                let frac = if self.progress.files_total > 0 {
                    self.progress.files_done as f32 / self.progress.files_total as f32
                } else {
                    0.0
                };
                let bar = egui::ProgressBar::new(frac.clamp(0.0, 1.0)).text(format!(
                    "{} / {}",
                    self.progress.files_done, self.progress.files_total
                ));
                ui.add(bar);
                ui.add_space(6.0);
                ui.label(format!(
                    "[{}] {}",
                    self.progress.step, self.progress.current_file
                ));
            }
            Page::Finish => {
                if self.uninstall_mode {
                    ui.heading(t.tr("wizard.uninstall.title"));
                    match &self.result {
                        Some(Ok(())) => {
                            ui.label(t.tr("wizard.uninstall.done"));
                        }
                        Some(Err(e)) => {
                            ui.label(format!("{}\n{}", t.tr("wizard.finish.failed"), e.message()));
                        }
                        None => {
                            ui.label(t.tr("wizard.uninstall.confirm"));
                            ui.add_space(8.0);
                            ui.checkbox(
                                &mut self.uninstall_keep,
                                t.tr("wizard.uninstall.keepdata"),
                            );
                        }
                    }
                } else {
                    match &self.result {
                        Some(Ok(())) => {
                            ui.heading(t.tr("wizard.finish.title"));
                            ui.add_space(6.0);
                            ui.label(t.tr("wizard.finish.text"));
                            if self.manifest.run.after.is_some() {
                                ui.add_space(8.0);
                                ui.checkbox(
                                    &mut self.run_after,
                                    format!(
                                        "{} {}",
                                        t.tr("wizard.finish.run"),
                                        self.manifest.app.name
                                    ),
                                );
                            }
                        }
                        Some(Err(e)) => {
                            ui.heading(t.tr("wizard.finish.failed"));
                            ui.add_space(6.0);
                            ui.label(e.message());
                        }
                        None => {
                            ui.heading(t.tr("wizard.install.title"));
                        }
                    }
                }
            }
        }
    }
}

fn human(n: u64) -> String {
    if n >= 1024 * 1024 * 1024 {
        format!("{:.1} GB", n as f64 / 1073741824.0)
    } else if n >= 1024 * 1024 {
        format!("{:.1} MB", n as f64 / 1048576.0)
    } else if n >= 1024 {
        format!("{:.1} KB", n as f64 / 1024.0)
    } else {
        format!("{n} B")
    }
}
