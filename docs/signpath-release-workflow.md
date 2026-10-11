# Draft: SignPath signing step for release.yml (apply only after SignPath Foundation approval)

Not active. Once the project is approved, replace the "Build exe" -> "Publish release" steps in
`.github/workflows/release.yml` with the following, and add repo secret `SIGNPATH_API_TOKEN`
(token of a SignPath CI user with submitter permission). Placeholders: `<ORG_ID>`, `<PROJECT_SLUG>`,
`<SIGNING_POLICY_SLUG>` (e.g. `release-signing`), `<ARTIFACT_CONFIG_SLUG>`.

SignPath also needs the GitHub trusted build system linked to the project, and an artifact
configuration that enforces file metadata (ProductName = "Claude Usage Tray", ProductVersion = tag
version). PyInstaller sets no version resource by default, so add `--version-file version_info.txt`
generated from the tag in CI.

```yaml
      - name: Build exe
        run: pyinstaller --onefile --noconsole --name ClaudeUsage --version-file version_info.txt claude_usage_tray.py

      - name: Upload unsigned exe
        id: upload-unsigned
        uses: actions/upload-artifact@v4
        with:
          name: ClaudeUsage-unsigned
          path: dist/ClaudeUsage.exe

      - name: Sign with SignPath
        uses: signpath/github-action-submit-signing-request@v1
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: <ORG_ID>
          project-slug: <PROJECT_SLUG>
          signing-policy-slug: <SIGNING_POLICY_SLUG>
          artifact-configuration-slug: <ARTIFACT_CONFIG_SLUG>
          github-artifact-id: ${{ steps.upload-unsigned.outputs.artifact-id }}
          wait-for-completion: true          # waits for Shui's manual approval in SignPath
          wait-for-completion-timeout-in-seconds: 3600
          output-artifact-directory: signed

      - name: Publish release
        uses: softprops/action-gh-release@v2
        with:
          files: signed/ClaudeUsage.exe
```

Workflow also needs `permissions: actions: read` (alongside `contents: write`) so SignPath can fetch the artifact.
