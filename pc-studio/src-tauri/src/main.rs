#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use picoet_pc_studio_lib::CliAction;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();

    match picoet_pc_studio_lib::parse_cli_args(&args) {
        Ok(CliAction::Version) => {
            picoet_pc_studio_lib::attach_parent_console_if_needed();
            println!("{}", picoet_pc_studio_lib::updater::APP_VERSION);
        }
        Ok(CliAction::Stop) => {
            let code = picoet_pc_studio_lib::stop_running_instance();
            std::process::exit(code);
        }
        Ok(CliAction::Run(opts)) => {
            picoet_pc_studio_lib::run_with_options(opts);
        }
        Err(err) => {
            picoet_pc_studio_lib::attach_parent_console_if_needed();
            eprintln!("Error: {err}");
            std::process::exit(2);
        }
    }
}
