use std::{
    env,
    ffi::{OsStr, OsString},
    fs, io,
    path::{Path, PathBuf},
    process::{self, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const HELP: &str = "MSSHI Doctor • Pemeriksaan awal\n\
Pemakaian: msshi-preflight [--doctor-dir FOLDER] [--hermes-home FOLDER]\n\
Jalankan dari paket portabel atau folder sumber Doctor, atau tentukan --doctor-dir.\n\
Opsi --hermes-home memeriksa keberadaan folder Hermes dan penanda pembaruan.\n\
Pemeriksaan ini berjalan offline tanpa membuka aplikasi, profil, atau konfigurasi.\n\
Kode keluar: 0 = siap, 2 = perlu perhatian, 1 = argumen tidak valid.";

// Isolated Python: bypass project modules, site packages and user startup hooks.
// Tcl() verifies the runtime and libraries without creating a GUI or opening Hermes.
const PYTHON_CHECK: &str = r#"import sys
if sys.version_info < (3, 10):
    sys.exit(10)
try:
    import tkinter
    tkinter.Tcl().call("info", "patchlevel")
except Exception:
    sys.exit(11)
"#;

struct Options {
    doctor_dir: PathBuf,
    hermes_home: Option<PathBuf>,
}

fn parse(args: impl Iterator<Item = OsString>) -> Result<Option<Options>, String> {
    let mut args = args;
    let mut options = Options { doctor_dir: default_doctor_dir(), hermes_home: None };
    while let Some(flag) = args.next() {
        if flag == "--help" || flag == "-h" {
            return Ok(None);
        }
        if flag != "--doctor-dir" && flag != "--hermes-home" {
            return Err(format!("Opsi tidak dikenal: {}", flag.to_string_lossy()));
        }
        let path = args.next().filter(|arg| !arg.is_empty() && !arg.to_string_lossy().starts_with("--"))
            .ok_or_else(|| format!("Isi folder untuk {}.", flag.to_string_lossy()))?;
        if flag == "--doctor-dir" {
            options.doctor_dir = path.into();
        } else {
            options.hermes_home = Some(path.into());
        }
    }
    Ok(Some(options))
}

#[derive(Debug, PartialEq, Eq)]
enum Runtime { Ready, CliOnly, Old, Missing, Unavailable }

// Only search absolute PATH directories. Do not launch a file found via the current
// project directory or an uninstalled Windows Store execution alias.
fn executable(name: &str, paths: &OsStr) -> Option<PathBuf> {
    env::split_paths(paths)
        .filter(|p| p.is_absolute())
        .map(|p| p.join(name))
        .find(|p| {
            let is_store_alias = cfg!(windows) && p.parent().and_then(Path::file_name)
                .is_some_and(|dir| dir.eq_ignore_ascii_case("WindowsApps"));
            !is_store_alias && p.is_file()
        })
}

fn inspect_python(program: &Path, is_launcher: bool) -> io::Result<Runtime> {
    let mut command = Command::new(program);
    if is_launcher { command.arg("-3"); }
    let mut child = command.args(["-I", "-B", "-S", "-c", PYTHON_CHECK])
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()?;
    let start = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(match status.code() {
                Some(0) => Runtime::Ready,
                Some(10) => Runtime::Old,
                Some(11) => Runtime::CliOnly,
                _ => Runtime::Unavailable,
            });
        }
        if start.elapsed() >= Duration::from_secs(5) {
            let _ = child.kill();
            let _ = child.wait();
            return Ok(Runtime::Unavailable);
        }
        thread::sleep(Duration::from_millis(40));
    }
}

fn find_runtime() -> Runtime {
    let paths = env::var_os("PATH").unwrap_or_default();
    let names = if cfg!(windows) { ["python.exe", "py.exe"] } else { ["python3", "python"] };
    for name in names {
        if let Some(program) = executable(name, &paths) {
            return inspect_python(&program, name == "py.exe").unwrap_or(Runtime::Unavailable);
        }
    }
    Runtime::Missing
}

const SOURCE_FILES: &[&str] = &[
    "MSSHI_Doctor.bat", "msshi-doctor.cmd", "src/msshi_doctor_app/cli.py",
    "src/msshi_doctor_app/gui.py", "src/msshi_doctor_app/__main__.py",
];
const PORTABLE_FILES: &[&str] = &[
    "MSSHI-Doctor.exe", "msshi-doctor-cli.exe", "msshi-doctor.cmd",
    "_internal/_tk_data/tk.tcl", "_internal/_tcl_data/init.tcl",
];

fn is_portable(root: &Path) -> bool {
    ["MSSHI-Doctor.exe", "msshi-doctor-cli.exe", "_internal"].iter()
        .any(|name| root.join(name).exists())
}

fn default_doctor_dir() -> PathBuf {
    env::current_exe().ok().and_then(|exe| {
        exe.parent().filter(|parent| is_portable(parent)).map(Path::to_owned)
    }).unwrap_or_else(|| PathBuf::from("."))
}

fn missing_files(root: &Path, expected: &'static [&'static str]) -> Vec<&'static str> {
    expected.iter().copied()
        .filter(|name| !fs::metadata(root.join(name)).is_ok_and(|f| f.is_file() && f.len() > 0))
        .collect()
}

fn check_home(home: &Path) -> Result<(), &'static str> {
    if !home.is_dir() { return Err("Folder Hermes belum ditemukan. Periksa pilihan --hermes-home."); }
    match fs::symlink_metadata(home.join(".hermes-update-in-progress")) {
        Ok(_) => Err("Ada penanda pembaruan. Periksa progres pembaruan Hermes sebelum mencoba perbaikan."),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err("Status penanda pembaruan tidak dapat dibaca. Periksa akses folder Hermes."),
    }
}

fn run(options: Options) -> i32 {
    println!("MSSHI Doctor • Pemeriksaan awal");
    let portable = is_portable(&options.doctor_dir);
    let expected = if portable { PORTABLE_FILES } else { SOURCE_FILES };
    let missing = missing_files(&options.doctor_dir, expected);
    let mut ready = missing.is_empty();
    if missing.is_empty() {
        println!("[OK] Berkas peluncur Doctor tersedia.");
    } else {
        println!("[CEK] Ekstrak kembali paket Doctor atau pilih --doctor-dir yang benar.");
        println!("      Berkas hilang, kosong, atau tidak dapat dibaca: {}", missing.join(", "));
    }
    if portable {
        println!("Paket portabel menggunakan Python dan Tcl/Tk bawaan.");
    } else {
        let runtime = find_runtime();
        ready &= runtime == Runtime::Ready;
        println!("{}", match runtime {
        Runtime::Ready => "[OK] Python 3.10+ dan Tcl/Tk terdeteksi.",
        Runtime::CliOnly => "[CEK] Python tersedia. Gunakan CLI atau aktifkan Tcl/Tk pada instalasi Python untuk GUI.",
        Runtime::Old => "[CEK] Python yang ditemukan terlalu lama. Pilih Python 3.10 atau lebih baru.",
        Runtime::Missing => "[CEK] Peluncur Python belum ditemukan di PATH. Pasang Python 3.10+ beserta Tcl/Tk.",
        Runtime::Unavailable => "[CEK] Python gagal dimulai dalam batas pemeriksaan. Coba periksa python --version.",
        });
    }
    if let Some(home) = options.hermes_home {
        match check_home(&home) {
            Ok(()) => println!("[OK] Folder Hermes tersedia dan penanda pembaruan tidak ditemukan."),
            Err(reason) => { println!("[CEK] {reason}"); ready = false; }
        }
    }
    if ready {
        let next = if portable {
            "Buka MSSHI-Doctor.exe untuk memeriksa instalasi."
        } else if cfg!(windows) {
            "Buka MSSHI_Doctor.bat untuk memeriksa kondisi instalasi."
        } else {
            "Ikuti panduan Python di README untuk membuka Doctor."
        };
        println!("{next}");
        0
    } else { 2 }
}

fn main() {
    let code = match parse(env::args_os().skip(1)) {
        Ok(Some(options)) => run(options),
        Ok(None) => { println!("{HELP}"); 0 }
        Err(reason) => { eprintln!("{reason}\n{HELP}"); 1 }
    };
    process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;

    #[test]
    fn options_accept_spaces_and_reject_missing_values() {
        let parsed = parse(["--doctor-dir", "repo with spaces", "--hermes-home", "H"].map(OsString::from).into_iter()).unwrap().unwrap();
        assert_eq!(parsed.doctor_dir, Path::new("repo with spaces"));
        assert_eq!(parsed.hermes_home.as_deref(), Some(Path::new("H")));
        assert!(parse(["--doctor-dir", "--hermes-home"].map(OsString::from).into_iter()).is_err());
        assert!(parse(["--fix"].map(OsString::from).into_iter()).is_err());
        assert!(parse(["--help"].map(OsString::from).into_iter()).unwrap().is_none());
    }

    #[test]
    fn source_and_update_marker_are_checked_without_modifying_them() {
        let nonce = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_nanos();
        let root = env::temp_dir().join(format!("msshi-preflight-{}-{nonce}", process::id()));
        assert!(check_home(&root).is_err());
        fs::create_dir(&root).unwrap();
        let missing = missing_files(&root, SOURCE_FILES);
        assert_eq!(missing.len(), 5);
        for name in missing {
            let file = root.join(name);
            fs::create_dir_all(file.parent().unwrap()).unwrap();
            fs::write(file, "test").unwrap();
        }
        assert!(missing_files(&root, SOURCE_FILES).is_empty());
        fs::write(root.join("MSSHI_Doctor.bat"), "").unwrap();
        assert_eq!(missing_files(&root, SOURCE_FILES), vec!["MSSHI_Doctor.bat"]);
        assert!(check_home(&root).is_ok());
        let marker = root.join(".hermes-update-in-progress");
        fs::write(&marker, "synthetic marker").unwrap();
        assert!(check_home(&root).is_err());
        assert_eq!(fs::read(&marker).unwrap(), b"synthetic marker");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn portable_layout_does_not_need_a_host_python() {
        let nonce = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_nanos();
        let root = env::temp_dir().join(format!("msshi-portable-{}-{nonce}", process::id()));
        for file in PORTABLE_FILES {
            let path = root.join(file);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "test").unwrap();
        }
        assert!(is_portable(&root));
        assert_eq!(run(Options { doctor_dir: root.clone(), hermes_home: None }), 0);
        fs::remove_file(root.join("_internal/_tk_data/tk.tcl")).unwrap();
        assert_eq!(run(Options { doctor_dir: root.clone(), hermes_home: None }), 2);
        fs::remove_dir_all(root).unwrap();
    }
}
