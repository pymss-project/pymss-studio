//! setup.exe 尾部 overlay 包格式。
//!
//! 布局（自文件尾反向定位），从文件头到文件尾依次是：
//! 1. 原始 PE（mo-setup 模板）
//! 2. 数据区：每条目为 header(path_len u16le + path utf8 + orig_size u64le
//!    + crc32 u32le + attrs u32le) 后跟一个压缩帧
//! 3. payload 索引：count u32le + 每条目 (data_off u64le, data_len u64le)，
//!    整体为一个 zstd 帧
//! 4. manifest：canonical JSON 的一个 zstd 帧
//! 5. footer 32 字节：magic "MOIS" + fmt_ver u32 + flags u32 + manifest_off u64
//!    + index_off u64 + total_crc u32（均小端）
//!
//! 包内路径命名空间：'f{N}/...' 是 files 规则 N 的 payload；
//! '__mo__/...' 是内部资源（license、主题图片、脚本等）。
//!
//! total_crc 覆盖索引与 manifest 两个 zstd 段（按文件中出现顺序），
//! 刻意不含 PE——将来 Authenticode 签名会修改 PE 头，不得因此失效。
//! 数据区完整性由每条目的内容 CRC32 保证。

use crate::manifest::Manifest;
use crate::{Error, Result};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const MAGIC: [u8; 4] = *b"MOIS";
pub const FOOTER_SIZE: u64 = 32;
/// 当前格式版本。
pub const FMT_VER: u32 = 2;

/// attrs 位标志：源文件只读。
pub const ATTR_READONLY: u32 = 0x1;
/// attrs 位标志：数据未压缩（store）。
pub const ATTR_STORED: u32 = 0x2;

/// 尾部 32 字节 footer。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Footer {
    pub fmt_ver: u32,
    pub flags: u32,
    /// manifest zstd 帧的绝对偏移。
    pub manifest_off: u64,
    /// 索引 zstd 帧的绝对偏移。
    pub index_off: u64,
    /// CRC32(index_zstd || manifest_zstd)。
    pub total_crc: u32,
}

impl Footer {
    pub fn to_bytes(self) -> [u8; 32] {
        let mut b = [0u8; 32];
        b[0..4].copy_from_slice(&MAGIC);
        b[4..8].copy_from_slice(&self.fmt_ver.to_le_bytes());
        b[8..12].copy_from_slice(&self.flags.to_le_bytes());
        b[12..20].copy_from_slice(&self.manifest_off.to_le_bytes());
        b[20..28].copy_from_slice(&self.index_off.to_le_bytes());
        b[28..32].copy_from_slice(&self.total_crc.to_le_bytes());
        b
    }

    pub fn from_bytes(b: &[u8; 32]) -> Result<Self> {
        if b[0..4] != MAGIC {
            return Err(Error::Overlay(
                "magic 不匹配：文件不含 MoInstaller overlay".into(),
            ));
        }
        let fmt_ver = u32::from_le_bytes(b[4..8].try_into().unwrap());
        if fmt_ver != FMT_VER {
            return Err(Error::Overlay(format!(
                "不支持的格式版本 {fmt_ver}（当前支持 {FMT_VER}）"
            )));
        }
        Ok(Self {
            fmt_ver,
            flags: u32::from_le_bytes(b[8..12].try_into().unwrap()),
            manifest_off: u64::from_le_bytes(b[12..20].try_into().unwrap()),
            index_off: u64::from_le_bytes(b[20..28].try_into().unwrap()),
            total_crc: u32::from_le_bytes(b[28..32].try_into().unwrap()),
        })
    }
}

/// 索引中的条目元数据。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EntryMeta {
    /// 包内路径（'/' 分隔）。
    pub path: String,
    pub orig_size: u64,
    pub crc32: u32,
    pub attrs: u32,
    /// 源文件修改时间（unix 秒），skip-if-newer 比较用。
    pub mtime: u64,
    /// 条目 header 在文件中的绝对偏移。
    pub data_off: u64,
    /// 压缩数据长度（header 之后）。
    pub data_len: u64,
}

/// 打开的安装包：持有文件句柄、manifest 与全部条目索引。
pub struct Package {
    file: std::fs::File,
    pub manifest: Manifest,
    /// manifest 的 canonical JSON（zstd 解压后）。
    pub manifest_json: Vec<u8>,
    pub entries: Vec<EntryMeta>,
    pub footer: Footer,
}

impl Package {
    /// 打开 setup.exe 并解析 overlay。
    pub fn open(path: &Path) -> Result<Self> {
        let file = std::fs::File::open(path)?;
        Self::from_file(file)
    }

    pub fn from_file(mut file: std::fs::File) -> Result<Self> {
        let file_len = file.seek(SeekFrom::End(0))?;
        if file_len < FOOTER_SIZE + 4 {
            return Err(Error::Overlay("文件过小，不是 MoInstaller 包".into()));
        }
        file.seek(SeekFrom::Start(file_len - FOOTER_SIZE))?;
        let mut fb = [0u8; 32];
        file.read_exact(&mut fb)?;
        let footer = Footer::from_bytes(&fb)?;

        if !(footer.index_off < footer.manifest_off && footer.manifest_off < file_len - FOOTER_SIZE)
        {
            return Err(Error::Overlay("footer 偏移非法：包损坏".into()));
        }

        let index_z = read_range(
            &mut file,
            footer.index_off,
            footer.manifest_off - footer.index_off,
        )?;
        let manifest_z = read_range(
            &mut file,
            footer.manifest_off,
            file_len - FOOTER_SIZE - footer.manifest_off,
        )?;

        // total CRC（index 在前、manifest 在后）
        let mut crc = crc32fast::Hasher::new();
        crc.update(&index_z);
        crc.update(&manifest_z);
        if crc.finalize() != footer.total_crc {
            return Err(Error::Overlay("total CRC 校验失败：包损坏或被篡改".into()));
        }

        let index = zstd::decode_all(&index_z[..])
            .map_err(|e| Error::Overlay(format!("索引解压失败: {e}")))?;
        let entries = parse_index(&index)?
            .into_iter()
            .map(|(off, len)| read_entry_header(&mut file, off, len))
            .collect::<Result<Vec<_>>>()?;

        let manifest_json = zstd::decode_all(&manifest_z[..])
            .map_err(|e| Error::Overlay(format!("manifest 解压失败: {e}")))?;
        let manifest: Manifest = serde_json::from_slice(&manifest_json)
            .map_err(|e| Error::Overlay(format!("manifest 反序列化失败: {e}")))?;

        Ok(Self {
            file,
            manifest,
            manifest_json,
            entries,
            footer,
        })
    }

    /// 读取并解压一条 entry，校验 orig_size 与 CRC32。
    ///
    /// 元数据已在 [Package::from_file] 阶段从数据区 header 提取并校验过，
    /// 这里直接定位到数据段读取。
    pub fn read_entry(&mut self, entry: &EntryMeta) -> Result<Vec<u8>> {
        let header_len = 2 + entry.path.len() + 24;
        self.file
            .seek(SeekFrom::Start(entry.data_off + header_len as u64))?;
        let mut data = vec![0u8; entry.data_len as usize];
        self.file.read_exact(&mut data)?;

        let raw = if entry.attrs & ATTR_STORED != 0 {
            data
        } else {
            zstd::bulk::decompress(&data, entry.orig_size as usize)
                .map_err(|e| Error::Overlay(format!("entry {:?} 解压失败: {e}", entry.path)))?
        };

        if raw.len() as u64 != entry.orig_size || entry.crc32 != crc32fast::hash(&raw) {
            return Err(Error::CrcMismatch {
                path: entry.path.clone(),
            });
        }
        Ok(raw)
    }

    /// 包内路径前缀查询（如 "f0/"），返回按 path 排序的副本。
    pub fn entries_with_prefix(&self, prefix: &str) -> Vec<EntryMeta> {
        let mut v: Vec<EntryMeta> = self
            .entries
            .iter()
            .filter(|e| e.path.starts_with(prefix))
            .cloned()
            .collect();
        v.sort_by(|a, b| a.path.cmp(&b.path));
        v
    }

    pub fn entry(&self, path: &str) -> Option<&EntryMeta> {
        self.entries.iter().find(|e| e.path == path)
    }
}

fn read_range(file: &mut std::fs::File, off: u64, len: u64) -> Result<Vec<u8>> {
    let mut buf = vec![0u8; len as usize];
    file.seek(SeekFrom::Start(off))?;
    file.read_exact(&mut buf)?;
    Ok(buf)
}

fn parse_index(index: &[u8]) -> Result<Vec<(u64, u64)>> {
    if index.len() < 4 {
        return Err(Error::Overlay("索引过短".into()));
    }
    let count = u32::from_le_bytes(index[0..4].try_into().unwrap()) as usize;
    if index.len() != 4 + count * 16 {
        return Err(Error::Overlay("索引长度与条目数不匹配".into()));
    }
    let mut out = Vec::with_capacity(count);
    let mut pos = 4usize;
    for _ in 0..count {
        let data_off = u64::from_le_bytes(index[pos..pos + 8].try_into().unwrap());
        let data_len = u64::from_le_bytes(index[pos + 8..pos + 16].try_into().unwrap());
        pos += 16;
        out.push((data_off, data_len));
    }
    Ok(out)
}

/// 从数据区读一条 entry 的内联 header，构造 EntryMeta。
fn read_entry_header(file: &mut std::fs::File, off: u64, data_len: u64) -> Result<EntryMeta> {
    file.seek(SeekFrom::Start(off))?;
    let mut rdl = [0u8; 2];
    file.read_exact(&mut rdl)?;
    let path_len = u16::from_le_bytes(rdl) as usize;
    if path_len == 0 || path_len > 4096 {
        return Err(Error::Overlay(format!("entry path 长度非法: {path_len}")));
    }
    let mut path_buf = vec![0u8; path_len];
    file.read_exact(&mut path_buf)?;
    let path = String::from_utf8_lossy(&path_buf).into_owned();
    if path.is_empty() || path.starts_with('/') || path.contains('\\') {
        return Err(Error::Overlay(format!("entry path 非法: {path:?}")));
    }
    let mut n8 = [0u8; 8];
    file.read_exact(&mut n8)?;
    let orig_size = u64::from_le_bytes(n8);
    let mut n4 = [0u8; 4];
    file.read_exact(&mut n4)?;
    let crc32 = u32::from_le_bytes(n4);
    file.read_exact(&mut n4)?;
    let attrs = u32::from_le_bytes(n4);
    file.read_exact(&mut n8)?;
    let mtime = u64::from_le_bytes(n8);
    Ok(EntryMeta {
        path,
        orig_size,
        crc32,
        attrs,
        mtime,
        data_off: off,
        data_len,
    })
}

/// 构建期写入器：收集条目后一次性写出（模板 + overlay）。
pub struct PackageBuilder {
    manifest_json: Vec<u8>,
    compression: crate::manifest::Compression,
    pending: Vec<PendingEntry>,
}

struct PendingEntry {
    path: String,
    orig_size: u64,
    /// header + 压缩数据（一次成型的字节块）。
    blob: Vec<u8>,
}

impl PackageBuilder {
    pub fn new(manifest: &Manifest, compression: crate::manifest::Compression) -> Result<Self> {
        Ok(Self {
            manifest_json: manifest.to_canonical_json()?,
            compression,
            pending: Vec::new(),
        })
    }

    /// 添加一个文件。attrs 可含 ATTR_READONLY；ATTR_STORED 由压缩设置决定；
    /// mtime 为源文件修改时间（unix 秒）。
    pub fn add_file(&mut self, path: &str, content: &[u8], attrs: u32, mtime: u64) -> Result<()> {
        if path.is_empty() || path.starts_with('/') || path.contains('\\') {
            return Err(Error::Overlay(format!(
                "包内路径非法: {path:?}（应为相对路径且用 '/' 分隔）"
            )));
        }
        let (data, attrs) = match self.compression {
            crate::manifest::Compression::Store => (content.to_vec(), attrs | ATTR_STORED),
            crate::manifest::Compression::Zstd(level) => (
                zstd::bulk::compress(content, i32::from(level))?,
                attrs & !ATTR_STORED,
            ),
        };
        let crc = crc32fast::hash(content);
        let mut blob = Vec::with_capacity(2 + path.len() + 24 + data.len());
        blob.extend_from_slice(&(path.len() as u16).to_le_bytes());
        blob.extend_from_slice(path.as_bytes());
        blob.extend_from_slice(&(content.len() as u64).to_le_bytes());
        blob.extend_from_slice(&crc.to_le_bytes());
        blob.extend_from_slice(&attrs.to_le_bytes());
        blob.extend_from_slice(&mtime.to_le_bytes());
        blob.extend_from_slice(&data);
        self.pending.push(PendingEntry {
            path: path.to_string(),
            orig_size: content.len() as u64,
            blob,
        });
        Ok(())
    }

    pub fn entry_count(&self) -> usize {
        self.pending.len()
    }

    /// 输入内容总字节数（未压缩）。
    pub fn total_input_bytes(&self) -> u64 {
        self.pending.iter().map(|p| p.orig_size).sum()
    }

    /// 写出完整 setup.exe：模板 PE + 数据区 + 索引 + manifest + footer。
    pub fn write_to(&self, w: &mut impl Write, template: &[u8]) -> Result<u64> {
        if template.len() < 2 || &template[..2] != b"MZ" {
            return Err(Error::Overlay("模板不是有效的 PE 文件（缺 MZ 头）".into()));
        }
        w.write_all(template)?;
        let mut off = template.len() as u64;

        let mut index_raw = Vec::new();
        index_raw.extend_from_slice(&(self.pending.len() as u32).to_le_bytes());
        for p in &self.pending {
            let header_len = 2 + p.path.len() + 24;
            let data_len = p.blob.len() - header_len;
            index_raw.extend_from_slice(&off.to_le_bytes()); // data_off
            index_raw.extend_from_slice(&(data_len as u64).to_le_bytes()); // data_len
            w.write_all(&p.blob)?;
            off += p.blob.len() as u64;
        }

        let index_z = zstd::bulk::compress(&index_raw, 19)?;
        let index_off = off;
        w.write_all(&index_z)?;
        off += index_z.len() as u64;

        let manifest_z = zstd::bulk::compress(&self.manifest_json, 19)?;
        let manifest_off = off;
        w.write_all(&manifest_z)?;
        off += manifest_z.len() as u64;

        let mut crc = crc32fast::Hasher::new();
        crc.update(&index_z);
        crc.update(&manifest_z);
        let footer = Footer {
            fmt_ver: FMT_VER,
            flags: 0,
            manifest_off,
            index_off,
            total_crc: crc.finalize(),
        };
        w.write_all(&footer.to_bytes())?;
        Ok(off + FOOTER_SIZE)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::Compression;

    fn demo_manifest() -> Manifest {
        Manifest::from_toml_str(
            r#"
[app]
id = "com.example.demo"
name = "Demo"
version = "0.1.0"
publisher = "T"

[[files]]
src = "dist/**/*"
dst = "{app}"
"#,
        )
        .unwrap()
    }

    fn build_pkg(
        compression: Compression,
        files: &[(&str, &[u8])],
    ) -> (Vec<u8>, Vec<(String, Vec<u8>)>) {
        let m = demo_manifest();
        let mut b = PackageBuilder::new(&m, compression).unwrap();
        let mut expect = Vec::new();
        for (p, c) in files {
            b.add_file(p, c, 0, 0).unwrap();
            expect.push((p.to_string(), c.to_vec()));
        }
        let mut out = Vec::new();
        let template = b"MZ fake pe body".to_vec();
        let total = b.write_to(&mut out, &template).unwrap();
        assert_eq!(total as usize, out.len());
        (out, expect)
    }

    fn write_temp(bytes: &[u8]) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("mo-core-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join(format!(
            "t-{}.bin",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::write(&p, bytes).unwrap();
        p
    }

    #[test]
    fn roundtrip_zstd() {
        let big: Vec<u8> = (0..100_000u32).map(|i| (i % 251) as u8).collect();
        let (out, expect) = build_pkg(
            Compression::Zstd(19),
            &[
                ("f0/bin/app.exe", big.as_slice()),
                ("f0/docs/manual-zh.txt", "你好，MoInstaller".as_bytes()),
                ("f0/empty.dat", b""),
            ],
        );
        let path = write_temp(&out);
        let mut pkg = Package::open(&path).unwrap();
        assert_eq!(pkg.manifest.app.id, "com.example.demo");
        assert_eq!(pkg.entries.len(), 3);
        for (p, content) in &expect {
            let e = pkg.entry(p).unwrap().clone();
            let data = pkg.read_entry(&e).unwrap();
            assert_eq!(&data, content, "内容不一致: {p}");
        }
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn roundtrip_store() {
        let (out, expect) = build_pkg(Compression::Store, &[("f0/a.bin", &[1u8, 2, 3, 4, 5])]);
        let path = write_temp(&out);
        let mut pkg = Package::open(&path).unwrap();
        let e = pkg.entry("f0/a.bin").unwrap().clone();
        assert_eq!(pkg.read_entry(&e).unwrap(), expect[0].1);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reject_no_overlay() {
        let path = write_temp(&"MZ just a plain exe".repeat(10).into_bytes());
        assert!(Package::open(&path).is_err());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reject_bad_magic() {
        let (mut out, _) = build_pkg(Compression::Zstd(19), &[("f0/x", b"1")]);
        let n = out.len();
        out[n - 1] ^= 0xFF; // 破坏 footer
        let path = write_temp(&out);
        assert!(Package::open(&path).is_err());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reject_truncated() {
        let (out, _) = build_pkg(Compression::Zstd(19), &[("f0/x", b"12345")]);
        let cut = &out[..out.len() - 10];
        let path = write_temp(cut);
        assert!(Package::open(&path).is_err());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn detect_manifest_tamper() {
        let (mut out, _) = build_pkg(Compression::Zstd(19), &[("f0/x", b"12345")]);
        // 篡改 manifest 段尾部某字节（footer 前 5 字节处属于 manifest_z）
        let n = out.len();
        out[n - 32 - 5] ^= 0xFF;
        let path = write_temp(&out);
        assert!(matches!(
            Package::open(&path),
            Err(Error::Overlay(msg)) if msg.contains("CRC")
        ));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn detect_entry_tamper() {
        let (mut out, _) = build_pkg(Compression::Zstd(19), &[("f0/x", &[7u8; 128])]);
        // 数据区在模板(15B)之后；篡改第一个 entry 的压缩数据
        let idx = 15 + 2 + 4 + 24 + 3;
        out[idx] ^= 0xFF;
        let path = write_temp(&out);
        let mut pkg = Package::open(&path).unwrap(); // 索引与 manifest 完好
        let e = pkg.entry("f0/x").unwrap().clone();
        // zstd 帧自带校验：篡改可能报解压失败或 CRC 不匹配，两者都算检测成功
        assert!(matches!(
            pkg.read_entry(&e),
            Err(Error::CrcMismatch { .. }) | Err(Error::Overlay(_))
        ));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn empty_payload_ok() {
        let (out, _) = build_pkg(Compression::Zstd(19), &[]);
        let path = write_temp(&out);
        let pkg = Package::open(&path).unwrap();
        assert!(pkg.entries.is_empty());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn entries_with_prefix_sorted() {
        let (out, _) = build_pkg(
            Compression::Zstd(1),
            &[("f1/b", b"2"), ("f0/z", b"3"), ("f0/a", b"1")],
        );
        let path = write_temp(&out);
        let pkg = Package::open(&path).unwrap();
        let f0: Vec<String> = pkg
            .entries_with_prefix("f0/")
            .into_iter()
            .map(|e| e.path)
            .collect();
        assert_eq!(f0, vec!["f0/a", "f0/z"]);
        let _ = std::fs::remove_file(&path);
    }
}
