"""Run PublicDB2 in a single local desktop window."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOOPBACK_HOST = "127.0.0.1"
APP_IMPORT = "app.main:app"
READINESS_TIMEOUT_SECONDS = 20.0
DESKTOP_MUTEX_NAME = "Local\\PublicDB2.Desktop"
ERROR_ALREADY_EXISTS = 183


class DesktopStartupError(RuntimeError):
    """An expected startup failure safe to summarize for the user."""

    def __init__(self, public_message: str, code: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.code = code


class DesktopInstanceGuard:
    """Hold an OS-owned mutex for one logical Windows desktop instance."""

    def __init__(
        self,
        name: str = DESKTOP_MUTEX_NAME,
        kernel32_loader: Callable[[], object] | None = None,
        last_error_getter: Callable[[], int] | None = None,
    ) -> None:
        self.name = name
        self._kernel32_loader = kernel32_loader
        self._last_error_getter = last_error_getter
        self._kernel32 = None
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        kernel32 = (
            self._kernel32_loader()
            if self._kernel32_loader
            else ctypes.WinDLL("kernel32", use_last_error=True)
        )
        create_mutex = kernel32.CreateMutexW
        if hasattr(create_mutex, "argtypes"):
            create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
            create_mutex.restype = ctypes.c_void_p
        if self._last_error_getter is None:
            ctypes.set_last_error(0)
        handle = create_mutex(None, False, self.name)
        if not handle:
            raise DesktopStartupError(
                "PublicDB2 실행 상태를 확인하지 못했습니다.",
                "instance-guard-failed",
            )
        get_last_error = self._last_error_getter or ctypes.get_last_error
        if get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class DesktopServer:
    """Own a Uvicorn server and its collision-safe pre-bound socket."""

    def __init__(self, uvicorn_loader: Callable[[], ModuleType] | None = None) -> None:
        self._uvicorn_loader = uvicorn_loader or _load_uvicorn
        self._server = None
        self._socket = None
        self._thread: threading.Thread | None = None
        self._run_error: BaseException | None = None
        self.port: int | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise DesktopStartupError("로컬 서버가 아직 시작되지 않았습니다.", "server-not-started")
        return f"http://{LOOPBACK_HOST}:{self.port}"

    @property
    def run_error(self) -> BaseException | None:
        return self._run_error

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        uvicorn = self._uvicorn_loader()
        config = uvicorn.Config(
            APP_IMPORT,
            host=LOOPBACK_HOST,
            port=0,
            reload=False,
            proxy_headers=True,
            forwarded_allow_ips=LOOPBACK_HOST,
            log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._socket = config.bind_socket()
        self.port = int(self._socket.getsockname()[1])
        self._thread = threading.Thread(target=self._run, name="publicdb2-uvicorn")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        except BaseException as error:  # recorded and surfaced by readiness polling
            self._run_error = error

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=8.0)
            if self._thread.is_alive() and self._server is not None:
                self._server.force_exit = True
                self._thread.join(timeout=2.0)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass


def _load_uvicorn() -> ModuleType:
    import uvicorn

    return uvicorn


def load_webview() -> ModuleType:
    try:
        import webview
    except ImportError as error:
        raise DesktopStartupError(
            "데스크톱 구성 요소가 없습니다. 명령 프롬프트에서 "
            '.venv\\Scripts\\python.exe -m pip install -e ".[desktop]" 를 실행해 주세요.',
            "desktop-dependency-missing",
        ) from error
    return webview


def wait_for_readiness(
    server: DesktopServer,
    timeout: float = READINESS_TIMEOUT_SECONDS,
    interval: float = 0.1,
    opener: Callable[..., object] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout
    health_ready = False
    while clock() < deadline:
        if server.run_error is not None or not server.is_alive:
            raise DesktopStartupError(
                "로컬 서버를 시작하지 못했습니다. logs 폴더의 진단 기록을 확인해 주세요.",
                "server-start-failed",
            )
        try:
            with opener(f"{server.base_url}/health", timeout=1.0) as response:
                health_ready = getattr(response, "status", None) == 200
            if health_ready:
                break
        except (OSError, urllib.error.URLError):
            pass
        sleeper(interval)
    if not health_ready:
        raise DesktopStartupError(
            "로컬 서버 준비 시간이 초과되었습니다. logs 폴더의 진단 기록을 확인해 주세요.",
            "health-timeout",
        )

    try:
        with opener(f"{server.base_url}/ready", timeout=2.0) as response:
            ready = getattr(response, "status", None) == 200
    except urllib.error.HTTPError as error:
        ready = error.code == 200
    except (OSError, urllib.error.URLError):
        ready = False
    if not ready:
        raise DesktopStartupError(
            "데이터베이스가 준비되지 않았습니다. 서버 모드에서 마이그레이션 상태를 확인해 주세요. "
            "데스크톱 실행기는 마이그레이션을 자동 수행하지 않습니다.",
            "database-not-ready",
        )


def launch_desktop(
    server_factory: Callable[[], DesktopServer] = DesktopServer,
    webview_loader: Callable[[], ModuleType] = load_webview,
    readiness: Callable[[DesktopServer], None] = wait_for_readiness,
) -> None:
    server = server_factory()
    try:
        server.start()
        readiness(server)
        webview = webview_loader()
        webview.create_window(
            "PublicDB2",
            server.base_url,
            width=1360,
            height=860,
            min_size=(1050, 700),
            resizable=True,
        )
        webview.start()
    finally:
        server.stop()


def _show_native_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, message, "PublicDB2 시작 오류", 0x10)
    else:
        print(message, file=sys.stderr)


def _show_already_running() -> None:
    message = "PublicDB2가 이미 실행 중입니다."
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, message, "PublicDB2", 0x40)
    else:
        print(message, file=sys.stderr)


def report_fatal_error(
    error: BaseException,
    project_root: Path = PROJECT_ROOT,
    notifier: Callable[[str], None] = _show_native_error,
) -> None:
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    code = error.code if isinstance(error, DesktopStartupError) else "unexpected-startup-error"
    diagnostic = [
        f"timestamp={time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"code={code}",
        f"exception_type={type(error).__name__}",
        "traceback_frames:",
        *traceback.format_tb(error.__traceback__),
    ]
    (log_dir / "desktop-launcher.log").write_text("\n".join(diagnostic), encoding="utf-8")
    if isinstance(error, DesktopStartupError):
        message = error.public_message
    else:
        message = "PublicDB2를 시작하지 못했습니다. logs 폴더의 진단 기록을 확인해 주세요."
    notifier(message)


def main(
    guard_factory: Callable[[], DesktopInstanceGuard] = DesktopInstanceGuard,
    launcher: Callable[[], None] = launch_desktop,
    already_running_notifier: Callable[[], None] = _show_already_running,
    fatal_reporter: Callable[[BaseException], None] = report_fatal_error,
) -> int:
    guard = guard_factory()
    try:
        if not guard.acquire():
            already_running_notifier()
            return 0
        launcher()
    except BaseException as error:
        fatal_reporter(error)
        return 1
    finally:
        guard.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
