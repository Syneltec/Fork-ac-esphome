from __future__ import annotations

import asyncio
import base64
import datetime
import functools
import gzip
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import tornado
import tornado.concurrent
import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.iostream
import tornado.netutil
import tornado.process
import tornado.queues
import tornado.web
import tornado.websocket
import yaml
from tornado.log import access_log

from esphome import const, platformio_api, yaml_util
from esphome.helpers import get_bool_env, mkdir_p
from esphome.storage_json import StorageJSON, ext_storage_path, trash_storage_path
from esphome.util import get_serial_ports, shlex_quote
from esphome.yaml_util import FastestAvailableSafeLoader

from .core import DASHBOARD
from .entries import EntryState, entry_state_to_bool
from .util.subprocess import async_run_system_command
from .util.text import friendly_name_slugify

_LOGGER = logging.getLogger(__name__)

ENV_DEV = "ESPHOME_DASHBOARD_DEV"


cookie_authenticated_yes = b"yes"


settings = DASHBOARD.settings


def template_args():
    version = const.__version__
    if "b" in version:
        docs_link = "https://beta.esphome.io/"
    elif "dev" in version:
        docs_link = "https://next.esphome.io/"
    else:
        docs_link = "https://www.esphome.io/"

    return {
        "version": version,
        "docs_link": docs_link,
        "get_static_file_url": get_static_file_url,
        "relative_url": settings.relative_url,
        "streamer_mode": settings.streamer_mode,
        "config_dir": settings.config_dir,
    }


def authenticated(func):
    @functools.wraps(func)
    def decorator(self, *args, **kwargs):
        if not is_authenticated(self):
            self.redirect("./login")
            return None
        return func(self, *args, **kwargs)

    return decorator


def is_authenticated(request_handler):
    if settings.on_ha_addon:
        # Handle ingress - disable auth on ingress port
        # X-HA-Ingress is automatically stripped on the non-ingress server in nginx
        header = request_handler.request.headers.get("X-HA-Ingress", "NO")
        if str(header) == "YES":
            return True
    if settings.using_auth:
        return (
            request_handler.get_secure_cookie("authenticated")
            == cookie_authenticated_yes
        )
    return True


def bind_config(func):
    def decorator(self, *args, **kwargs):
        configuration = self.get_argument("configuration")
        kwargs = kwargs.copy()
        kwargs["configuration"] = configuration
        return func(self, *args, **kwargs)

    return decorator


# pylint: disable=abstract-method
class BaseHandler(tornado.web.RequestHandler):
    pass


def websocket_class(cls):
    # pylint: disable=protected-access
    if not hasattr(cls, "_message_handlers"):
        cls._message_handlers = {}

    for _, method in cls.__dict__.items():
        if hasattr(method, "_message_handler"):
            cls._message_handlers[method._message_handler] = method

    return cls


def websocket_method(name):
    def wrap(fn):
        # pylint: disable=protected-access
        fn._message_handler = name
        return fn

    return wrap


@websocket_class
class EsphomeCommandWebSocket(tornado.websocket.WebSocketHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._proc = None
        self._queue = None
        self._is_closed = False
        # Windows doesn't support non-blocking pipes,
        # use Popen() with a reading thread instead
        self._use_popen = os.name == "nt"

    @authenticated
    async def on_message(  # pylint: disable=invalid-overridden-method
        self, message: str
    ) -> None:
        # Since tornado 4.5, on_message is allowed to be a coroutine
        # Messages are always JSON, 500 when not
        json_message = json.loads(message)
        type_ = json_message["type"]
        # pylint: disable=no-member
        handlers = type(self)._message_handlers
        if type_ not in handlers:
            _LOGGER.warning("Requested unknown message type %s", type_)
            return

        await handlers[type_](self, json_message)

    @websocket_method("spawn")
    async def handle_spawn(self, json_message: dict[str, Any]) -> None:
        if self._proc is not None:
            # spawn can only be called once
            return
        command = await self.build_command(json_message)
        _LOGGER.info("Running command '%s'", " ".join(shlex_quote(x) for x in command))

        if self._use_popen:
            self._queue = tornado.queues.Queue()
            # pylint: disable=consider-using-with
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout_thread = threading.Thread(target=self._stdout_thread)
            stdout_thread.daemon = True
            stdout_thread.start()
        else:
            self._proc = tornado.process.Subprocess(
                command,
                stdout=tornado.process.Subprocess.STREAM,
                stderr=subprocess.STDOUT,
                stdin=tornado.process.Subprocess.STREAM,
                close_fds=False,
            )
            self._proc.set_exit_callback(self._proc_on_exit)

        tornado.ioloop.IOLoop.current().spawn_callback(self._redirect_stdout)

    @property
    def is_process_active(self):
        return self._proc is not None and self._proc.returncode is None

    @websocket_method("stdin")
    async def handle_stdin(self, json_message: dict[str, Any]) -> None:
        if not self.is_process_active:
            return
        text: str = json_message["data"]
        data = text.encode("utf-8", "replace")
        _LOGGER.debug("< stdin: %s", data)
        self._proc.stdin.write(data)

    @tornado.gen.coroutine
    def _redirect_stdout(self) -> None:
        reg = b"[\n\r]"

        while True:
            try:
                if self._use_popen:
                    data: bytes = yield self._queue.get()
                    if data is None:
                        self._proc_on_exit(self._proc.poll())
                        break
                else:
                    data: bytes = yield self._proc.stdout.read_until_regex(reg)
            except tornado.iostream.StreamClosedError:
                break

            text = data.decode("utf-8", "replace")
            _LOGGER.debug("> stdout: %s", text)
            self.write_message({"event": "line", "data": text})

    def _stdout_thread(self) -> None:
        if not self._use_popen:
            return
        while True:
            data = self._proc.stdout.readline()
            if data:
                data = data.replace(b"\r", b"")
                self._queue.put_nowait(data)
            if self._proc.poll() is not None:
                break
        self._proc.wait(1.0)
        self._queue.put_nowait(None)

    def _proc_on_exit(self, returncode: int) -> None:
        if not self._is_closed:
            # Check if the proc was not forcibly closed
            _LOGGER.info("Process exited with return code %s", returncode)
            self.write_message({"event": "exit", "code": returncode})

    def on_close(self) -> None:
        # Check if proc exists (if 'start' has been run)
        if self.is_process_active:
            _LOGGER.debug("Terminating process")
            if self._use_popen:
                self._proc.terminate()
            else:
                self._proc.proc.terminate()
        # Shutdown proc on WS close
        self._is_closed = True

    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        raise NotImplementedError


DASHBOARD_COMMAND = ["esphome", "--dashboard"]


class EsphomePortCommandWebSocket(EsphomeCommandWebSocket):
    """Base class for commands that require a port."""

    async def build_device_command(
        self, args: list[str], json_message: dict[str, Any]
    ) -> list[str]:
        """Build the command to run."""
        dashboard = DASHBOARD
        configuration = json_message["configuration"]
        config_file = settings.rel_path(configuration)
        port = json_message["port"]
        if (
            port == "OTA"
            and (mdns := dashboard.mdns_status)
            and (host_name := mdns.get_path_to_host_name(config_file))
            and (address := await mdns.async_resolve_host(host_name))
        ):
            port = address

        return [
            *DASHBOARD_COMMAND,
            *args,
            config_file,
            "--device",
            port,
        ]


class EsphomeLogsHandler(EsphomePortCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        """Build the command to run."""
        return await self.build_device_command(["logs"], json_message)


class EsphomeRenameHandler(EsphomeCommandWebSocket):
    old_name: str

    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        config_file = settings.rel_path(json_message["configuration"])
        self.old_name = json_message["configuration"]
        return [
            *DASHBOARD_COMMAND,
            "rename",
            config_file,
            json_message["newName"],
        ]

    def _proc_on_exit(self, returncode):
        super()._proc_on_exit(returncode)

        if returncode != 0:
            return

        # Remove the old ping result from the cache
        entries = DASHBOARD.entries
        if entry := entries.get(self.old_name):
            entries.async_set_state(entry, EntryState.UNKNOWN)


class EsphomeUploadHandler(EsphomePortCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        """Build the command to run."""
        return await self.build_device_command(["upload"], json_message)


class EsphomeRunHandler(EsphomePortCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        """Build the command to run."""
        return await self.build_device_command(["run"], json_message)


class EsphomeCompileHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        config_file = settings.rel_path(json_message["configuration"])
        command = [*DASHBOARD_COMMAND, "compile"]
        if json_message.get("only_generate", False):
            command.append("--only-generate")
        command.append(config_file)
        return command


class EsphomeValidateHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        config_file = settings.rel_path(json_message["configuration"])
        command = [*DASHBOARD_COMMAND, "config", config_file]
        if not settings.streamer_mode:
            command.append("--show-secrets")
        return command


class EsphomeCleanMqttHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        config_file = settings.rel_path(json_message["configuration"])
        return [*DASHBOARD_COMMAND, "clean-mqtt", config_file]


class EsphomeCleanHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        config_file = settings.rel_path(json_message["configuration"])
        return [*DASHBOARD_COMMAND, "clean", config_file]


class EsphomeVscodeHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        return [*DASHBOARD_COMMAND, "-q", "vscode", "dummy"]


class EsphomeAceEditorHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        return [*DASHBOARD_COMMAND, "-q", "vscode", "--ace", settings.config_dir]


class EsphomeUpdateAllHandler(EsphomeCommandWebSocket):
    async def build_command(self, json_message: dict[str, Any]) -> list[str]:
        return [*DASHBOARD_COMMAND, "update-all", settings.config_dir]


class SerialPortRequestHandler(BaseHandler):
    @authenticated
    async def get(self):
        ports = await asyncio.get_running_loop().run_in_executor(None, get_serial_ports)
        data = []
        for port in ports:
            desc = port.description
            if port.path == "/dev/ttyAMA0":
                desc = "UART pins on GPIO header"
            split_desc = desc.split(" - ")
            if len(split_desc) == 2 and split_desc[0] == split_desc[1]:
                # Some serial ports repeat their values
                desc = split_desc[0]
            data.append({"port": port.path, "desc": desc})
        data.append({"port": "OTA", "desc": "Over-The-Air"})
        data.sort(key=lambda x: x["port"], reverse=True)
        self.set_header("content-type", "application/json")
        self.write(json.dumps(data))


class WizardRequestHandler(BaseHandler):
    @authenticated
    def post(self):
        from esphome import wizard

        kwargs = {
            k: v
            for k, v in json.loads(self.request.body.decode()).items()
            if k in ("name", "platform", "board", "ssid", "psk", "password")
        }
        if not kwargs["name"]:
            self.set_status(422)
            self.set_header("content-type", "application/json")
            self.write(json.dumps({"error": "Name is required"}))
            return

        kwargs["friendly_name"] = kwargs["name"]
        kwargs["name"] = friendly_name_slugify(kwargs["friendly_name"])

        kwargs["ota_password"] = secrets.token_hex(16)
        noise_psk = secrets.token_bytes(32)
        kwargs["api_encryption_key"] = base64.b64encode(noise_psk).decode()
        filename = f"{kwargs['name']}.yaml"
        destination = settings.rel_path(filename)
        wizard.wizard_write(path=destination, **kwargs)
        self.set_status(200)
        self.set_header("content-type", "application/json")
        self.write(json.dumps({"configuration": filename}))
        self.finish()


class ImportRequestHandler(BaseHandler):
    @authenticated
    def post(self):
        from esphome.components.dashboard_import import import_config

        dashboard = DASHBOARD
        args = json.loads(self.request.body.decode())
        try:
            name = args["name"]
            friendly_name = args.get("friendly_name")
            encryption = args.get("encryption", False)

            imported_device = next(
                (
                    res
                    for res in dashboard.import_result.values()
                    if res.device_name == name
                ),
                None,
            )

            if imported_device is not None:
                network = imported_device.network
                if friendly_name is None:
                    friendly_name = imported_device.friendly_name
            else:
                network = const.CONF_WIFI

            import_config(
                settings.rel_path(f"{name}.yaml"),
                name,
                friendly_name,
                args["project_name"],
                args["package_import_url"],
                network,
                encryption,
            )
            # Make sure the device gets marked online right away
            dashboard.ping_request.set()
        except FileExistsError:
            self.set_status(500)
            self.write("File already exists")
            return
        except ValueError:
            self.set_status(422)
            self.write("Invalid package url")
            return

        self.set_status(200)
        self.set_header("content-type", "application/json")
        self.write(json.dumps({"configuration": f"{name}.yaml"}))
        self.finish()


class DownloadListRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    def get(self, configuration=None):
        storage_path = ext_storage_path(configuration)
        storage_json = StorageJSON.load(storage_path)
        if storage_json is None:
            self.send_error(404)
            return

        from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
        from esphome.components.esp32 import get_download_types as esp32_types
        from esphome.components.esp8266 import get_download_types as esp8266_types
        from esphome.components.libretiny import get_download_types as libretiny_types
        from esphome.components.rp2040 import get_download_types as rp2040_types

        downloads = []
        platform = storage_json.target_platform.lower()
        if platform == const.PLATFORM_RP2040:
            downloads = rp2040_types(storage_json)
        elif platform == const.PLATFORM_ESP8266:
            downloads = esp8266_types(storage_json)
        elif platform.upper() in ESP32_VARIANTS:
            downloads = esp32_types(storage_json)
        elif platform == const.PLATFORM_BK72XX:
            downloads = libretiny_types(storage_json)
        elif platform == const.PLATFORM_RTL87XX:
            downloads = libretiny_types(storage_json)
        else:
            self.send_error(418)
            return

        self.set_status(200)
        self.set_header("content-type", "application/json")
        self.write(json.dumps(downloads))
        self.finish()
        return


class DownloadBinaryRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    async def get(self, configuration=None):
        compressed = self.get_argument("compressed", "0") == "1"

        storage_path = ext_storage_path(configuration)
        storage_json = StorageJSON.load(storage_path)
        if storage_json is None:
            self.send_error(404)
            return

        # fallback to type=, but prioritize file=
        file_name = self.get_argument("type", None)
        file_name = self.get_argument("file", file_name)
        if file_name is None:
            self.send_error(400)
            return
        file_name = file_name.replace("..", "").lstrip("/")
        # get requested download name, or build it based on filename
        download_name = self.get_argument(
            "download",
            f"{storage_json.name}-{file_name}",
        )
        path = os.path.dirname(storage_json.firmware_bin_path)
        path = os.path.join(path, file_name)

        if not Path(path).is_file():
            args = ["esphome", "idedata", settings.rel_path(configuration)]
            rc, stdout, _ = await async_run_system_command(args)

            if rc != 0:
                self.send_error(404 if rc == 2 else 500)
                return

            idedata = platformio_api.IDEData(json.loads(stdout))

            found = False
            for image in idedata.extra_flash_images:
                if image.path.endswith(file_name):
                    path = image.path
                    download_name = file_name
                    found = True
                    break

            if not found:
                self.send_error(404)
                return

        download_name = download_name + ".gz" if compressed else download_name

        self.set_header("Content-Type", "application/octet-stream")
        self.set_header(
            "Content-Disposition", f'attachment; filename="{download_name}"'
        )
        self.set_header("Cache-Control", "no-cache")
        if not Path(path).is_file():
            self.send_error(404)
            return

        with open(path, "rb") as f:
            data = f.read()
            if compressed:
                data = gzip.compress(data, 9)
            self.write(data)

        self.finish()


class EsphomeVersionHandler(BaseHandler):
    @authenticated
    def get(self):
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"version": const.__version__}))
        self.finish()


class ListDevicesHandler(BaseHandler):
    @authenticated
    async def get(self):
        dashboard = DASHBOARD
        await dashboard.entries.async_request_update_entries()
        entries = dashboard.entries.async_all()
        self.set_header("content-type", "application/json")
        configured = {entry.name for entry in entries}

        self.write(
            json.dumps(
                {
                    "configured": [entry.to_dict() for entry in entries],
                    "importable": [
                        {
                            "name": res.device_name,
                            "friendly_name": res.friendly_name,
                            "package_import_url": res.package_import_url,
                            "project_name": res.project_name,
                            "project_version": res.project_version,
                            "network": res.network,
                        }
                        for res in dashboard.import_result.values()
                        if res.device_name not in configured
                    ],
                }
            )
        )


class MainRequestHandler(BaseHandler):
    @authenticated
    def get(self):
        begin = bool(self.get_argument("begin", False))

        self.render(
            "index.template.html",
            begin=begin,
            **template_args(),
            login_enabled=settings.using_password,
        )


class PrometheusServiceDiscoveryHandler(BaseHandler):
    @authenticated
    async def get(self):
        dashboard = DASHBOARD
        await dashboard.entries.async_request_update_entries()
        entries = dashboard.entries.async_all()
        self.set_header("content-type", "application/json")
        sd = []
        for entry in entries:
            if entry.web_port is None:
                continue
            labels = {
                "__meta_name": entry.name,
                "__meta_esp_platform": entry.target_platform,
                "__meta_esphome_version": entry.storage.esphome_version,
            }
            for integration in entry.storage.loaded_integrations:
                labels[f"__meta_integration_{integration}"] = "true"
            sd.append(
                {
                    "targets": [
                        f"{entry.address}:{entry.web_port}",
                    ],
                    "labels": labels,
                }
            )
        self.write(json.dumps(sd))


class BoardsRequestHandler(BaseHandler):
    @authenticated
    def get(self, platform: str):
        from esphome.components.bk72xx.boards import BOARDS as BK72XX_BOARDS
        from esphome.components.esp32.boards import BOARDS as ESP32_BOARDS
        from esphome.components.esp8266.boards import BOARDS as ESP8266_BOARDS
        from esphome.components.rp2040.boards import BOARDS as RP2040_BOARDS
        from esphome.components.rtl87xx.boards import BOARDS as RTL87XX_BOARDS

        platform_to_boards = {
            const.PLATFORM_ESP32: ESP32_BOARDS,
            const.PLATFORM_ESP8266: ESP8266_BOARDS,
            const.PLATFORM_RP2040: RP2040_BOARDS,
            const.PLATFORM_BK72XX: BK72XX_BOARDS,
            const.PLATFORM_RTL87XX: RTL87XX_BOARDS,
        }
        # filter all ESP32 variants by requested platform
        if platform.startswith("esp32"):
            boards = {
                k: v
                for k, v in platform_to_boards[const.PLATFORM_ESP32].items()
                if v[const.KEY_VARIANT] == platform.upper()
            }
        else:
            boards = platform_to_boards[platform]

        # map to a {board_name: board_title} dict
        platform_boards = {key: val[const.KEY_NAME] for key, val in boards.items()}
        # sort by board title
        boards_items = sorted(platform_boards.items(), key=lambda item: item[1])
        output = [{"items": dict(boards_items)}]

        self.set_header("content-type", "application/json")
        self.write(json.dumps(output))


class PingRequestHandler(BaseHandler):
    @authenticated
    def get(self):
        dashboard = DASHBOARD
        dashboard.ping_request.set()
        if settings.status_use_mqtt:
            dashboard.mqtt_ping_request.set()
        self.set_header("content-type", "application/json")

        self.write(
            json.dumps(
                {
                    entry.filename: entry_state_to_bool(entry.state)
                    for entry in dashboard.entries.async_all()
                }
            )
        )


class InfoRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    async def get(self, configuration=None):
        yaml_path = settings.rel_path(configuration)
        dashboard = DASHBOARD
        entry = dashboard.entries.get(yaml_path)

        if not entry:
            self.set_status(404)
            return

        self.set_header("content-type", "application/json")
        self.write(entry.storage.to_json())


class EditRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    def get(self, configuration=None):
        filename = settings.rel_path(configuration)
        content = ""
        if os.path.isfile(filename):
            with open(file=filename, encoding="utf-8") as f:
                content = f.read()
        self.write(content)

    @authenticated
    @bind_config
    def post(self, configuration=None):
        with open(file=settings.rel_path(configuration), mode="wb") as f:
            f.write(self.request.body)
        self.set_status(200)


class DeleteRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    def post(self, configuration=None):
        config_file = settings.rel_path(configuration)
        storage_path = ext_storage_path(configuration)

        trash_path = trash_storage_path()
        mkdir_p(trash_path)
        shutil.move(config_file, os.path.join(trash_path, configuration))

        storage_json = StorageJSON.load(storage_path)
        if storage_json is not None:
            # Delete build folder (if exists)
            name = storage_json.name
            build_folder = os.path.join(settings.config_dir, name)
            if build_folder is not None:
                shutil.rmtree(build_folder, os.path.join(trash_path, name))


class UndoDeleteRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    def post(self, configuration=None):
        config_file = settings.rel_path(configuration)
        trash_path = trash_storage_path()
        shutil.move(os.path.join(trash_path, configuration), config_file)


class LoginHandler(BaseHandler):
    def get(self):
        if is_authenticated(self):
            self.redirect("./")
        else:
            self.render_login_page()

    def render_login_page(self, error=None):
        self.render(
            "login.template.html",
            error=error,
            ha_addon=settings.using_ha_addon_auth,
            has_username=bool(settings.username),
            **template_args(),
        )

    def post_ha_addon_login(self):
        import requests

        headers = {
            "X-Supervisor-Token": os.getenv("SUPERVISOR_TOKEN"),
        }

        data = {
            "username": self.get_argument("username", ""),
            "password": self.get_argument("password", ""),
        }
        try:
            req = requests.post(
                "http://supervisor/auth", headers=headers, json=data, timeout=30
            )
            if req.status_code == 200:
                self.set_secure_cookie("authenticated", cookie_authenticated_yes)
                self.redirect("/")
                return
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Error during Hass.io auth request: %s", err)
            self.set_status(500)
            self.render_login_page(error="Internal server error")
            return
        self.set_status(401)
        self.render_login_page(error="Invalid username or password")

    def post_native_login(self):
        username = self.get_argument("username", "")
        password = self.get_argument("password", "")
        if settings.check_password(username, password):
            self.set_secure_cookie("authenticated", cookie_authenticated_yes)
            self.redirect("./")
            return
        error_str = (
            "Invalid username or password" if settings.username else "Invalid password"
        )
        self.set_status(401)
        self.render_login_page(error=error_str)

    def post(self):
        if settings.using_ha_addon_auth:
            self.post_ha_addon_login()
        else:
            self.post_native_login()


class LogoutHandler(BaseHandler):
    @authenticated
    def get(self):
        self.clear_cookie("authenticated")
        self.redirect("./login")


class SecretKeysRequestHandler(BaseHandler):
    @authenticated
    def get(self):
        filename = None

        for secret_filename in const.SECRETS_FILES:
            relative_filename = settings.rel_path(secret_filename)
            if os.path.isfile(relative_filename):
                filename = relative_filename
                break

        if filename is None:
            self.send_error(404)
            return

        secret_keys = list(yaml_util.load_yaml(filename, clear_secrets=False))

        self.set_header("content-type", "application/json")
        self.write(json.dumps(secret_keys))


class SafeLoaderIgnoreUnknown(FastestAvailableSafeLoader):
    def ignore_unknown(self, node):
        return f"{node.tag} {node.value}"

    def construct_yaml_binary(self, node) -> str:
        return super().construct_yaml_binary(node).decode("ascii")


SafeLoaderIgnoreUnknown.add_constructor(None, SafeLoaderIgnoreUnknown.ignore_unknown)
SafeLoaderIgnoreUnknown.add_constructor(
    "tag:yaml.org,2002:binary", SafeLoaderIgnoreUnknown.construct_yaml_binary
)


class JsonConfigRequestHandler(BaseHandler):
    @authenticated
    @bind_config
    async def get(self, configuration=None):
        filename = settings.rel_path(configuration)
        if not os.path.isfile(filename):
            self.send_error(404)
            return

        args = ["esphome", "config", filename, "--show-secrets"]

        rc, stdout, _ = await async_run_system_command(args)

        if rc != 0:
            self.send_error(422)
            return

        data = yaml.load(stdout, Loader=SafeLoaderIgnoreUnknown)
        self.set_header("content-type", "application/json")
        self.write(json.dumps(data))
        self.finish()


def get_base_frontend_path():
    if ENV_DEV not in os.environ:
        import esphome_dashboard

        return esphome_dashboard.where()

    static_path = os.environ[ENV_DEV]
    if not static_path.endswith("/"):
        static_path += "/"

    # This path can be relative, so resolve against the root or else templates don't work
    return os.path.abspath(os.path.join(os.getcwd(), static_path, "esphome_dashboard"))


def get_static_path(*args):
    return os.path.join(get_base_frontend_path(), "static", *args)


@functools.cache
def get_static_file_url(name):
    base = f"./static/{name}"

    if ENV_DEV in os.environ:
        return base

    # Module imports can't deduplicate if stuff added to url
    if name == "js/esphome/index.js":
        import esphome_dashboard

        return base.replace("index.js", esphome_dashboard.entrypoint())

    path = get_static_path(name)
    with open(path, "rb") as f_handle:
        hash_ = hashlib.md5(f_handle.read()).hexdigest()[:8]
    return f"{base}?hash={hash_}"


def make_app(debug=get_bool_env(ENV_DEV)) -> tornado.web.Application:
    def log_function(handler):
        if handler.get_status() < 400:
            log_method = access_log.info

            if isinstance(handler, SerialPortRequestHandler) and not debug:
                return
            if isinstance(handler, PingRequestHandler) and not debug:
                return
        elif handler.get_status() < 500:
            log_method = access_log.warning
        else:
            log_method = access_log.error

        request_time = 1000.0 * handler.request.request_time()
        # pylint: disable=protected-access
        log_method(
            "%d %s %.2fms",
            handler.get_status(),
            handler._request_summary(),
            request_time,
        )

    class StaticFileHandler(tornado.web.StaticFileHandler):
        def get_cache_time(
            self, path: str, modified: datetime.datetime | None, mime_type: str
        ) -> int:
            """Override to customize cache control behavior."""
            if debug:
                return 0
            # Assets that are hashed have ?hash= in the URL, all javascript
            # filenames hashed so we can cache them for a long time
            if "hash" in self.request.arguments or "/javascript" in mime_type:
                return self.CACHE_MAX_AGE
            return super().get_cache_time(path, modified, mime_type)

    app_settings = {
        "debug": debug,
        "cookie_secret": settings.cookie_secret,
        "log_function": log_function,
        "websocket_ping_interval": 30.0,
        "template_path": get_base_frontend_path(),
    }
    rel = settings.relative_url
    return tornado.web.Application(
        [
            (f"{rel}", MainRequestHandler),
            (f"{rel}login", LoginHandler),
            (f"{rel}logout", LogoutHandler),
            (f"{rel}logs", EsphomeLogsHandler),
            (f"{rel}upload", EsphomeUploadHandler),
            (f"{rel}run", EsphomeRunHandler),
            (f"{rel}compile", EsphomeCompileHandler),
            (f"{rel}validate", EsphomeValidateHandler),
            (f"{rel}clean-mqtt", EsphomeCleanMqttHandler),
            (f"{rel}clean", EsphomeCleanHandler),
            (f"{rel}vscode", EsphomeVscodeHandler),
            (f"{rel}ace", EsphomeAceEditorHandler),
            (f"{rel}update-all", EsphomeUpdateAllHandler),
            (f"{rel}info", InfoRequestHandler),
            (f"{rel}edit", EditRequestHandler),
            (f"{rel}downloads", DownloadListRequestHandler),
            (f"{rel}download.bin", DownloadBinaryRequestHandler),
            (f"{rel}serial-ports", SerialPortRequestHandler),
            (f"{rel}ping", PingRequestHandler),
            (f"{rel}delete", DeleteRequestHandler),
            (f"{rel}undo-delete", UndoDeleteRequestHandler),
            (f"{rel}wizard", WizardRequestHandler),
            (f"{rel}static/(.*)", StaticFileHandler, {"path": get_static_path()}),
            (f"{rel}devices", ListDevicesHandler),
            (f"{rel}import", ImportRequestHandler),
            (f"{rel}secret_keys", SecretKeysRequestHandler),
            (f"{rel}json-config", JsonConfigRequestHandler),
            (f"{rel}rename", EsphomeRenameHandler),
            (f"{rel}prometheus-sd", PrometheusServiceDiscoveryHandler),
            (f"{rel}boards/([a-z0-9]+)", BoardsRequestHandler),
            (f"{rel}version", EsphomeVersionHandler),
        ],
        **app_settings,
    )


def start_web_server(
    app: tornado.web.Application,
    socket: str | None,
    address: str | None,
    port: int | None,
    config_dir: str,
) -> None:
    """Start the web server listener."""
    if socket is None:
        _LOGGER.info(
            "Starting dashboard web server on http://%s:%s and configuration dir %s...",
            address,
            port,
            config_dir,
        )
        app.listen(port, address)
        return

    _LOGGER.info(
        "Starting dashboard web server on unix socket %s and configuration dir %s...",
        socket,
        config_dir,
    )
    server = tornado.httpserver.HTTPServer(app)
    socket = tornado.netutil.bind_unix_socket(socket, mode=0o666)
    server.add_socket(socket)
