# Screen Vision

**Personal AI, on personal devices** — en el espíritu de OpenJarvis ([open-jarvis/OpenJarvis](https://github.com/open-jarvis/OpenJarvis)):
un servidor MCP que le da a un agente *ojos y manos* sobre la pantalla del usuario, 100% local, on-demand — nada corriendo sin que lo pidas.

| | |
|---|---|
| **Tipo** | MCP server (FastMCP sobre stdio), no skill |
| **Runtime** | Windows, Python 3.10+, `mcp<2` + Pillow + pywinauto |
| **Instalable** | `pipx install ./screen-vision-mcp` o ejecutar `screen_server.py` directo |
| **Compatibilidad** | Cualquier cliente MCP estándar (OpenCode, Claude Desktop, …) — ver `screen_manifest.json` |

## Qué hace

| Grupo | Herramientas | Vía |
|---|---|---|
| **Visión** | `screenshot`, `list_monitors`, `active_window`, `watch_*`, `diff_frames` | píxeles |
| **OCR+geometría** | `read_screen_text`, `find_text`, `click_by_text`, `type_and_verify`, `ui_alerts` | OCR + reglas de rol/posición |
| **DOM de escritorio** | `uia_tree`, `uia_click`, `uia_get_value` | Windows **UI Automation** (nativo + Chromium) |
| **Entrada** | `mouse_move/click/drag`, `scroll`, `type_text`, `press_key`, `window_list`, `focus_window` | SendInput |
| **Flujos y perfiles** | `run_flow` (mirar→actuar→verificar con asserts/retries/abort), `save_profile` / `get_profile` | engine |
| **Gobernanza y salud** | `set_autonomy demo/full`, `get_autonomy`, `doctor`, log JSONL de auditoría | — |

## Principios

1. **On-demand, no espía**: nada corre en background salvo el watcher explícito (`watch_start`), que guarda máx. 12 frames.
2. **Cero red**: todo queda en disco (`<temp>/opencode/screen-vision/`).
3. **Assert de foco en cada paso**: si la ventana activa cambia, el agente aborta antes de teclear al lugar equivocado.
4. **Verificación post-acción**: OCR/`uia_get_value` confirma lo tecleado; el envío se comprueba en pantalla.
5. **Exclusión de textos de error** en la selección de candidatos: “Contraseña vacía…” no es un campo.
6. **Auditable**: cada acción queda en `control_log.jsonl`.

## Uso rápido

```python
# Type el descubrimiento: OCR encuentra 'GLM' en [510, 678] -> click sin adivinar
json.loads(click_by_text("GLM"))
```

Conectar a un cliente MCP (OpenCode):

```json
{ "mcp": { "servers": { "screen": {
  "type": "local",
  "command": ["python", "<ruta>/screen_server.py"] } } } }
```

Las tool descubribles y sus contratos están en [`screen_manifest.json`](screen_manifest.json).

## Pruebas (12/12)

```
C:\Users\practicante1\AppData\Local\Python\pythoncore-3.12-64\python.exe `
  -m pytest tests/test_core.py -q
```

Lecciones convertidas en código en los tests: `pick_candidate` (rol, posición
relativa, exclusión de alertas), clipboard 64-bit (handles sin truncar) y
`access violation`, manifiesto↔servidor.

## Lecciones aprendidas (de la batalla de WhatsApp)

Todo esto nació de una sesión real manipulando WhatsApp Web: yendo de
"funciona" a "funciona sin errores" (foco verificado antes de teclear,
campo verificado por OCR tras teclear, burbuja verificada tras enviar).

## Licencia

Apache 2.0 (ver `LICENSE`). Esta versión de Screen Vision no está afiliada a OpenJarvis.
