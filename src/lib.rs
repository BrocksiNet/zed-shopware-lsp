use std::fs;

use zed_extension_api::{
    self as zed,
    http_client::{HttpMethod, HttpRequest, RedirectPolicy},
    settings::{ContextServerSettings, LspSettings},
    Architecture, ContextServerConfiguration, ContextServerId, DownloadedFileType,
    LanguageServerId, LanguageServerInstallationStatus, Os, Project, Result,
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
    /// Remembered from the language server, which is the only hook that gets a
    /// `Worktree`. `Project` exposes worktree IDs but no paths, so this is the
    /// only way the MCP server can learn the project root.
    cached_worktree_root: Option<String>,
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
    ///
    /// `status_id` is absent when the MCP server triggers the download, because
    /// installation status is a language-server-only concept in Zed.
    fn download_server(&mut self, status_id: Option<&LanguageServerId>) -> Result<String> {
        if let Some(path) = &self.cached_binary_path {
            if fs::metadata(path)
                .map(|stat| stat.is_file())
                .unwrap_or(false)
            {
                return Ok(path.clone());
            }
        }

        if let Some(id) = status_id {
            zed::set_language_server_installation_status(
                id,
                &LanguageServerInstallationStatus::CheckingForUpdate,
            );
        }

        let target = open_vsx_target()?;
        let (version, url) = Self::latest_release(target)?;

        let version_dir = format!("{SERVER_NAME}-{version}-{target}");
        let binary = binary_path_in(&version_dir);

        if !fs::metadata(&binary)
            .map(|stat| stat.is_file())
            .unwrap_or(false)
        {
            if let Some(id) = status_id {
                zed::set_language_server_installation_status(
                    id,
                    &LanguageServerInstallationStatus::Downloading,
                );
            }

            zed::download_file(&url, &version_dir, DownloadedFileType::Zip).map_err(|err| {
                format!("failed to download {SERVER_NAME} {version} for {target}: {err}")
            })?;
            zed::make_file_executable(&binary)?;

            Self::remove_other_versions(&version_dir);
        }

        if let Some(id) = status_id {
            zed::set_language_server_installation_status(
                id,
                &LanguageServerInstallationStatus::None,
            );
        }

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
            self.cached_binary_path = Some(path.clone());
            return Ok(path);
        }

        if let Some(path) = worktree.which(SERVER_NAME) {
            self.cached_binary_path = Some(path.clone());
            return Ok(path);
        }

        self.download_server(Some(language_server_id))
    }
}

impl zed::Extension for ShopwareLspExtension {
    fn new() -> Self {
        Self {
            cached_binary_path: None,
            cached_worktree_root: None,
        }
    }

    fn language_server_command(
        &mut self,
        language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        self.cached_worktree_root = Some(worktree.root_path());

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

    /// Expose the server's MCP tools to Zed's Agent Panel.
    ///
    /// `shopware-lsp mcp` refuses to start outside a Shopware or Symfony
    /// project, so the root matters. It is taken from the `root` setting first,
    /// then from whatever the language server reported, and otherwise left to
    /// the process working directory.
    fn context_server_command(
        &mut self,
        context_server_id: &ContextServerId,
        project: &Project,
    ) -> Result<zed::Command> {
        let settings = ContextServerSettings::for_project(context_server_id.as_ref(), project).ok();

        let command = settings.as_ref().and_then(|s| s.command.as_ref());

        // A full command override bypasses discovery entirely.
        if let Some(path) = command.and_then(|c| c.path.clone()) {
            return Ok(zed::Command {
                command: path,
                args: command
                    .and_then(|c| c.arguments.clone())
                    .unwrap_or_else(|| vec!["mcp".into()]),
                env: command
                    .and_then(|c| c.env.clone())
                    .map(|env| env.into_iter().collect())
                    .unwrap_or_default(),
            });
        }

        let root = settings
            .as_ref()
            .and_then(|s| s.settings.as_ref())
            .and_then(|s| s["root"].as_str())
            .map(str::to_string)
            .or_else(|| self.cached_worktree_root.clone());

        // Global flags have to precede the subcommand.
        let args = match root {
            Some(root) => vec!["-root".into(), root, "mcp".into()],
            None => vec!["mcp".into()],
        };

        Ok(zed::Command {
            command: self.download_server(None)?,
            args,
            env: command
                .and_then(|c| c.env.clone())
                .map(|env| env.into_iter().collect())
                .unwrap_or_default(),
        })
    }

    fn context_server_configuration(
        &mut self,
        _context_server_id: &ContextServerId,
        _project: &Project,
    ) -> Result<Option<ContextServerConfiguration>> {
        Ok(Some(ContextServerConfiguration {
            installation_instructions: include_str!("../docs/mcp-instructions.md").to_string(),
            settings_schema: include_str!("../docs/mcp-settings-schema.json").to_string(),
            default_settings: "{}\n".to_string(),
        }))
    }
}

zed::register_extension!(ShopwareLspExtension);
