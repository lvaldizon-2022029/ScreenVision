# Screen Vision

On-demand screen perception and interaction for AI agents, delivered as a Model Context Protocol (MCP) server. Windows-native, local-only, and fully audited. No background monitoring by default: nothing runs unless a tool is invoked.

## Overview

Agents typically reason about applications through pixels alone, which is slow and ambiguous. Screen Vision combines three perception paths so an agent can act with high confidence:

1. **UI Automation** — reads and drives real controls by name via Windows UI Automation (`pywinauto`).
2. **OCR + geometry** — reads visible text and returns click coordinates, with explicit role and relative-position rules.
3. **Flow engine** — declarative `run_flow` steps with focus assertions, retries, and abort-on-mismatch so every action is verified before the next one.

## Features

| Area | Tools | Method |
| --- | --- | --- |
| Vision | `screenshot`, `list_monitors`, `active_window`, `watch_*`, `diff_frames` | Pixel capture |
| OCR + geometry | `read_screen_text`, `find_text`, `click_by_text`, `type_and_verify`, `ui_alerts` | Local OCR (`RapidOCR`) |
| Desktop tree | `uia_tree`, `uia_click`, `uia_get_value` | Windows UI Automation |
| Input | `mouse_move`, `mouse_click`, `mouse_drag`, `scroll`, `type_text`, `press_key`, `window_list`, `focus_window` | `SendInput` |
| Flows & profiles | `run_flow`, `save_profile`, `get_profile` | JSON flow engine |
| Governance & health | `set_autonomy`, `get_autonomy`, `doctor` | Mode gate + self-check |

## Design Principles

1. **On-demand only.** Captures and input occur only when a tool is invoked. The optional watcher keeps only the 12 most recent frames.
2. **Local-only.** No network access. Frames, profiles, and logs remain on disk under `AppData\Local\Temp\opencode\screen-vision\`.
3. **Focus-guarded.** Control tools accept `expect_window`; on focus mismatch the agent aborts before sending input to the wrong application.
4. **Verify-after-act.** Typed text is re-read via OCR, UI values via `uia_get_value`, and submissions are confirmed on screen before `Enter` is pressed.
5. **Alert-aware.** Error and confirmation dialog text is excluded from click-target selection; `ui_alerts` surfaces failure feedback proactively.
6. **Auditable.** Every input action is appended to `control_log.jsonl` with timestamp and parameters.

## Installation

Requires Python 3.10+ on Windows, plus `mcp<2`, `pillow`, and `pywinauto`.

```bash
pip install -e ./screen-vision-mcp
```

## Usage

Connect to any MCP client over stdio:

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

Full tool contracts are specified in [`screen_manifest.json`](screen_manifest.json).

### Example flow

```json
[
  { "op": "assert", "window": "notepad" },
  { "op": "click_text", "query": "Save", "role": "button" },
  { "op": "type", "text": "Report", "expect_input_query": "File name" },
  { "op": "alerts" },
  { "op": "press", "combo": "enter", "confirm": true },
  { "op": "verify", "query": "Report" }
]
```

Tool autodiscovery: the server registers all tools under the `screen` namespace on client connect.

## Testing

```bash
pytest tests/test_core.py -q
```

Test coverage includes candidate selection (role, anchor-relative geometry, alert exclusion), autonomy round-trip, 64-bit clipboard handling, and manifest-to-server consistency.

## License

Apache 2.0. See `LICENSE`.
