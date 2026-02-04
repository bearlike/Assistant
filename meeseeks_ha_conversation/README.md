# Home Assistant Conversation Integration for Meeseeks 🚀

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

- Home Assistant Conversation Integration for Meeseeks. Can be used with HA Assist ⭐.
- Wrapped around the REST API Engine for Meeseeks. 100% coverage of Meeseeks API.
- This integration is optional and auto-disables if `HA_URL`/`HA_TOKEN` are missing or auth fails.
- No components are explicitly tested for safety or security. Use with caution in a production environment.
- For full setup and configuration, see `docs/getting-started.md`.

## Install (optional)
```bash
uv sync --extra ha
```

To use it in Home Assistant, install `meeseeks_ha_conversation/` as a custom component
and point it at the Meeseeks API.

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
