# Home Assistant Conversation Integration for Truss 🚀

<p align="center">
    <a href="https://github.com/bearlike/Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Assistant?style=for-the-badge&" alt="GitHub Release"></a>
</p>


<table align="center">
    <tr>
        <th>Answer questions and interpret sensor information</th>
        <th>Control devices and entities</th>
    </tr>
    <tr>
        <td align="center"><img src="../docs/screenshot_ha_assist_1.png" alt="Screenshot" height="512px"></td>
        <td align="center"><img src="../docs/screenshot_ha_assist_2.png" alt="Screenshot" height="512px"></td>
    </tr>
</table>

- Home Assistant Conversation integration for Truss (works with HA Assist).
- Wrapped around the Truss REST API for synchronous conversations.
- This integration is optional and auto-disables if `home_assistant.enabled` is false or credentials are missing in `configs/app.json`.
- No components are explicitly tested for safety or security. Use with caution in a production environment.
- For full setup and configuration, see `docs/getting-started.md`.

## Install (optional)
```bash
uv sync --extra ha
```

To use it in Home Assistant, install `truss_ha_conversation/` as a custom component
and point it at the Truss API URL + API key.

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
