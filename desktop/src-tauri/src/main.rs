// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // `--self-test <absolute temporary data dir>`: headless artifact check
    // (start the bundled backend, authenticate, read status, stop, exit).
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 3 && args[1] == "--self-test" {
        std::process::exit(sam_desktop_lib::self_test(&args[2]));
    }
    sam_desktop_lib::run_app();
}
