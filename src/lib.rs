use std::fs;

use zed_extension_api::{
    self as zed,
    http_client::{HttpMethod, HttpRequest, RedirectPolicy},
    settings::LspSettings,
    Architecture, DownloadedFileType, LanguageServerId, LanguageServerInstallationStatus, Os,
    Result,
};

const SERVER_NAME: &str = "shopware-lsp";

/// Open VSX is the only current source for 0.3.x builds. The GitHub releases of
/// shopware/shopware-lsp stopped at 0.1.2, so the marketplace artifact is the
/// binary of record.
const OPEN_VSX_API: &str = "https://open-vsx.org/api/shopware/shopware-lsp";

struct ShopwareLspExtension {
    /// Resolved once per extension process so a settings reload does not send
    /// another request to Open VSX.
    cached_binary_path: Option<String>,
}

/// Where the binary lives inside the downloaded `.vsix`, which is a plain zip.
fn binary_path_in(dir: &str) -> String {
    match zed::current_platform().0 {
        Os::Windows => format!("{dir}/extension/{SERVER_NAME}.exe"),
        _ => format!("{dir}/extension/{SERVER_NAME}"),
    }
}

/// Map Zed's platform to an Open VSX target triple.
///
/// musl is deliberately absent: the extension API cannot tell glibc from musl,
/// so Alpine users have to point `binary.path` at an `alpine-*` build themselves.
fn open_vsx_target() -> Result<&'static str> {
    let (os, arch) = zed::current_platform();
    match (os, arch) {
        (Os::Mac, Architecture::Aarch64) => Ok("darwin-arm64"),
        (Os::Mac, Architecture::X8664) => Ok("darwin-x64"),
        (Os::Linux, Architecture::Aarch64) => Ok("linux-arm64"),
        (Os::Linux, Architecture::X8664) => Ok("linux-x64"),
        (Os::Windows, Architecture::X8664) => Ok("win32-x64"),
        _ => Err(format!(
            "no published {SERVER_NAME} build for this platform. Set \
             lsp.{SERVER_NAME}.binary.path to a server you built yourself."
        )),
    }
}

impl ShopwareLspExtension {
    /// Ask Open VSX for the newest build for this platform.
    fn latest_release(target: &str) -> Result<(String, String)> {
        let response = HttpRequest::builder()
            .method(HttpMethod::Get)
            .url(format!("{OPEN_VSX_API}/{target}/latest"))
            .header("Accept", "application/json")
            .redirect_policy(RedirectPolicy::FollowAll)
            .build()?
            .fetch()?;

        let payload: zed::serde_json::Value = zed::serde_json::from_slice(&response.body)
            .map_err(|err| format!("Open VSX returned malformed JSON: {err}"))?;

        let version = payload["version"]
            .as_str()
            .ok_or_else(|| "Open VSX response has no version".to_string())?;
        let url = payload["files"]["download"]
            .as_str()
            .ok_or_else(|| format!("Open VSX has no download for {target}"))?;

        Ok((version.to_string(), url.to_string()))
    }

    /// Download the server on demand, reusing an existing copy when possible.
    fn download_server(&mut self, language_server_id: &LanguageServerId) -> Result<String> {
        if let Some(path) = &self.cached_binary_path {
            if fs::metadata(path)
                .map(|stat| stat.is_file())
                .unwrap_or(false)
            {
                return Ok(path.clone());
            }
        }

        zed::set_language_server_installation_status(
            language_server_id,
            &LanguageServerInstallationStatus::CheckingForUpdate,
        );

        let target = open_vsx_target()?;
        let (version, url) = Self::latest_release(target)?;

        let version_dir = format!("{SERVER_NAME}-{version}-{target}");
        let binary = binary_path_in(&version_dir);

        if !fs::metadata(&binary)
            .map(|stat| stat.is_file())
            .unwrap_or(false)
        {
            zed::set_language_server_installation_status(
                language_server_id,
                &LanguageServerInstallationStatus::Downloading,
            );

            zed::download_file(&url, &version_dir, DownloadedFileType::Zip).map_err(|err| {
                format!("failed to download {SERVER_NAME} {version} for {target}: {err}")
            })?;
            zed::make_file_executable(&binary)?;

            Self::remove_other_versions(&version_dir);
        }

        zed::set_language_server_installation_status(
            language_server_id,
            &LanguageServerInstallationStatus::None,
        );

        self.cached_binary_path = Some(binary.clone());
        Ok(binary)
    }

    /// Each server is roughly 31 MB, so drop superseded downloads.
    fn remove_other_versions(keep: &str) {
        let entries = match fs::read_dir(".") {
            Ok(entries) => entries,
            Err(_) => return,
        };

        for entry in entries.flatten() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with(SERVER_NAME) && name != keep {
                fs::remove_dir_all(entry.path()).ok();
            }
        }
    }

    /// Resolve the server, preferring anything the user controls.
    ///
    /// An explicit path wins, then a `shopware-lsp` already on PATH, and only
    /// then a managed download. That order matters: a locally built server is
    /// usually newer, or carries fixes the published build does not have yet.
    fn server_binary(
        &mut self,
        language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<String> {
        if let Some(path) = LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.binary)
            .and_then(|binary| binary.path)
        {
            return Ok(path);
        }

        if let Some(path) = worktree.which(SERVER_NAME) {
            return Ok(path);
        }

        self.download_server(language_server_id)
    }
}

impl zed::Extension for ShopwareLspExtension {
    fn new() -> Self {
        Self {
            cached_binary_path: None,
        }
    }

    fn language_server_command(
        &mut self,
        language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        let arguments = LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.binary)
            .and_then(|binary| binary.arguments)
            .unwrap_or_default();

        Ok(zed::Command {
            command: self.server_binary(language_server_id, worktree)?,
            // No subcommand: the binary starts a stdio language server by default.
            args: arguments,
            env: worktree.shell_env(),
        })
    }

    /// Forward the `shopwareLSP.*` block from Zed settings, so the server sees
    /// the same configuration the VS Code extension would push.
    fn language_server_workspace_configuration(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<Option<zed::serde_json::Value>> {
        Ok(LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.settings))
    }

    fn language_server_initialization_options(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<Option<zed::serde_json::Value>> {
        Ok(LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.initialization_options))
    }
}

zed::register_extension!(ShopwareLspExtension);
