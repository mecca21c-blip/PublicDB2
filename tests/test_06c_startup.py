from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import uvicorn
from fastapi.testclient import TestClient

from scripts import run_desktop
from tests.test_app import make_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependencies_cover_korea_timezone_and_desktop_extra():
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]
    desktop = metadata["project"]["optional-dependencies"]["desktop"]
    assert any(item.startswith("tzdata") for item in dependencies)
    assert any(item.startswith("pywebview") for item in desktop)
    assert ZoneInfo("Asia/Seoul").key == "Asia/Seoul"


def test_login_uses_compact_dedicated_styles_without_workspace_dependency(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        response = client.get("/login")
        login_css = client.get("/static/css/login.css")
    assert response.status_code == 200
    assert "login.css" in response.text
    assert "workspace.css" not in response.text
    assert "app.css" not in response.text
    assert 'class="login-page"' in response.text
    assert 'class="panel login-card"' in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text
    assert 'name="csrf_token"' in response.text
    assert "width: min(420px" in login_css.text
    assert ".login-control" in login_css.text
    assert "html.login-page" in login_css.text


def test_desktop_module_import_has_no_server_or_webview_side_effect(monkeypatch):
    monkeypatch.delitem(sys.modules, "webview", raising=False)
    module = importlib.reload(run_desktop)
    assert "webview" not in sys.modules
    assert module.APP_IMPORT == "app.main:app"


class FakeSocket:
    def __init__(self, port=49152):
        self.port = port
        self.closed = False

    def getsockname(self):
        return ("127.0.0.1", self.port)

    def close(self):
        self.closed = True


def test_server_prebinds_dynamic_loopback_port_and_stops_owned_uvicorn():
    socket = FakeSocket()
    calls = {}

    class Config:
        def __init__(self, app, **kwargs):
            calls["config"] = (app, kwargs)

        def bind_socket(self):
            return socket

    class Server:
        def __init__(self, config):
            self.should_exit = False
            self.force_exit = False
            calls["server"] = self

        def run(self, sockets):
            calls["sockets"] = sockets

    server = run_desktop.DesktopServer(lambda: SimpleNamespace(Config=Config, Server=Server))
    server.start()
    server._thread.join(timeout=1)
    assert calls["config"][0] == "app.main:app"
    assert calls["config"][1]["host"] == "127.0.0.1"
    assert calls["config"][1]["port"] == 0
    assert calls["config"][1]["log_config"] is None
    assert calls["config"][1]["access_log"] is False
    assert server.base_url == "http://127.0.0.1:49152"
    assert calls["sockets"] == [socket]
    server.stop()
    assert calls["server"].should_exit is True
    assert socket.closed is True


def test_desktop_uvicorn_config_constructs_without_console_streams(monkeypatch):
    class Server:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.force_exit = False

        def run(self, sockets):
            return None

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server = run_desktop.DesktopServer(
        lambda: SimpleNamespace(Config=uvicorn.Config, Server=Server),
    )
    try:
        server.start()
        server._thread.join(timeout=1)
        assert server._server.config.log_config is None
        assert server._server.config.access_log is False
    finally:
        server.stop()


def test_readiness_poll_is_bounded():
    ticks = iter([0.0, 0.0, 0.2, 0.4, 0.6])
    server = SimpleNamespace(
        base_url="http://127.0.0.1:49152", run_error=None, is_alive=True,
    )

    def unavailable(*args, **kwargs):
        raise OSError("not listening")

    with pytest.raises(run_desktop.DesktopStartupError) as captured:
        run_desktop.wait_for_readiness(
            server,
            timeout=0.5,
            interval=0,
            opener=unavailable,
            clock=lambda: next(ticks),
            sleeper=lambda _: None,
        )
    assert captured.value.code == "health-timeout"


def test_launch_creates_one_window_only_after_readiness_and_always_stops_server():
    events = []

    class Server:
        base_url = "http://127.0.0.1:54321"

        def start(self):
            events.append("server-start")

        def stop(self):
            events.append("server-stop")

    webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: events.append(("window", args, kwargs)),
        start=lambda: events.append("webview-start"),
    )

    run_desktop.launch_desktop(
        server_factory=Server,
        webview_loader=lambda: webview,
        readiness=lambda server: events.append(("ready", server.base_url)),
    )

    window = next(item for item in events if isinstance(item, tuple) and item[0] == "window")
    assert events[:2] == ["server-start", ("ready", "http://127.0.0.1:54321")]
    assert window[1] == ("PublicDB2", "http://127.0.0.1:54321")
    assert window[2] == {
        "width": 1360, "height": 860, "min_size": (1050, 700), "resizable": True,
    }
    assert events[-2:] == ["webview-start", "server-stop"]


def test_readiness_failure_never_loads_window_and_stops_server():
    events = []

    class Server:
        def start(self):
            events.append("server-start")

        def stop(self):
            events.append("server-stop")

    def fail(_server):
        raise run_desktop.DesktopStartupError("database unavailable", "database-not-ready")

    with pytest.raises(run_desktop.DesktopStartupError):
        run_desktop.launch_desktop(
            server_factory=Server,
            webview_loader=lambda: pytest.fail("window must not load"),
            readiness=fail,
        )
    assert events == ["server-start", "server-stop"]


def test_fatal_log_omits_exception_message_and_notifies_safely(tmp_path):
    notices = []
    error = RuntimeError("SECRET-SENTINEL")
    try:
        raise error
    except RuntimeError as caught:
        run_desktop.report_fatal_error(caught, project_root=tmp_path, notifier=notices.append)
    contents = (tmp_path / "logs" / "desktop-launcher.log").read_text(encoding="utf-8")
    assert "RuntimeError" in contents
    assert "SECRET-SENTINEL" not in contents
    assert notices and "logs" in notices[0]


def test_root_launcher_uses_pythonw_and_server_launcher_stays_independent():
    desktop_command = (PROJECT_ROOT / "PublicDB2.cmd").read_text(encoding="utf-8")
    server_script = (PROJECT_ROOT / "scripts" / "run_web.py").read_text(encoding="utf-8")
    assert '.venv\\Scripts\\pythonw.exe' in desktop_command
    assert 'scripts\\run_desktop.py' in desktop_command
    assert "webview" not in server_script
    assert "run_desktop" not in server_script
    assert 'default="127.0.0.1"' in server_script
    assert "default=8000" in server_script
