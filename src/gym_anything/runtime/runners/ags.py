from __future__ import annotations

import io
import os
import shlex
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ...contracts import RunnerRuntimeInfo
from ...specs import EnvSpec, MountSpec
from .base import BaseRunner


class AGSRunner(BaseRunner):
    """Run Gym Anything environments on AGS through the E2B/envd API."""

    skip_pre_start = True

    def __init__(self, spec: EnvSpec):
        super().__init__(spec)
        self.sandbox = None
        self.instance_name: Optional[str] = None
        self._running = False
        self._env_root: Optional[Path] = None
        self._task_root: Optional[Path] = None
        self._display = os.environ.get("GYM_ANYTHING_AGS_DISPLAY", ":1")
        self._template = (
            os.environ.get("GYM_ANYTHING_AGS_TEMPLATE")
            or os.environ.get("GYM_ANYTHING_AGS_TOOL_NAME")
            or getattr(spec, "image", None)
        )
        self._timeout = int(os.environ.get("GYM_ANYTHING_AGS_TIMEOUT", "3600"))
        self._create_timeout = int(os.environ.get("GYM_ANYTHING_AGS_CREATE_TIMEOUT", "300"))
        self._create_retries = int(os.environ.get("GYM_ANYTHING_AGS_CREATE_RETRIES", "2"))
        self._keep_alive = os.environ.get("GYM_ANYTHING_AGS_KEEP_ALIVE", "").lower() in {"1", "true", "yes"}

    def set_roots(self, env_root: Optional[os.PathLike], task_root: Optional[os.PathLike]) -> None:
        self._env_root = Path(env_root).resolve() if env_root else None
        self._task_root = Path(task_root).resolve() if task_root else None

    def start(self, seed: Optional[int] = None) -> None:
        del seed
        if self._running:
            return
        if not self._template:
            raise RuntimeError(
                "AGSRunner requires a template/tool name. Set GYM_ANYTHING_AGS_TEMPLATE "
                "or GYM_ANYTHING_AGS_TOOL_NAME."
            )
        # Credentials: AGS_API_KEY / AGS_DOMAIN are the canonical names (shared
        # with the OSWorld AGS provider, see
        # nexus/extensions/tasks/osworld/generator.py::_build_runtime_provider_config);
        # E2B_API_KEY / E2B_DOMAIN are accepted as fallbacks for backwards
        # compatibility with older deployments.
        api_key = os.environ.get("AGS_API_KEY") or os.environ.get("E2B_API_KEY")
        domain = os.environ.get("AGS_DOMAIN") or os.environ.get("E2B_DOMAIN")
        if not api_key:
            raise RuntimeError(
                "AGSRunner requires AGS_API_KEY (or fallback E2B_API_KEY) in the environment."
            )
        if not domain:
            raise RuntimeError(
                "AGSRunner requires AGS_DOMAIN (or fallback E2B_DOMAIN), "
                "e.g. ap-guangzhou.tencentags.com."
            )

        try:
            from e2b import Sandbox
        except ImportError as exc:
            raise RuntimeError("AGSRunner requires the e2b Python package.") from exc

        last_error: Optional[Exception] = None
        for attempt in range(1, self._create_retries + 2):
            try:
                self.sandbox = Sandbox.create(
                    template=self._template,
                    timeout=self._timeout,
                    metadata={"gym_anything_env": self.spec.id},
                    secure=True,
                    allow_internet_access=True,
                    request_timeout=self._create_timeout,
                    api_key=api_key,
                    domain=domain,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt > self._create_retries:
                    raise RuntimeError(f"Failed to create AGS sandbox from template {self._template!r}: {exc}") from exc
                time.sleep(min(10, 2 * attempt))
        if self.sandbox is None:
            raise RuntimeError(f"Failed to create AGS sandbox: {last_error}")

        self.instance_name = getattr(self.sandbox, "sandbox_id", None)
        self._running = True
        self._bootstrap_filesystem()
        self._upload_mounts()
        self._apply_runtime_compatibility()

    def stop(self) -> None:
        if not self._running:
            return
        if not self._keep_alive and self.sandbox is not None:
            try:
                self.sandbox.kill()
            except Exception:
                pass
        self._running = False
        self.sandbox = None

    def run_reset(self, reset_script: str, seed: Optional[int] = None) -> None:
        seed_env = {"SEED": str(seed)} if seed is not None else None
        self.exec(f"bash -lc {shlex.quote(reset_script)}", env=seed_env)

    def run_task_init(self, init_script: str) -> None:
        self.exec(f"bash -lc {shlex.quote(init_script)}")

    def inject_action(self, action: Dict[str, Any]) -> None:
        parts = []
        mouse = action.get("mouse")
        if mouse:
            if "left_click" in mouse:
                x, y = mouse["left_click"]
                parts.append(f"xdotool mousemove {int(x)} {int(y)} click 1")
            if "right_click" in mouse:
                x, y = mouse["right_click"]
                parts.append(f"xdotool mousemove {int(x)} {int(y)} click 3")
            if "double_click" in mouse:
                x, y = mouse["double_click"]
                parts.append(f"xdotool mousemove {int(x)} {int(y)} click --repeat 2 1")
            if "triple_click" in mouse:
                x, y = mouse["triple_click"]
                parts.append(f"xdotool mousemove {int(x)} {int(y)} click --repeat 3 1")
            if "left_click_drag" in mouse:
                (x1, y1), (x2, y2) = mouse["left_click_drag"]
                parts.append(
                    "python3 - <<'PY'\n"
                    "import pyautogui\n"
                    f"pyautogui.moveTo({int(x1)}, {int(y1)})\n"
                    f"pyautogui.dragTo({int(x2)}, {int(y2)}, duration=1.5, button='left')\n"
                    "PY"
                )
            if "right_click_drag" in mouse:
                (x1, y1), (x2, y2) = mouse["right_click_drag"]
                parts.append(
                    "python3 - <<'PY'\n"
                    "import pyautogui\n"
                    f"pyautogui.moveTo({int(x1)}, {int(y1)})\n"
                    f"pyautogui.dragTo({int(x2)}, {int(y2)}, duration=1.5, button='right')\n"
                    "PY"
                )
            if "move" in mouse:
                x, y = mouse["move"]
                parts.append(f"xdotool mousemove {int(x)} {int(y)}")
            buttons = mouse.get("buttons", {})
            if buttons.get("left_down"):
                parts.append("xdotool mousedown 1")
            if buttons.get("left_up"):
                parts.append("xdotool mouseup 1")
            if buttons.get("right_down"):
                parts.append("xdotool click 3")
            if "scroll" in mouse:
                dy = int(mouse["scroll"])
                click_code = 5 if dy > 0 else 4
                parts.extend(f"xdotool click {click_code}" for _ in range(abs(dy)))

        keyboard = action.get("keyboard")
        if keyboard:
            text = keyboard.get("text")
            keys = keyboard.get("keys")
            if text:
                parts.append(f"xdotool type --delay 1 {shlex.quote(text)}")
            if keys:
                if isinstance(keys, str):
                    keys = [keys]
                key_str = "+".join(self._normalize_key_name(str(k)) for k in keys)
                parts.append(f"xdotool key {shlex.quote(key_str)}")

        api_call = action.get("api_call")
        if api_call:
            name = api_call.get("name")
            args = api_call.get("args", {})
            argv = " ".join(f"--{k} {shlex.quote(str(v))}" for k, v in (args or {}).items())
            parts.append(f"python3 /workspace/env_api.py {shlex.quote(str(name))} {argv}")

        if parts:
            self.exec(" && ".join(parts))

    def capture_observation(self) -> Dict[str, Any]:
        obs: Dict[str, Any] = {}
        screen_spec = next((o for o in self.spec.observation if o.type == "rgb_screen"), None)
        if screen_spec:
            obs["screen"] = {
                "format": "rgb",
                "fps": screen_spec.fps,
                "resolution": screen_spec.resolution,
            }
        audio_spec = next((o for o in self.spec.observation if o.type == "audio_waveform"), None)
        if audio_spec:
            obs["audio"] = {
                "rate": audio_spec.sample_rate or 16000,
                "channels": audio_spec.channels or 1,
            }
        return obs

    def default_exec_env(self) -> Dict[str, str]:
        env = super().default_exec_env()
        env.setdefault("DISPLAY", self._display)
        env.setdefault("HOME", "/home/ga")
        env.setdefault("NPM_CONFIG_CACHE", "/tmp/ags-npm-cache")
        compat_path = "/tmp/ags_python_compat"
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{compat_path}:{existing_pythonpath}" if existing_pythonpath else compat_path
        return env

    def exec(
        self,
        cmd: str,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        use_pty: bool = True,
        timeout: int = 600,
    ) -> int:
        del use_pty
        try:
            result = self._run_command(cmd, env=env, user=user, timeout=timeout)
        except Exception as exc:
            log_tail = self._hook_log_tail()
            if log_tail:
                raise RuntimeError(f"AGS command failed: {cmd}\n{exc}{log_tail}") from exc
            raise
        if result.exit_code != 0:
            output = (result.stdout or "") + (result.stderr or "")
            output += self._hook_log_tail()
            raise RuntimeError(f"AGS command failed with exit code {result.exit_code}: {cmd}\n{output}")
        return result.exit_code

    def exec_async(self, cmd: str, env: Optional[Dict[str, str]] = None, stdout=None, stderr=None):
        del stdout, stderr
        return self._sandbox().commands.run(
            cmd,
            background=True,
            envs=self.merge_exec_env(env),
            user=self._exec_user(None),
            timeout=0,
            request_timeout=0,
        )

    def put_file(self, host_path) -> str:
        host_path = Path(host_path).resolve()
        dest = f"/tmp/ga_{uuid.uuid4().hex[:8]}_{host_path.name}"
        self.copy_to(str(host_path), dest)
        return dest

    def exec_capture(self, cmd: str) -> str:
        result = self._run_command(cmd, timeout=600)
        return (result.stdout or "") + (result.stderr or "")

    def exec_capture_bytes(self, cmd: str) -> bytes:
        remote_out = f"/tmp/ga_capture_{uuid.uuid4().hex}.bin"
        self.exec(f"{cmd} > {shlex.quote(remote_out)}")
        data = self._sandbox().files.read(remote_out, format="bytes", user=self._exec_user(None))
        try:
            self._sandbox().files.remove(remote_out, user=self._exec_user(None))
        except Exception:
            pass
        return bytes(data)

    def capture_screenshot(self, host_path) -> bool:
        host_path = Path(host_path).resolve()
        host_path.parent.mkdir(parents=True, exist_ok=True)
        remote_out = f"/tmp/ga_screenshot_{uuid.uuid4().hex}.png"
        screen_spec = next((o for o in self.spec.observation if o.type == "rgb_screen"), None)
        size_arg = ""
        if screen_spec and screen_spec.resolution:
            size_arg = f"-video_size {int(screen_spec.resolution[0])}x{int(screen_spec.resolution[1])}"
        command = (
            f"DISPLAY=${{DISPLAY:-{shlex.quote(self._display)}}} "
            f"ffmpeg -y -loglevel error -f x11grab {size_arg} -i $DISPLAY "
            f"-vframes 1 {shlex.quote(remote_out)}"
        )
        try:
            self.exec(command, timeout=30)
        except Exception:
            fallback = (
                f"DISPLAY=${{DISPLAY:-{shlex.quote(self._display)}}} "
                f"(scrot {shlex.quote(remote_out)} || import -window root {shlex.quote(remote_out)})"
            )
            self.exec(fallback, timeout=30)
        self.copy_from(remote_out, str(host_path))
        return host_path.exists()

    def capture_audio_raw(self, duration_sec: float, rate: int, channels: int) -> bytes:
        dur = max(0.05, float(duration_sec))
        cmd = (
            f"ffmpeg -hide_banner -loglevel error -f pulse -ac {int(channels)} -ar {int(rate)} "
            f"-t {dur:.3f} -i default -f s16le -"
        )
        return self.exec_capture_bytes(cmd)

    def copy_to(self, host_src: str, container_dst: str) -> None:
        src = Path(host_src).resolve()
        if not src.exists():
            raise FileNotFoundError(src)
        if src.is_dir():
            self._upload_directory_contents(src, container_dst)
            return
        self._sandbox().files.write(
            container_dst,
            src.read_bytes(),
            user=self._exec_user(None),
            request_timeout=300,
        )

    def copy_from(self, container_src: str, host_dst: str) -> None:
        dst = Path(host_dst).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self._remote_is_dir(container_src):
            dst.mkdir(parents=True, exist_ok=True)
            self._download_directory(container_src, dst)
            return
        data = self._sandbox().files.read(container_src, format="bytes", user=self._exec_user(None), request_timeout=300)
        dst.write_bytes(bytes(data))

    def capture_ui_tree(self) -> str:
        try:
            return self.exec_capture("xwininfo -root -tree || true")
        except Exception:
            return ""

    def get_runtime_info(self) -> RunnerRuntimeInfo:
        info = super().get_runtime_info()
        return RunnerRuntimeInfo(
            platform_family=info.platform_family,
            container_name=info.container_name,
            instance_name=self.instance_name,
            vnc_port=info.vnc_port,
            vnc_password=info.vnc_password,
            ssh_port=info.ssh_port,
            ssh_user=info.ssh_user,
            ssh_password=info.ssh_password,
        )

    def _sandbox(self):
        if self.sandbox is None:
            raise RuntimeError("AGS sandbox is not running")
        return self.sandbox

    def _exec_user(self, user: Optional[str]) -> str:
        if user:
            return user
        spec_user = getattr(getattr(self.spec, "security", None), "user", None)
        if spec_user and spec_user not in {"1000:1000", "ga"}:
            return str(spec_user)
        return os.environ.get("GYM_ANYTHING_AGS_USER", "root")

    def _run_command(
        self,
        cmd: str,
        *,
        env: Optional[Dict[str, str]] = None,
        user: Optional[str] = None,
        timeout: int = 600,
    ):
        return self._sandbox().commands.run(
            cmd,
            envs=self.merge_exec_env(env),
            user=self._exec_user(user),
            timeout=timeout,
            request_timeout=0 if timeout == 0 else max(timeout + 60, 300),
        )

    def _hook_log_tail(self) -> str:
        log_paths = (
            "/home/ga/task_pre_task.log",
            "/home/ga/task_post_task.log",
            "/home/ga/env_setup_pre_start.log",
            "/home/ga/env_setup_post_start.log",
            "/tmp/impress_task.log",
            "/tmp/writer.log",
            "/tmp/calc_circular_task.log",
            "/tmp/vscode_task.log",
            "/tmp/vlc_task.log",
        )
        chunks = []
        for path in log_paths:
            try:
                result = self._run_command(
                    f"test -s {shlex.quote(path)} && tail -n 120 {shlex.quote(path)} || true",
                    user="root",
                    timeout=30,
                )
            except Exception:
                continue
            text = (result.stdout or "") + (result.stderr or "")
            if text.strip():
                chunks.append(f"\n--- {path} ---\n{text}")
        return "".join(chunks)

    def _bootstrap_filesystem(self) -> None:
        self.exec("mkdir -p /workspace /home/ga /home/ga/Desktop /tmp /run/user/1000 && chmod 777 /tmp", timeout=60)
        self.exec("id ga >/dev/null 2>&1 || useradd -m -s /bin/bash ga || true", user="root", timeout=60)
        self.exec(
            r"""
install -d -o ga -g ga -m 700 /run/user/1000
install -d -o ga -g ga -m 700 /home/ga/.config/libreoffice
chown -R ga:ga /home/ga /run/user/1000 || true
cd /home/ga
runuser -u ga -- env HOME=/home/ga DISPLAY=:1 XDG_RUNTIME_DIR=/run/user/1000 SAL_USE_VCLPLUGIN=gen \
  timeout 45 libreoffice --headless --terminate_after_init >/tmp/ags-libreoffice-preinit.log 2>&1 || true
""",
            user="root",
            timeout=60,
        )

    def _upload_mounts(self) -> None:
        for mount in getattr(self.spec, "mounts", []) or []:
            self._upload_mount(mount)

    def _apply_runtime_compatibility(self) -> None:
        self.exec(
            r"""
mkdir -p /tmp/ags_python_compat
cat > /tmp/ags_python_compat/sitecustomize.py <<'PY'
import sys
import types

try:
    from odf.element import Element, IllegalChild
    if not hasattr(Element, "addAttribute"):
        def addAttribute(self, attr, value):
            return self.setAttribute(attr, value)
        Element.addAttribute = addAttribute
    _ags_original_add_element = Element.addElement
    def addElement(self, element, check_grammar=True):
        try:
            return _ags_original_add_element(self, element, check_grammar)
        except IllegalChild:
            if getattr(self, "tagName", "") == "draw:frame" and getattr(element, "tagName", "") == "text:p":
                from odf.draw import TextBox
                box = TextBox()
                _ags_original_add_element(box, element, check_grammar)
                return _ags_original_add_element(self, box, check_grammar)
            return _ags_original_add_element(self, element, False)
            raise
    Element.addElement = addElement
except Exception:
    pass

try:
    import odf.draw as _ags_odf_draw
    _ags_original_page = _ags_odf_draw.Page
    _ags_original_image = _ags_odf_draw.Image
    _ags_original_frame = _ags_odf_draw.Frame
    def Page(**args):
        args.setdefault("masterpagename", "Default")
        return _ags_original_page(**args)
    def Frame(**args):
        args.pop("presentationclass", None)
        return _ags_original_frame(**args)
    def Image(**args):
        geometry = {}
        for key in ("x", "y", "width", "height"):
            if key in args:
                geometry[key] = args.pop(key)
        image = _ags_original_image(**args)
        if geometry:
            frame = Frame(**geometry)
            frame.addElement(image)
            return frame
        return image
    _ags_odf_draw.Page = Page
    _ags_odf_draw.Frame = Frame
    _ags_odf_draw.Image = Image
except Exception:
    pass

try:
    if "odf.base" not in sys.modules:
        base = types.ModuleType("odf.base")
        base.Double = float
        sys.modules["odf.base"] = base
except Exception:
    pass
PY
cp /tmp/ags_python_compat/sitecustomize.py /usr/local/lib/python3.12/dist-packages/sitecustomize.py 2>/dev/null || true
cp /tmp/ags_python_compat/sitecustomize.py /usr/lib/python3/dist-packages/sitecustomize.py 2>/dev/null || true
cp /tmp/ags_python_compat/sitecustomize.py /usr/lib/python3.12/sitecustomize.py 2>/dev/null || true
cat > /etc/sudoers.d/ags-pythonpath <<'SUDO'
Defaults env_keep += "PYTHONPATH DISPLAY XDG_RUNTIME_DIR"
Defaults env_keep += "NPM_CONFIG_CACHE"
SUDO
chmod 440 /etc/sudoers.d/ags-pythonpath
cat > /etc/pip.conf <<'PIP'
[global]
break-system-packages = true
constraint = /etc/pip-constraints.txt
PIP
cat > /etc/pip-constraints.txt <<'PIP'
pandas<3
numpy<2
PIP
mkdir -p /tmp/ags-npm-cache
chmod 777 /tmp/ags-npm-cache
if [ -x /usr/bin/npm ]; then
  cat > /usr/local/bin/npm <<'SH'
#!/bin/sh
uid="$(id -u)"
cache="/tmp/ags-npm-cache-${uid}"
mkdir -p "$cache" 2>/dev/null || true
chmod 777 "$cache" 2>/dev/null || true
unset NPM_CONFIG_CACHE
export npm_config_cache="$cache"
exec /usr/bin/npm "$@"
SH
  chmod +x /usr/local/bin/npm
fi
if [ -x /usr/bin/libreoffice ]; then
  cat > /usr/local/bin/libreoffice <<'SH'
#!/bin/sh
uid="$(id -u)"
profile="/tmp/ags-libreoffice-profile-${uid}"
mkdir -p "$profile" 2>/dev/null || true
chmod 700 "$profile" 2>/dev/null || true
export SAL_USE_VCLPLUGIN="${SAL_USE_VCLPLUGIN:-gen}"
export HOME="${HOME:-/home/ga}"
cd "$HOME" 2>/dev/null || true
exec /usr/bin/libreoffice "-env:UserInstallation=file://${profile}" --norestore "$@"
SH
  chmod +x /usr/local/bin/libreoffice
fi
if [ -x /usr/bin/soffice ]; then
  cat > /usr/local/bin/soffice <<'SH'
#!/bin/sh
uid="$(id -u)"
profile="/tmp/ags-libreoffice-profile-${uid}"
mkdir -p "$profile" 2>/dev/null || true
chmod 700 "$profile" 2>/dev/null || true
export SAL_USE_VCLPLUGIN="${SAL_USE_VCLPLUGIN:-gen}"
export HOME="${HOME:-/home/ga}"
cd "$HOME" 2>/dev/null || true
exec /usr/bin/soffice "-env:UserInstallation=file://${profile}" --norestore "$@"
SH
  chmod +x /usr/local/bin/soffice
fi
chown -R ga:ga /home/ga/.cache /home/ga/.npm /home/ga/.local 2>/dev/null || true
if [ -f /workspace/scripts/task_utils.sh ] && grep -q 'wait_for_thunderbird_window' /workspace/scripts/task_utils.sh; then
  if ! grep -q 'wait_for_thunderbird_ready()' /workspace/scripts/task_utils.sh; then
    chmod u+w /workspace/scripts/task_utils.sh || true
    cat >> /workspace/scripts/task_utils.sh <<'SH'

wait_for_thunderbird_ready() {
    wait_for_thunderbird_window "$@"
}

get_thunderbird_window_id() {
    get_thunderbird_windows | awk '{print $1; exit}'
}

focus_window() {
    local window_id="$1"
    DISPLAY=:1 wmctrl -ia "$window_id" 2>/dev/null || DISPLAY=:1 wmctrl -a "$window_id" 2>/dev/null || true
}
SH
  fi
fi
if [ -f /workspace/scripts/task_utils.sh ] && grep -q 'wait_for_vscode()' /workspace/scripts/task_utils.sh; then
  if ! grep -q 'AGS compatibility wait_for_vscode' /workspace/scripts/task_utils.sh; then
    chmod u+w /workspace/scripts/task_utils.sh || true
    cat >> /workspace/scripts/task_utils.sh <<'SH'

# AGS compatibility wait_for_vscode
wait_for_vscode() {
    local timeout=${1:-20}
    local elapsed=0
    echo "Waiting for VSCode window..."
    while [ $elapsed -lt $timeout ]; do
        if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'Visual Studio Code\|Code'; then
            echo "VSCode window found after ${elapsed}s"
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    echo "Timeout: VSCode window not found after ${timeout}s"
    return 1
}
export -f wait_for_vscode

wait_for_window() {
    local window_pattern="$1"
    local timeout=${2:-30}
    local elapsed=0
    echo "Waiting for window matching '$window_pattern'..."
    while [ $elapsed -lt $timeout ]; do
        if [ "$window_pattern" = "Visual Studio Code" ]; then
            DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'Visual Studio Code\|Code' && return 0
        elif DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi "$window_pattern"; then
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    echo "Timeout: Window not found after ${timeout}s"
    echo "WARNING: continuing without detected window"
    return 0
}
export -f wait_for_window
SH
  fi
fi
if [ -f /workspace/scripts/task_utils.sh ] && ! grep -q 'take_screenshot()' /workspace/scripts/task_utils.sh; then
  chmod u+w /workspace/scripts/task_utils.sh || true
  cat >> /workspace/scripts/task_utils.sh <<'SH'

take_screenshot() {
    local output_file="${1:-/tmp/screenshot.png}"
    DISPLAY=:1 scrot "$output_file" 2>/dev/null || \
    DISPLAY=:1 import -window root "$output_file" 2>/dev/null || \
    echo "Warning: Could not take screenshot"
    [ -f "$output_file" ] && echo "Screenshot saved: $output_file"
}
export -f take_screenshot
SH
fi
if [ -f /workspace/scripts/task_utils.sh ] && ! grep -q 'AGS compatibility safe_xdotool' /workspace/scripts/task_utils.sh; then
  chmod u+w /workspace/scripts/task_utils.sh || true
  cat >> /workspace/scripts/task_utils.sh <<'SH'

# AGS compatibility safe_xdotool
safe_xdotool() {
    local user="$1"
    local display="$2"
    shift 2
    su - "$user" -c "DISPLAY=$display xdotool $*" >/tmp/ags_safe_xdotool.log 2>&1 || true
}
export -f safe_xdotool
SH
fi
if [ -f /workspace/scripts/task_utils.sh ] && ! grep -q 'AGS compatibility generic wait_for_window' /workspace/scripts/task_utils.sh; then
  chmod u+w /workspace/scripts/task_utils.sh || true
  cat >> /workspace/scripts/task_utils.sh <<'SH'

# AGS compatibility generic wait_for_window
wait_for_window() {
    local window_pattern="$1"
    local timeout=${2:-30}
    local elapsed=0
    echo "Waiting for window matching '$window_pattern'..."
    while [ $elapsed -lt $timeout ]; do
        if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi "$window_pattern"; then
            echo "Window found after ${elapsed}s"
            return 0
        fi
        if echo "$window_pattern" | grep -qi 'Visual Studio Code\|Code'; then
            if DISPLAY=:1 wmctrl -l 2>/dev/null | grep -qi 'Visual Studio Code\|Code' || pgrep -u ga -f 'code' >/dev/null 2>&1; then
                echo "VSCode process/window found after ${elapsed}s"
                return 0
            fi
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    echo "Timeout: Window not found after ${timeout}s"
    echo "WARNING: continuing without detected window"
    return 0
}
export -f wait_for_window
SH
fi
""",
            user="root",
            timeout=60,
        )

    def _upload_mount(self, mount: MountSpec) -> None:
        source = self._resolve_mount_source(mount.source)
        target = mount.target
        if source is None:
            raise FileNotFoundError(f"Unable to resolve AGS mount source: {mount.source}")

        if self._task_root and target.rstrip("/") == "/workspace/tasks":
            remote_task = f"{target.rstrip('/')}/{self._task_root.name}"
            self._upload_directory_contents(self._task_root, remote_task)
        elif source.is_dir():
            self._upload_directory_contents(source, target)
        else:
            self.copy_to(str(source), target)

        if mount.mode == "ro":
            self.exec(f"chmod -R a-w {shlex.quote(target)} || true", user="root", timeout=60)

    def _resolve_mount_source(self, source: str) -> Optional[Path]:
        raw = Path(source).expanduser()
        candidates = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            if self._env_root:
                candidates.append(self._env_root / raw)
                for parent in self._env_root.parents:
                    candidates.append(parent / raw)
            candidates.append(Path.cwd() / raw)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.exists():
                return resolved
        return None

    def _upload_directory_contents(self, src: Path, remote_dir: str) -> None:
        remote_dir = remote_dir.rstrip("/") or "/"
        self.exec(f"mkdir -p {shlex.quote(remote_dir)}", user="root", timeout=60)
        with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
            with tarfile.open(fileobj=handle, mode="w") as tar:
                for child in self._iter_children(src):
                    tar.add(child, arcname=str(child.relative_to(src)), recursive=True)
            handle.flush()
            handle.seek(0)
            remote_tar = f"/tmp/ga_mount_{uuid.uuid4().hex}.tar"
            self._sandbox().files.write(remote_tar, handle.read(), user=self._exec_user(None), request_timeout=300)
        self.exec(f"tar -xf {shlex.quote(remote_tar)} -C {shlex.quote(remote_dir)} && rm -f {shlex.quote(remote_tar)}", user="root", timeout=300)

    def _download_directory(self, remote_dir: str, host_dir: Path) -> None:
        remote_tar = f"/tmp/ga_copy_{uuid.uuid4().hex}.tar"
        self.exec(
            f"tar -cf {shlex.quote(remote_tar)} -C {shlex.quote(remote_dir)} .",
            user="root",
            timeout=300,
        )
        data = self._sandbox().files.read(remote_tar, format="bytes", user=self._exec_user(None), request_timeout=300)
        try:
            self._sandbox().files.remove(remote_tar, user=self._exec_user(None))
        except Exception:
            pass
        with tarfile.open(fileobj=io.BytesIO(bytes(data)), mode="r") as tar:
            tar.extractall(host_dir)

    def _remote_is_dir(self, path: str) -> bool:
        result = self._run_command(f"[ -d {shlex.quote(path)} ]", user="root", timeout=30)
        return result.exit_code == 0

    def _iter_children(self, src: Path) -> Iterable[Path]:
        for child in sorted(src.iterdir(), key=lambda path: path.name):
            if child.name == "__pycache__":
                continue
            yield child

    def _normalize_key_name(self, key: str) -> str:
        mapping = {
            "ctrl": "ctrl",
            "control": "ctrl",
            "cmd": "super",
            "command": "super",
            "option": "alt",
            "return": "Return",
            "enter": "Return",
            "esc": "Escape",
            "escape": "Escape",
            "space": "space",
            "backspace": "BackSpace",
            "delete": "Delete",
            "tab": "Tab",
        }
        return mapping.get(key.lower(), key)
