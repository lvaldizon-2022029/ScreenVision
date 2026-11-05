# Screen Vision MCP

**Personal AI, on personal devices** — un servidor MCP que le da al agente *ojos y manos* sobre la pantalla del usuario, 100% local.

## Qué hace

| Grupo | Herramientas | Vía |
|---|---|---|
| Visión | `screenshot`, `list_monitors`, `active_window`, `watch_*`, `diff_frames` | píxeles |
| OCR+geometría | `read_screen_text`, `find_text`, `click_by_text`, `type_and_verify`, `ui_alerts` | OCR + reglas de rol/posición |
| DOM de escritorio | `uia_tree`, `uia_click`, `uia_get_value` | Windows **UI Automation** (nativo + Chromium) |
| Entrada | `mouse_*`, `type_text`, `press_key`, `window_list`, `focus_window` | SendInput |
| Flujos | `run_flow` (mirar→actuar→verificar con asserts, retries y abort), perfiles `save/get_profile` | engine |
| Gobernanza | `set_autonomy demo|full` (confirm en envíos), `doctor` (self-check), log JSONL de cada acción | auditoría |

## Instalar (pipx/pip)

```
python -m pip install -e ~/.config/opencode/mcp-servers/screen-vision-mcp
screen-vision-mcp   # corre el servidor MCP por stdio
```

## Conectarlo a un agente (OpenCode)

```json
{ "mcp": { "servers": { "screen": {
  "type": "local",
  "command": ["python", "~/.config/opencode/mcp-servers/screen_server.py"] } } } }
```

## Principios

1. **On-demand, no espía**: nada corre en background salvo el watcher visible (`watch_start`), que guarda máx. 12 frames.
2. **Cero red**: todo queda en disco (`<temp>/opencode/screen-vision/`).
3. **Assert de foco en cada paso**: el agente aborta antes que teclear a la ventana equivocada (aprendido con WhatsApp Web, 2026-11-05).
4. **Verificación post-acción**: OCR/`uia_get_value` confirma lo tecleado; el envío se comprueba con la burbuja.
5. **Exclusion de textos de error** en selección de candidatos: la franja "Contraseña vacía..." no es un campo.

## Historial de lecciones (tests que lo prueban)

`tests/test_core.py` — 12 tests: `pick_candidate` (rol, posición relativa, exclusion de alertas),
rondtrip de autonomía, clipboard 64-bit (`restype` explícito o access violation), manifest↔servidor.

## Licencia

Apache 2.0.
