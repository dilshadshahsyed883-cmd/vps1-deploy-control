//! Persistent Z Studio-compatible scene Media Library.
//! Keeps originals untouched. Counts only completed, SHA-verified native MP4
//! renders, with an independent read-only import path for legacy Z Builder usage.
use rusqlite::{params, Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, fs, io::Read, path::{Path, PathBuf}};
use walkdir::WalkDir;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

#[derive(Clone, Debug, PartialEq)]
pub struct Asset {
    pub id: String,
    pub path: PathBuf,
    pub filename: String,
    pub kind: String,
    pub tags: String,
    pub approved: bool,
    pub exists: bool,
    pub used: u32,
    pub legacy_used: u32,
}

pub fn kind_of(path: &Path) -> Option<&'static str> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    match ext.as_str() {
        "jpg" | "jpeg" | "png" | "webp" | "bmp" => Some("image"),
        "mp4" | "m4v" | "mov" | "mkv" | "webm" | "avi" => Some("video"),
        _ => None,
    }
}

pub fn stable_id(path: &Path) -> Result<String> {
    let canonical = fs::canonicalize(path)?;
    // Equivalent to Z Builder stable_id(path): casefold canonical path then SHA256[:24].
    let hash = Sha256::digest(canonical.to_string_lossy().to_lowercase().as_bytes());
    Ok(hex::encode(hash)[..24].to_owned())
}

pub fn verify_mp4(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let mut head = [0u8; 12];
    file.read_exact(&mut head)?;
    if &head[4..8] != b"ftyp" {
        return Err("Rendered output is not a valid MP4 container header".into());
    }
    if fs::metadata(path)?.len() < 32 {
        return Err("Rendered MP4 is too small".into());
    }
    let mut hasher = Sha256::new();
    hasher.update(head);
    let mut buffer = [0u8; 1 << 20];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 { break; }
        hasher.update(&buffer[..count]);
    }
    Ok(hex::encode(hasher.finalize()))
}

pub struct Catalog {
    conn: Connection,
}

impl Catalog {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() { fs::create_dir_all(parent)?; }
        let conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA foreign_keys=ON;
             PRAGMA journal_mode=WAL;
             CREATE TABLE IF NOT EXISTS assets (
                 id TEXT PRIMARY KEY,
                 path TEXT UNIQUE NOT NULL,
                 filename TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 tags TEXT NOT NULL DEFAULT '',
                 approved INTEGER NOT NULL DEFAULT 1,
                 size_bytes INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE IF NOT EXISTS media_exports (
                 asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
                 times_exported INTEGER NOT NULL DEFAULT 0,
                 exported_at TEXT
             );
             CREATE TABLE IF NOT EXISTS legacy_z_usage (
                 asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
                 times_exported INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE IF NOT EXISTS render_runs (
                 run_id TEXT PRIMARY KEY,
                 project_path TEXT NOT NULL,
                 output_path TEXT NOT NULL,
                 output_sha256 TEXT NOT NULL,
                 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
             );
             CREATE TABLE IF NOT EXISTS render_run_assets (
                 run_id TEXT NOT NULL REFERENCES render_runs(run_id) ON DELETE CASCADE,
                 asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                 PRIMARY KEY (run_id, asset_id)
             );")?;
        Ok(Self { conn })
    }

    pub fn add(&mut self, path: &Path) -> Result<String> {
        if fs::symlink_metadata(path)?.file_type().is_symlink() {
            return Err("Media symlinks are not eligible".into());
        }
        let source = fs::canonicalize(path)?;
        if !source.is_file() { return Err("Select a regular media file".into()); }
        let kind = kind_of(&source).ok_or("Unsupported image/video format")?;
        let id = stable_id(&source)?;
        let name = source.file_name().ok_or("Unnamed media")?.to_string_lossy().into_owned();
        let location = source.to_string_lossy().into_owned();
        self.conn.execute(
            "INSERT INTO assets(id,path,filename,kind,size_bytes) VALUES(?1,?2,?3,?4,?5)
             ON CONFLICT(id) DO UPDATE SET
                filename=excluded.filename,kind=excluded.kind,size_bytes=excluded.size_bytes",
            params![id, location, name, kind, fs::metadata(&source)?.len() as i64])?;
        Ok(id)
    }

    pub fn add_folder(&mut self, root: &Path) -> Result<(usize, Vec<String>)> {
        if !root.is_dir() { return Err("Select an existing media directory".into()); }
        let mut count = 0;
        let mut warnings = Vec::new();
        for entry in WalkDir::new(root).follow_links(false).into_iter() {
            match entry {
                Ok(item) if item.file_type().is_file() && kind_of(item.path()).is_some() => {
                    match self.add(item.path()) {
                        Ok(_) => count += 1,
                        Err(e) => warnings.push(format!("{}: {}", item.path().display(), e)),
                    }
                }
                Err(e) => warnings.push(e.to_string()),
                _ => {}
            }
        }
        Ok((count, warnings))
    }

    pub fn assets(&self) -> Result<Vec<Asset>> {
        let mut stmt = self.conn.prepare(
            "SELECT a.id,a.path,a.filename,a.kind,a.tags,a.approved,
                    COALESCE(e.times_exported,0), COALESCE(z.times_exported,0)
             FROM assets a
             LEFT JOIN media_exports e ON e.asset_id=a.id
             LEFT JOIN legacy_z_usage z ON z.asset_id=a.id
             ORDER BY a.kind,a.filename COLLATE NOCASE,a.id")?;
        let rows = stmt.query_map([], |r| {
            let path: String = r.get(1)?;
            Ok(Asset {
                id:r.get(0)?, path:PathBuf::from(path), filename:r.get(2)?,
                kind:r.get(3)?, tags:r.get(4)?, approved:r.get::<_,i64>(5)? != 0,
                exists:false, used:r.get::<_,i64>(6)?.max(0) as u32,
                legacy_used:r.get::<_,i64>(7)?.max(0) as u32
            })
        })?;
        let mut assets = rows.collect::<std::result::Result<Vec<_>,_>>()?;
        for asset in &mut assets {
            asset.exists = asset.path.is_file() && !asset.path.is_symlink();
        }
        Ok(assets)
    }

    pub fn update_tags(&self, id: &str, tags: &str) -> Result<()> {
        if self.conn.execute("UPDATE assets SET tags=?1 WHERE id=?2", params![tags.trim(),id])? == 0 {
            return Err("No matching catalog item".into());
        }
        Ok(())
    }

    /// Forget catalog metadata only; original PC media is never deleted.
    pub fn forget(&self, id: &str) -> Result<()> {
        self.conn.execute("DELETE FROM assets WHERE id=?1", [id])?;
        Ok(())
    }

    /// Clear all library data; local files and completed MP4 outputs survive.
    pub fn clear_records(&self) -> Result<usize> {
        let count = self.assets()?.len();
        self.conn.execute_batch(
            "DELETE FROM render_run_assets;
             DELETE FROM render_runs;
             DELETE FROM media_exports;
             DELETE FROM legacy_z_usage;
             DELETE FROM assets;")?;
        Ok(count)
    }

    /// One legacy import, read-only source DB. This is *CapCut draft history*
    /// and MUST NOT be presented as verified final MP4 render history.
    pub fn import_z_builder(&mut self, legacy_db: &Path) -> Result<usize> {
        if !legacy_db.is_file() { return Err("Choose the existing Z Builder catalog.sqlite".into()); }
        let source = Connection::open_with_flags(
            legacy_db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let mut stmt = source.prepare(
            "SELECT a.path,COALESCE(a.tags,''),COALESCE(a.approved,1),
                    COALESCE(e.times_exported,0)
             FROM assets a LEFT JOIN media_exports e ON e.asset_id=a.id")?;
        let mut rows = stmt.query([])?;
        let mut imported = 0;
        while let Some(row) = rows.next()? {
            let raw: String = row.get(0)?;
            let path = PathBuf::from(raw);
            if !path.is_file() || path.is_symlink() || kind_of(&path).is_none() { continue; }
            let id = self.add(&path)?;
            let tags: String = row.get(1)?;
            let approved: i64 = row.get(2)?;
            let used: i64 = row.get(3)?;
            self.conn.execute("UPDATE assets SET tags=?1, approved=?2 WHERE id=?3",
                              params![tags,approved,id])?;
            self.conn.execute(
                "INSERT INTO legacy_z_usage(asset_id,times_exported) VALUES(?1,?2)
                 ON CONFLICT(asset_id) DO UPDATE SET
                     times_exported=MAX(times_exported,excluded.times_exported)",
                params![id,used.max(0)])?;
            imported += 1;
        }
        Ok(imported)
    }

    /// Register only a final MP4 which was produced and verified by Concat.
    /// Duplicate paths/appearances in one export count ONCE.
    /// Re-importing an identical render is idempotent across restarts.
    pub fn record_verified_render(&mut self, project: &Path, output: &Path) -> Result<usize> {
        let checksum = verify_mp4(output)?;
        let project_root = fs::canonicalize(project)?;
        let doc: serde_json::Value = serde_json::from_slice(
            &fs::read(project_root.join("concat.json"))?)?;
        let media = doc.get("media").and_then(|v|v.as_array())
            .ok_or("Native Concat document has no media array")?;
        let unique_paths: BTreeSet<String> = media.iter()
            .filter_map(|v| v.get("path").and_then(|v|v.as_str()))
            .filter(|p| !p.is_empty())
            .filter_map(|p| fs::canonicalize(p).ok())
            .map(|p|p.to_string_lossy().into_owned())
            .collect();
        let absolute_output = fs::canonicalize(output)?;
        let fingerprint = format!("{}:{}:{}",project_root.display(),absolute_output.display(),checksum);
        let run_id = hex::encode(Sha256::digest(fingerprint.as_bytes()));
        let tx = self.conn.transaction()?;
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO render_runs(run_id,project_path,output_path,output_sha256)
             VALUES(?1,?2,?3,?4)",
            params![run_id,project_root.to_string_lossy(),absolute_output.to_string_lossy(),checksum])?;
        if inserted == 0 { return Ok(0); }
        let mut counted = 0;
        for path in unique_paths {
            let id = match stable_id(Path::new(&path)) { Ok(id)=>id,Err(_)=>continue };
            let found: Option<String> = tx.query_row(
                "SELECT id FROM assets WHERE id=?1 AND path=?2",
                params![id,path],|r|r.get(0)).optional()?;
            if found.is_none() { continue; }
            tx.execute("INSERT INTO render_run_assets(run_id,asset_id) VALUES(?1,?2)",
                       params![run_id,id])?;
            tx.execute(
                "INSERT INTO media_exports(asset_id,times_exported,exported_at)
                 VALUES(?1,1,CURRENT_TIMESTAMP)
                 ON CONFLICT(asset_id) DO UPDATE SET
                     times_exported=media_exports.times_exported+1,
                     exported_at=CURRENT_TIMESTAMP",
                [id.as_str()])?;
            counted += 1;
        }
        tx.commit()?;
        Ok(counted)
    }

    pub fn summary(&self) -> Result<String> {
        let items = self.assets()?;
        let image_count = items.iter().filter(|a| a.kind=="image").count();
        let video_count = items.iter().filter(|a| a.kind=="video").count();
        let native_used = items.iter().filter(|a|a.used>0).count();
        let legacy_used = items.iter().filter(|a|a.legacy_used>0).count();
        let mut text = format!(
            "Concat Studio - Z Builder Scene Media Library Summary\n\
             Images: {image_count}\nVideos: {video_count}\n\
             Used in verified Concat MP4 renders: {native_used}\n\
             Used in imported Z Builder CapCut draft exports: {legacy_used}\n\
             Never used: {}\n\
             Usage: used=N means N verified Concat MP4 renders; legacy_used=N means imported Z Builder draft usage.\n\
             Repeated appearances in the same final output count once.\n\
             No original PC media is deleted by this library.\n\n",
            items.iter().filter(|a| a.used==0 && a.legacy_used==0).count());
        for kind in ["image","video"] {
            text.push_str(&format!("{}S\n",kind.to_uppercase()));
            for a in items.iter().filter(|a|a.kind==kind) {
                text.push_str(&format!(
                    "- {} | id={} | used={} | legacy_used={} | approved={} | exists={} | tags={} | path={}\n",
                    a.filename,a.id,a.used,a.legacy_used,a.approved,a.exists,a.tags,a.path.display()));
            }
            text.push('\n');
        }
        Ok(text)
    }

    pub fn save_summary(&self, path: &Path) -> Result<()> {
        if path.exists() { return Err("Summary destination already exists".into()); }
        fs::write(path, self.summary()?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime,UNIX_EPOCH};
    fn temp() -> PathBuf {
        let p=std::env::temp_dir().join(format!(
            "concat-native-lib-{}-{}",std::process::id(),
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(&p).unwrap(); p
    }
    #[test]
    fn import_summary_and_forget_do_not_modify_originals() {
        let root=temp();
        let photo=root.join("Never Used.jpg");
        let clip=root.join("Used Clip.mp4");
        fs::write(&photo,b"photo").unwrap();
        fs::write(&clip,b"video").unwrap();
        let mut lib=Catalog::open(&root.join("studio.sqlite")).unwrap();
        let photo_id=lib.add(&photo).unwrap();
        lib.add(&clip).unwrap();
        assert!(lib.summary().unwrap().contains("used=0"));
        lib.update_tags(&photo_id,"wood, countryside").unwrap();
        assert!(lib.summary().unwrap().contains("tags=wood, countryside"));
        lib.forget(&photo_id).unwrap();
        assert!(photo.is_file());
        assert_eq!(lib.assets().unwrap().len(),1);
        lib.clear_records().unwrap();
        assert!(clip.is_file());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn verified_mp4_usage_is_idempotent_and_deduped() {
        let root=temp();
        let pic=root.join("photo.jpg");
        fs::write(&pic,b"image").unwrap();
        let project=root.join("project");
        fs::create_dir(&project).unwrap();
        let mut lib=Catalog::open(&root.join("db.sqlite")).unwrap();
        lib.add(&pic).unwrap();
        let doc=serde_json::json!({"media":[{"path":pic},{"path":pic}]});
        fs::write(project.join("concat.json"),serde_json::to_vec(&doc).unwrap()).unwrap();
        let output=root.join("movie.mp4");
        let mut fake=vec![0u8;128];fake[4..8].copy_from_slice(b"ftyp");
        fs::write(&output,&fake).unwrap();
        assert_eq!(lib.record_verified_render(&project,&output).unwrap(),1);
        assert_eq!(lib.record_verified_render(&project,&output).unwrap(),0);
        assert_eq!(lib.assets().unwrap()[0].used,1);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unverified_mp4_never_counts() {
        let root=temp();let p=root.join("bad.mp4");fs::write(&p,b"wrong").unwrap();
        assert!(verify_mp4(&p).is_err());fs::remove_dir_all(root).unwrap();
    }
}
