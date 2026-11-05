# Screen Vision MCP

On-demand screen perception and interaction for AI agents, delivered as a Model Context Protocol (MCP) server. Windows-native, local-only, and fully audited.

## Features

| Area | Tools | Method |
| --- | --- | --- |
| Vision | `screenshot`, `list_monitors`, `active_window`, `watch_*`, `diff_frames` | Pixel capture |
| OCR + geometry | `read_screen_text`, `find_text`, `click_by_text`, `type_and_verify`, `ui_alerts` | Local OCR |
| Desktop tree | `uia_tree`, `uia_click`, `uia_get_value` | Windows UI Automation |
| Input | `mouse_move`, `mouse_click`, `mouse_drag`, `scroll`, `type_text`, `press_key` | `SendInput` |
| Flows & profiles | `run_flow`, `save_profile`, `get_profile` | JSON flow engine |
| Governance & health | `set_autonomy`, `get_autonomy`, `doctor` | Mode gate + self-check |

## Installation

```bash
pip install -e ./screen-vision-mcp
screen-vision-mcp  # run the MCP server over stdio
```

## Configuration

```json
{
  "mcp": {
    "servers": {
      "screen": {
        "type": "local",
        "command": ["python", "<path>/screen_server.py"]
      }
    }
  }
}
```

See [`../screen_manifest.json`](../screen_manifest.json) for full tool contracts.

## Design Principles

1. **On-demand only.** Nothing captures or clicks unless invoked; the watcher retains 12 frames max.
2. **Local-only.** No network access; data stays on disk.
3. **Focus-guarded.** Tools abort on unexpected active-window changes.
4. **Verify-after-act.** Typed content and submissions are confirmed before proceeding.
5. **Auditable.** Actions are logged to `control_log.jsonl`.

## Testing

```bash
pytest tests/test_core.py -q
```

## License

Apache 2.0. See `../LICENSE.md`.
