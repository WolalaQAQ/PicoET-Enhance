#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();

    if args.iter().any(|arg| arg == "--version" || arg == "-V") {
        picoet_pc_studio_lib::attach_parent_console_if_needed();
        println!("{}", picoet_pc_studio_lib::updater::APP_VERSION);
        return;
    }

    if args.iter().any(|arg| arg == "--stop") {
        let code = picoet_pc_studio_lib::stop_running_instance();
        std::process::exit(code);
    }

    picoet_pc_studio_lib::run();
}
