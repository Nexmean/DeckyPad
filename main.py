from dataclasses import dataclass
import json
import os
import subprocess
import aiohttp
import configparser
import socket

from aiohttp import web

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code one directory up
# or add the `decky-loader/plugin` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky_plugin

VIRTUALHERE_SERVER_URL: str = "https://www.virtualhere.com/sites/default/files/usbserver/vhusbdx86_64"
VIRTUALHERE_SERVER_DIR: str = decky_plugin.DECKY_PLUGIN_RUNTIME_DIR + "/server"
VIRTUALHERE_SERVER_PATH: str = VIRTUALHERE_SERVER_DIR + "/vhusbdx86_64"
VIRTUALHERE_SERVER_CONFIG_PATH: str = VIRTUALHERE_SERVER_DIR + "/server/config.ini"

class System:
    BRIGHTNESS_FILE = "/sys/class/backlight/amdgpu_bl0/brightness"   
    __brightness_before: str | None = None

    def disable_sleep(self):
        subprocess.run(['systemctl', 'mask', 'sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target'])

    def enable_sleep(self):
        subprocess.run(['systemctl', 'unmask', 'sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target'])

    def set_minimum_brightness(self):
        with open(System.BRIGHTNESS_FILE, 'r') as f:
            self.__brightness_before = f.read()

        with open(System.BRIGHTNESS_FILE, 'w') as f:
            f.write("0")

    def restore_brightness(self):
        if self.__brightness_before is not None:
            with open(System.BRIGHTNESS_FILE, 'w') as f:
                f.write(self.__brightness_before)
            
            self.__brightness_before = None

class VirtualhereServerProcess:
    __process: subprocess.Popen | None
    __eventsHandlerPort: int | None = None

    @dataclass
    class Config:
        onBind: str
        onUnbind: str

    CONFIG = Config(
        onBind = decky_plugin.DECKY_PLUGIN_DIR + """/shell/onBind.sh "$VENDOR_ID$" "$PRODUCT_ID$" "$CLIENT_IP$" "$CONNECTION_ID$" """,
        onUnbind =  decky_plugin.DECKY_PLUGIN_DIR + """/shell/onUnbind.sh "$VENDOR_ID$" "$PRODUCT_ID$" "$CLIENT_IP$" "$CONNECTION_ID$" "$SURPRISE_UNBOUND$" """,
    )

    async def start(self, eventsHandlerPort: int):
        # write config to __configPath in ini format
        with open(VIRTUALHERE_SERVER_CONFIG_PATH, 'w') as f:
            config = configparser.ConfigParser()
            config['onBind'] = VirtualhereServerProcess.CONFIG.onBind
            config['onUnbind'] = VirtualhereServerProcess.CONFIG.onUnbind
            config.write(f)
        # run the server with events handler port in environment
        self.__process = subprocess.Popen([VIRTUALHERE_SERVER_PATH], env={'VIRTUALHERE_SERVER_EVENTS_HANDLER_PORT': str(eventsHandlerPort)}, stdout=subprocess.PIPE)
        self.__eventsHandlerPort = eventsHandlerPort

    async def stop(self, releaseEventsHandlerPort: bool = True):
        if self.__process is not None:
            self.__process.kill()
            self.__process = None
            if releaseEventsHandlerPort:
                self.__eventsHandlerPort = None

    async def restart(self):
        if self.__process is not None:
            await self.stop(False)
            await self.start(self.__eventsHandlerPort)

    def is_up(self) -> bool:
        return self.__process is not None

class VirtualhereServerEventsHandler:
    __app: aiohttp.web.Application
    __port: int | None = None
    __site: web.TCPSite | None = None

    def __init__(self, onBind, onUnbind):
        self.__onBindHandler = onBind
        self.__onUnbindHandler = onUnbind
        self.__app = self.__create_app()

    async def start(self) -> int:
        runner = web.AppRunner(self.__app)
        await runner.setup()
        port = self.__get_free_port()
        self.__port = port
        self.__site = web.TCPSite(runner, '127.0.0.1', self.__port)
        await self.__site.start()
        return port

    async def stop(self):
        if self.__site is not None:
            await self.__site.stop()
            self.__port = None
            self.__site = None

    def is_up(self) -> bool:
        return self.__site is not None

    def get_port(self) -> int | None:
        return self.__port

    async def __onBind(self, request: aiohttp.web.Request):
        json_parsed = await request.json()
        request_data = VirtualhereServer.OnBindRequest(
            vendor_id=json_parsed["vendor_id"],
            product_id=json_parsed["product_id"],
            client_ip=json_parsed["client_ip"],
            connection_id=json_parsed["connection_id"],
        )
        await self.__onBindHandler(request_data)
    
    async def __onUnbind(self, request: aiohttp.web.Request):
        json_parsed = await request.json()
        surprise_unbound = False
        if json_parsed["surprise_unbound"] == "1":
            surprise_unbound = True
        request_data = VirtualhereServer.OnBindRequest(
            vendor_id=json_parsed["vendor_id"],
            product_id=json_parsed["product_id"],
            client_ip=json_parsed["client_ip"],
            connection_id=json_parsed["connection_id"],
            surprise_unbound=surprise_unbound,
        )
        await self.__onUnbindHandler(request_data)

    def __create_app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        app.router.add_post("/onBind", self.__onBind)
        app.router.add_post("/onUnbind", self.__onUnbind)
        return app

    def __get_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            port = s.getsockname()[1]
        return port

class VirtualhereServer:
    @dataclass
    class OnBindRequest:
        vendor_id: str
        product_id: str
        client_ip: str
        connection_id: str

    @dataclass
    class OnUnbindRequest:
        vendor_id: str
        product_id: str
        client_ip: str
        connection_id: str
        surprise_unbound: bool

    __client_ip: str | None
    __system: System
    __process: VirtualhereServerProcess
    __eventsHandler: VirtualhereServerEventsHandler

    async def __onBind(self, request: OnBindRequest):
        self.__client_ip = request.client_ip
        self.__system.disable_sleep()
        self.__system.set_minimum_brightness()

    async def __onUnbind(self, _request: OnUnbindRequest):
        self.__system.enable_sleep()
        self.__system.restore_brightness()
        self.__client_ip = None
        self.__process.restart()

    def __init__(self):
        self.__system = System()
        self.__process = VirtualhereServerProcess()
        self.__eventsHandler = VirtualhereServerEventsHandler(self.__onBind, self.__onUnbind)

    async def start(self):
        eventsHandlerPort = await self.__eventsHandler.start()
        await self.__process.start(eventsHandlerPort)

    async def stop(self):
        self.__system.enable_sleep()
        self.__system.restore_brightness()
        await self.__process.stop()
        await self.__eventsHandler.stop()

    def is_up(self) -> bool:
        return self.__process.is_up() and self.__eventsHandler.is_up()

    def get_client_ip(self) -> str | None:
        return self.__client_ip

class Plugin:
    __virtualhere_server: VirtualhereServer

    async def get_deck_ip(self) -> str:
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
        return ip_address

    async def start_server(self) -> str:
        await self.__virtualhere_server.start()
        return await self.get_deck_ip()

    async def stop_server(self):
        await self.__virtualhere_server.stop()

    async def server_is_up(self) -> bool:
        return self.__virtualhere_server.is_up()

    async def server_get_client_ip(self) -> str | None:
        return self.__virtualhere_server.get_client_ip()

    async def __ensure_virtualhere_installed(self):
        if not self.__is_virtualhere_installed():
            decky_plugin.logger.info("Installing VirtualHere server")
            # Download and save the VirtualHere server
            if not os.path.exists(VIRTUALHERE_SERVER_DIR):
                os.mkdir(VIRTUALHERE_SERVER_DIR)
            async with aiohttp.ClientSession() as session:
                async with session.get(VIRTUALHERE_SERVER_URL) as response:
                    with open(VIRTUALHERE_SERVER_PATH, "wb") as f:
                        f.write(await response.read())
            # Make it executable
            os.chmod(VIRTUALHERE_SERVER_PATH, 0o755)
            decky_plugin.logger.info("Installed VirtualHere server")

    def __is_virtualhere_installed(self) -> bool:
        return os.path.exists(self.VIRTUALHERE_SERVER_PATH)

    # aiohttp server for handling virtualhere events

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        await self.__ensure_virtualhere_installed()
        self.__virtualhere_server = VirtualhereServer()

    # Function called first during the unload process, utilize this to handle your plugin being removed
    async def _unload(self):
        self.__virtualhere_server.stop()