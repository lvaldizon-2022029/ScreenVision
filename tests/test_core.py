"""Core unit tests for screen_server v2 (no mouse/keyboard side effects)."""
import json
import os
import sys

SERVER_DIR = r"C:\Users\practicante1\.config\opencode\mcp-servers"
sys.path.insert(0, SERVER_DIR)

import screen_server as srv  # noqa: E402
import pytest  # noqa: E402


# ---------- Fase 1: pick_candidate ---------- #

def _c(text, x, y):
    return {"text": text, "center": [x, y]}


def test_pick_candidate_role_button_prefers_keyword():
    cands = [_c("Ingresa la contraseña", 676, 493), _c("Desbloquear", 677, 577)]
    chosen = srv.pick_candidate(cands, role="button")
    assert chosen["text"] == "Desbloquear"


def test_pick_candidate_rel_field_above_button():
    # caso real de WhatsApp: campo 'contrasena' ~84px sobre el boton
    cands = [
        _c("El bloqueo está activado. contrase", 660, 416),
        _c("Ingresa la contraseña", 676, 493),
        _c("Desbloquear", 677, 577),
    ]
    chosen = srv.pick_candidate(
        cands, role="field",
        rel={"anchor_query": "desbloquear", "min_dy": -100, "max_dy": -60})
    assert chosen["text"] == "Ingresa la contraseña"
    assert chosen["center"] == [676, 493]


def test_pick_candidate_rel_out_of_range_falls_back_to_closest():
    cands = [_c("Ingresa la contraseña", 676, 200), _c("Desbloquear", 677, 577)]
    chosen = srv.pick_candidate(
        cands, role="field",
        rel={"anchor_query": "desbloquear", "min_dy": -100, "max_dy": -60})
    assert chosen is not None  # hay ancla; fuera de rango elige la mas cercana al ancla


def test_pick_candidate_no_anchor_match_returns_none():
    cands = [_c("Desbloquear", 677, 577)]
    chosen = srv.pick_candidate(cands, rel={"anchor_query": "noexiste", "min_dy": -10, "max_dy": 10})
    assert chosen is None


def test_pick_candidate_error_text_is_not_a_field():
    # leccion de hoy: 'Contrasena vacia...' (mas cerca del boton) no debe ganar
    cands = [
        _c("Contrasena vacia. Vuelve a intentarlo.", 647, 529),
        _c("Ingresa la contraseña", 676, 493),
        _c("Desbloquear", 678, 578),
    ]
    chosen = srv.pick_candidate(
        cands, role="field",
        rel={"anchor_query": "desbloquear", "min_dy": -100, "max_dy": -40})
    assert "vacia" not in chosen["text"].lower()


# ---------- herramientas / registro ---------- #

def test_module_exposes_v2_tools():
    for name in ("click_by_text", "type_and_verify", "uia_tree", "uia_click",
                 "uia_get_value", "diff_frames", "run_flow", "ui_alerts",
                 "doctor", "save_profile", "get_profile", "set_autonomy",
                 "get_autonomy"):
        assert hasattr(srv, name), name


@pytest.mark.skipif(not hasattr(srv.mcp, "_tool_manager") or srv.mcp._tool_manager is None,
                    reason="FastMCP internals unavailable")
def test_mcp_registry_contains_v2_tools():
    try:
        names = set(srv.mcp._tool_manager._tools.keys())
    except Exception:
        pytest.skip("no registry introspection")
    for name in ("doctor", "run_flow", "uia_click"):
        assert name in names


def test_run_flow_invalid_step_rejected():
    out = json.loads(srv.run_flow([{"nada": 1}]))
    assert out["ok"] is False


def test_run_flow_alerts_and_abort(tmp_path):
    steps = [{"op": "alerts"}]  # en una pantalla limpia no debe abortar
    out = json.loads(srv.run_flow(steps))
    assert "steps" in out


# ---------- autonomia ---------- #

def test_autonomy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_AUTONOMY_FILE", str(tmp_path / "autonomy.json"))
    assert json.loads(srv.set_autonomy("full"))["ok"]
    assert json.loads(srv.get_autonomy())["mode"] == "full"
    assert json.loads(srv.set_autonomy("demo"))["ok"]
    assert json.loads(srv.set_autonomy("otro"))["ok"] is False
    assert "error" in json.loads(srv.set_autonomy("otro"))


# ---------- clipboard ---------- #

def test_clipboard_set_and_read():
    assert srv._clipboard_set("SV2-test-123") is True
    # read with win32; 64-bit handles must be void_p (int truncation -> crash)
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    assert user32.OpenClipboard(0)
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        assert h, "no clipboard handle"
        n = kernel32.GlobalSize(h)  # bytes incl. L'\0'
        assert n >= len("SV2-test-123") * 2 + 2
        data = ctypes.wstring_at(h, n // 2 - 1)
        assert data == "SV2-test-123"
    finally:
        user32.CloseClipboard()


# ---------- manifest v2 ---------- #

def test_manifest_v2_matches_server():
    path = os.path.join(SERVER_DIR, "screen_manifest.json")
    man = json.load(open(path, encoding="utf-8"))
    assert man["version"] == "2.0.0"
    all_tools = [t for g in man["tool_groups"].values() if isinstance(g, list) for t in g]
    for t in all_tools:
        assert hasattr(srv, t), f"{t} en manifest pero no en el servidor"
