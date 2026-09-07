use zed_extension_api::{self as zed, settings::LspSettings, LanguageServerId, Result};

const SERVER_NAME: &str = "shopware-lsp";

struct ShopwareLspExtension;

impl ShopwareLspExtension {
    /// Resolve the shopware-lsp executable.
    ///
    /// Order: an explicit `lsp.shopware-lsp.binary.path` from Zed settings,
    /// then the worktree PATH, then the well-known install location.
    fn server_binary(&self, worktree: &zed::Worktree) -> Result<String> {
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

        Err(format!(
            "{SERVER_NAME} not found on PATH. Install it to ~/.local/bin/{SERVER_NAME} or set \
             lsp.{SERVER_NAME}.binary.path in your Zed settings."
        ))
    }
}

impl zed::Extension for ShopwareLspExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        _language_server_id: &LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        let arguments = LspSettings::for_worktree(SERVER_NAME, worktree)
            .ok()
            .and_then(|settings| settings.binary)
            .and_then(|binary| binary.arguments)
            .unwrap_or_default();

        Ok(zed::Command {
            command: self.server_binary(worktree)?,
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
