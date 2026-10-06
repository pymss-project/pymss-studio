// Pymss Studio setup script — port of the legacy Inno Setup [Code] section.
// Semantics preserved:
//  * fresh-install marker: pymss-studio.inno-install (name kept for upgrade continuity)
//  * bundled runtime-envs are installed on fresh installs, or when the packaged
//    backend env is missing on the target (user-managed envs are preserved)
//  * active-runtime.json is never installed (the app owns it)
//  * pyvenv.cfg entries are rewritten to the local install directory
//  * VC++ x64 runtime is installed quietly when missing, then the cached
//    installer is removed
//  * development leftovers inside the staged tree are cleaned up

fn mark_file() {
	"pymss-studio.inno-install"
}

fn envs_prefix() {
	"python-runtime/runtime-envs/"
}

fn is_fresh_install(ctx) {
	!ctx.file_exists(ctx.app_dir + "\\" + mark_file())
}

fn backend_installed(ctx, backend) {
	ctx.file_exists(
		ctx.app_dir + "\\python-runtime\\runtime-envs\\" + backend + "\\Scripts\\python.exe"
	)
}

// Per-file decision replacing the Inno rule-level Check (ShouldInstallBundledRuntimeEnvs)
// plus the Excludes entries. Paths use package-internal forward slashes.
fn before_file(ctx, path) {
	if path == envs_prefix() + "active-runtime.json" {
		return false;
	}
	if !path.starts_with(envs_prefix()) {
		return true;
	}
	if is_fresh_install(ctx) {
		return true;
	}
	let rest = path.sub_string(envs_prefix().len(), path.len());
	let parts = rest.split("/");
	let backend = parts[0];
	if backend == "cpu" || backend == "cuda" || backend == "rocm" {
		return !backend_installed(ctx, backend);
	}
	true
}

fn repair_venv(ctx, backend) {
	let app = ctx.app_dir;
	let env_dir = app + "\\python-runtime\\runtime-envs\\" + backend;
	let cfg = env_dir + "\\pyvenv.cfg";
	let runtime_dir = app + "\\python-runtime";
	let python = runtime_dir + "\\python.exe";
	if !ctx.file_exists(cfg) || !ctx.file_exists(python) {
		return;
	}
	let content = "home = " + runtime_dir + "\r\n" +
		"include-system-site-packages = false\r\n" +
		"executable = " + python + "\r\n" +
		"command = " + python + " -m venv " + env_dir + "\r\n";
	if !ctx.write_text(cfg, content) {
		ctx.abort("Failed to write Python venv config: " + cfg);
	}
	// The ROCm pip console launcher embeds the CI python path and is not relocatable.
	if backend == "rocm" {
		ctx.delete_file(env_dir + "\\Scripts\\offload-arch.exe");
	}
}

fn remove_dev_leftovers(ctx) {
	let app = ctx.app_dir;
	ctx.delete_dir(app + "\\.git");
	ctx.delete_dir(app + "\\.github");
	ctx.delete_dir(app + "\\.claude");
	ctx.delete_dir(app + "\\.omc");
	ctx.delete_dir(app + "\\.spec-workflow");
	ctx.delete_dir(app + "\\__pycache__");
	ctx.delete_dir(app + "\\python-runtime\\Doc");
	ctx.delete_dir(app + "\\python-runtime\\include");
	ctx.delete_dir(app + "\\python-runtime\\libs");
}

fn after_install(ctx) {
	let app = ctx.app_dir;

	// VC++ 2015-2022 x64 runtime: install quietly only when missing.
	let vcredist = app + "\\bin\\VC_redist.x64.exe";
	let installed = ctx.reg_read(
		"hklm",
		"SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64",
		"Installed"
	) == "1";
	if !installed && ctx.file_exists(vcredist) {
		ctx.log("Installing Microsoft Visual C++ Runtime...");
		let code = ctx.run(vcredist, ["/install", "/quiet", "/norestart"]);
		if code != 0 {
			ctx.log("VC redist installer exited with " + code + " (continuing)");
		}
		ctx.delete_file(vcredist);
	}

	repair_venv(ctx, "cpu");
	repair_venv(ctx, "cuda");
	repair_venv(ctx, "rocm");

	remove_dev_leftovers(ctx);

	// Remove the legacy Inno uninstaller when upgrading in place.
	ctx.delete_file(app + "\\unins000.exe");
	ctx.delete_file(app + "\\unins000.dat");

	ctx.write_text(app + "\\" + mark_file(), "managed\r\n");
	ctx.log("Pymss Studio post-install tasks finished.");
}
