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

/// Must match the server's own `ClientProtocolVersion`. A mismatch makes
/// `initialize` fail outright, so the contract check pins it.
const CLIENT_PROTOCOL_VERSION: u32 = 1;

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
fn binary_path_for(dir: &str, os: Os) -> String {
    match os {
        Os::Windows => format!("{dir}/extension/{SERVER_NAME}.exe"),
        _ => format!("{dir}/extension/{SERVER_NAME}"),
    }
}

fn binary_path_in(dir: &str) -> String {
    binary_path_for(dir, zed::current_platform().0)
}

/// Map a platform to an Open VSX target triple.
///
/// musl is deliberately absent: the extension API cannot tell glibc from musl,
/// so Alpine users have to point `binary.path` at an `alpine-*` build themselves.
fn target_for(os: Os, arch: Architecture) -> Result<&'static str> {
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

fn open_vsx_target() -> Result<&'static str> {
    let (os, arch) = zed::current_platform();
    target_for(os, arch)
}

/// Read the version and artifact URL out of an Open VSX `/latest` payload.
fn parse_latest_release(target: &str, body: &[u8]) -> Result<(String, String)> {
    let payload: zed::serde_json::Value = zed::serde_json::from_slice(body)
        .map_err(|err| format!("Open VSX returned malformed JSON: {err}"))?;

    let version = payload["version"]
        .as_str()
        .ok_or_else(|| "Open VSX response has no version".to_string())?;
    let url = payload["files"]["download"]
        .as_str()
        .ok_or_else(|| format!("Open VSX has no download for {target}"))?;

    Ok((version.to_string(), url.to_string()))
}

/// Argument vector for the MCP server.
///
/// Global flags have to precede the subcommand; the binary rejects
/// `mcp -root ...` with "mcp takes no arguments".
fn mcp_args(root: Option<&str>) -> Vec<String> {
    match root {
        Some(root) => vec!["-root".into(), root.into(), "mcp".into()],
        None => vec!["mcp".into()],
    }
}

/// Whether a work-dir entry is an older managed download.
///
/// Deliberately requires the `shopware-lsp-` prefix rather than `shopware-lsp`,
/// so a binary someone dropped into the work directory is never deleted.
/// Tell the server which editor-side commands this client implements.
///
/// Zed's extension API cannot register commands, so the honest answer is none.
/// The server then drops every command-backed code action and code lens, which
/// is what stops ~20 generator entries appearing in the menu and doing nothing.
/// Diagnostic quickfixes are unaffected: they carry no command.
///
/// `presentationProfile` stays `full` because Zed has no PHP intelligence of
/// its own; `framework` is for hosts like PhpStorm that do.
fn default_initialization_options() -> zed::serde_json::Value {
    zed::serde_json::json!({
        "shopwareClient": {
            "protocolVersion": CLIENT_PROTOCOL_VERSION,
            "presentationProfile": "full",
            "supportedCommands": [],
        }
    })
}

/// Recursively overlay `overlay` onto `base`, so a user can override any single
/// key without having to restate the whole object.
fn merge_json(base: &mut zed::serde_json::Value, overlay: zed::serde_json::Value) {
    match (base, overlay) {
        (zed::serde_json::Value::Object(target), zed::serde_json::Value::Object(source)) => {
            for (key, value) in source {
                merge_json(
                    target.entry(key).or_insert(zed::serde_json::Value::Null),
                    value,
                );
            }
        }
        (target, source) => *target = source,
    }
}

fn is_superseded_download(name: &str, keep: &str) -> bool {
    name.strip_prefix(SERVER_NAME)
        .is_some_and(|rest| rest.starts_with('-'))
        && name != keep
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

        parse_latest_release(target, &response.body)
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
            if is_superseded_download(&name.to_string_lossy(), keep) {
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
        let mut options = default_initialization_options();
        if let Some(user) = LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.initialization_options)
        {
            merge_json(&mut options, user);
        }
        Ok(Some(options))
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

        let args = mcp_args(root.as_deref());

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

#[cfg(test)]
mod tests {
    use super::*;

    /// Fixture trimmed from a real https://open-vsx.org/api/.../latest response.
    const LATEST_JSON: &[u8] = br#"{
        "namespace": "shopware",
        "name": "shopware-lsp",
        "version": "0.3.52",
        "targetPlatform": "darwin-arm64",
        "files": {
            "download": "https://open-vsx.org/api/shopware/shopware-lsp/darwin-arm64/0.3.52/file/shopware.shopware-lsp-0.3.52@darwin-arm64.vsix",
            "manifest": "https://open-vsx.org/api/shopware/shopware-lsp/darwin-arm64/0.3.52/file/package.json"
        }
    }"#;

    #[test]
    fn maps_every_published_platform_to_its_open_vsx_target() {
        // These strings are a wire contract with Open VSX. A typo here is a
        // download that 404s on someone else's machine.
        assert_eq!(
            target_for(Os::Mac, Architecture::Aarch64),
            Ok("darwin-arm64")
        );
        assert_eq!(target_for(Os::Mac, Architecture::X8664), Ok("darwin-x64"));
        assert_eq!(
            target_for(Os::Linux, Architecture::Aarch64),
            Ok("linux-arm64")
        );
        assert_eq!(target_for(Os::Linux, Architecture::X8664), Ok("linux-x64"));
        assert_eq!(
            target_for(Os::Windows, Architecture::X8664),
            Ok("win32-x64")
        );
    }

    #[test]
    fn rejects_platforms_without_a_published_build() {
        // No 32-bit builds exist, and Windows on ARM is not published either.
        for (os, arch) in [
            (Os::Mac, Architecture::X86),
            (Os::Linux, Architecture::X86),
            (Os::Windows, Architecture::X86),
            (Os::Windows, Architecture::Aarch64),
        ] {
            let error = target_for(os, arch).expect_err("must not invent a target");
            assert!(
                error.contains("binary.path"),
                "the error has to name the escape hatch, got: {error}"
            );
        }
    }

    #[test]
    fn locates_the_binary_inside_the_vsix() {
        // The vsix puts everything under extension/; the archive root only
        // holds extension.vsixmanifest and [Content_Types].xml.
        assert_eq!(
            binary_path_for("shopware-lsp-0.3.52-darwin-arm64", Os::Mac),
            "shopware-lsp-0.3.52-darwin-arm64/extension/shopware-lsp"
        );
        assert_eq!(
            binary_path_for("dir", Os::Linux),
            "dir/extension/shopware-lsp"
        );
        assert_eq!(
            binary_path_for("dir", Os::Windows),
            "dir/extension/shopware-lsp.exe"
        );
    }

    #[test]
    fn reads_version_and_download_url_from_open_vsx() {
        let (version, url) = parse_latest_release("darwin-arm64", LATEST_JSON).unwrap();
        assert_eq!(version, "0.3.52");
        assert!(url.ends_with("@darwin-arm64.vsix"), "got {url}");
    }

    #[test]
    fn reports_which_part_of_the_open_vsx_payload_is_missing() {
        let err = parse_latest_release("darwin-arm64", b"not json").unwrap_err();
        assert!(err.contains("malformed JSON"), "got {err}");

        let err = parse_latest_release("darwin-arm64", br#"{"files":{"download":"u"}}"#)
            .expect_err("a payload without a version must fail");
        assert!(err.contains("version"), "got {err}");

        let err = parse_latest_release("linux-x64", br#"{"version":"1.0.0","files":{}}"#)
            .expect_err("a payload without a download must fail");
        assert!(
            err.contains("linux-x64"),
            "the error has to name the target, got: {err}"
        );
    }

    #[test]
    fn passes_the_root_before_the_mcp_subcommand() {
        // `mcp -root x` is rejected by the binary: "mcp takes no arguments;
        // use -root before the command".
        assert_eq!(
            mcp_args(Some("/srv/shop")),
            vec!["-root", "/srv/shop", "mcp"]
        );
    }

    #[test]
    fn falls_back_to_the_working_directory_without_a_known_root() {
        assert_eq!(mcp_args(None), vec!["mcp"]);
    }

    #[test]
    fn declares_no_editor_side_commands() {
        // Claiming a command Zed cannot run brings back the dead menu entries;
        // the empty list is what makes the server drop them.
        let options = default_initialization_options();
        let client = &options["shopwareClient"];
        assert_eq!(client["protocolVersion"], 1);
        assert_eq!(client["presentationProfile"], "full");
        assert_eq!(
            client["supportedCommands"].as_array().map(Vec::len),
            Some(0)
        );
    }

    #[test]
    fn user_initialization_options_override_the_defaults() {
        let mut options = default_initialization_options();
        merge_json(
            &mut options,
            zed::serde_json::json!({
                "shopwareClient": {"supportedCommands": ["shopware.openReferences"]},
                "allowUnsupportedProject": true,
            }),
        );

        // Overridden leaf.
        assert_eq!(
            options["shopwareClient"]["supportedCommands"][0],
            "shopware.openReferences"
        );
        // Sibling keys inside the same object survive the merge.
        assert_eq!(options["shopwareClient"]["protocolVersion"], 1);
        assert_eq!(options["shopwareClient"]["presentationProfile"], "full");
        // New top-level keys are added.
        assert_eq!(options["allowUnsupportedProject"], true);
    }

    #[test]
    fn prunes_only_older_managed_downloads() {
        let keep = "shopware-lsp-0.3.52-darwin-arm64";

        assert!(is_superseded_download(
            "shopware-lsp-0.3.50-darwin-arm64",
            keep
        ));
        assert!(is_superseded_download(
            "shopware-lsp-0.3.52-linux-x64",
            keep
        ));

        assert!(
            !is_superseded_download(keep, keep),
            "must keep the current version"
        );
        // A binary dropped into the work dir by hand, and unrelated neighbours.
        assert!(!is_superseded_download("shopware-lsp", keep));
        assert!(!is_superseded_download("shopware-lsp.exe", keep));
        assert!(!is_superseded_download("some-other-server-1.0", keep));
    }
}
