//! 主题运行时：accent 色板、横幅/侧图纹理、文案覆盖、有效页面序列。

use crate::strings::{Lang, builtin};
use egui::{Color32, Context, TextureHandle, TextureOptions};
use mo_core::manifest::Manifest;
use std::collections::BTreeMap;

/// 向导页（解析自 theme.pages）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Page {
    Welcome,
    License,
    Dir,
    Components,
    Install,
    Finish,
}

impl Page {
    pub fn from_name(s: &str) -> Option<Self> {
        Some(match s {
            "welcome" => Page::Welcome,
            "license" => Page::License,
            "dir" => Page::Dir,
            "components" => Page::Components,
            "install" => Page::Install,
            "finish" => Page::Finish,
            _ => return None,
        })
    }
}

pub struct ThemeRuntime {
    pub lang: Lang,
    pub accent: Color32,
    pub banner: Option<TextureHandle>,
    pub sidebar: Option<TextureHandle>,
    pub strings: BTreeMap<String, String>,
    /// 有效页面序列（显隐/顺序已应用）。
    pub pages: Vec<Page>,
}

impl ThemeRuntime {
    /// 从清单与包内资源构造。banner/sidebar_bytes 为 __mo__/ 资源原始字节。
    pub fn new(
        manifest: &Manifest,
        ctx: &Context,
        lang: Lang,
        banner_bytes: Option<Vec<u8>>,
        sidebar_bytes: Option<Vec<u8>>,
    ) -> Self {
        let accent = manifest
            .theme
            .accent
            .as_deref()
            .and_then(parse_hex_color)
            .unwrap_or(Color32::from_rgb(0x2E, 0x7C, 0xF6));

        let banner = banner_bytes.and_then(|b| load_texture(ctx, b));
        let sidebar = sidebar_bytes.and_then(|b| load_texture(ctx, b));

        // 页面序列：pages - hide_pages；license 页无许可文件则隐藏；components 无组件则隐藏
        let has_license = manifest.options.license.is_some();
        let has_components = !manifest.components.is_empty();
        let hidden: Vec<String> = manifest.theme.hide_pages.clone();
        let mut pages = Vec::new();
        for name in &manifest.theme.pages {
            if hidden.iter().any(|h| h == name) {
                continue;
            }
            if name == "license" && !has_license {
                continue;
            }
            if name == "components" && !has_components {
                continue;
            }
            if let Some(p) = Page::from_name(name) {
                pages.push(p);
            }
        }
        // 兜底：至少有安装与完成页
        if !pages.contains(&Page::Install) {
            pages.push(Page::Install);
        }
        if !pages.contains(&Page::Finish) {
            pages.push(Page::Finish);
        }

        Self {
            lang,
            accent,
            banner,
            sidebar,
            strings: manifest.theme.strings.clone(),
            pages,
        }
    }

    /// 文案：theme.strings 覆盖 > 内置（lang）> key 本身。
    pub fn tr(&self, key: &str) -> String {
        if let Some(s) = self.strings.get(key) {
            return s.clone();
        }
        builtin(key, self.lang).unwrap_or(key).to_string()
    }

    /// 应用主题到 egui Context（accent 作为高亮色/进度色）。
    pub fn apply_style(&self, ctx: &Context) {
        let mut style = (*ctx.style()).clone();
        let a = self.accent;
        style.visuals.selection.bg_fill = a;
        style.visuals.widgets.hovered.bg_fill = a;
        style.visuals.widgets.active.bg_fill = a;
        style.visuals.widgets.active.weak_bg_fill = a;
        style.visuals.hyperlink_color = a;
        ctx.set_style(style);
    }
}

fn parse_hex_color(s: &str) -> Option<Color32> {
    let s = s.trim();
    if s.len() != 7 || !s.starts_with('#') {
        return None;
    }
    let hex = &s[1..];
    if !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let r = u8::from_str_radix(&hex[0..2], 16).ok()?;
    let g = u8::from_str_radix(&hex[2..4], 16).ok()?;
    let b = u8::from_str_radix(&hex[4..6], 16).ok()?;
    Some(Color32::from_rgb(r, g, b))
}

fn load_texture(ctx: &Context, bytes: Vec<u8>) -> Option<TextureHandle> {
    let img = image::load_from_memory(&bytes).ok()?.to_rgba8();
    let (w, h) = img.dimensions();
    let color = egui::ColorImage::from_rgba_unmultiplied([w as usize, h as usize], &img);
    Some(ctx.load_texture("mo-theme", color, TextureOptions::default()))
}
