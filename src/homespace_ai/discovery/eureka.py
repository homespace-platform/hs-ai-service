import asyncio
import socket
from urllib.parse import urlparse

import httpx
import structlog

from homespace_ai.core.config import Settings

logger = structlog.get_logger(__name__)


def resolve_advertised_host(configured_host: str) -> str:
    if configured_host not in {"localhost", "127.0.0.1"}:
        return configured_host

    # Ask Windows' routing table which local address it would use for outbound
    # traffic. UDP connect selects an interface and sends no packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass

    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for address in addresses:
            candidate = address[4][0]
            if not candidate.startswith(("127.", "169.254.")):
                return candidate
    except OSError:
        pass

    return configured_host


class EurekaRegistration:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.host = resolve_advertised_host(settings.eureka_instance_hostname)
        self.service_name = settings.app_name.upper()
        self.instance_id = f"{self.host}:{settings.app_name}:{settings.port}"
        eureka = urlparse(str(settings.eureka_client_service_url))
        self._eureka_root = f"{eureka.scheme}://{eureka.netloc}{eureka.path.rstrip('/')}"
        self._registration_url = f"{self._eureka_root}/apps/{self.service_name}"

    async def register(self) -> bool:
        payload = {
            "instance": {
                "instanceId": self.instance_id,
                "app": self.service_name,
                "vipAddress": self.service_name.lower(),
                "secureVipAddress": self.service_name.lower(),
                "hostName": self.host,
                "ipAddr": self.host,
                "status": "UP",
                "port": {"$": self._settings.port, "@enabled": True},
                "securePort": {"$": 443, "@enabled": False},
                "homePageUrl": f"http://{self.host}:{self._settings.port}/",
                "statusPageUrl": f"http://{self.host}:{self._settings.port}/ping",
                "healthCheckUrl": f"http://{self.host}:{self._settings.port}/ping",
                "dataCenterInfo": {
                    "@class": "com.netflix.appinfo.InstanceInfo$DefaultDataCenterInfo",
                    "name": "MyOwn",
                },
                "metadata": {"management.port": str(self._settings.port)},
            }
        }
        try:
            response = await self._client.post(
                self._registration_url,
                json=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            logger.info(
                "eureka_registered",
                service=self.service_name,
                instance_id=self.instance_id,
            )
            return True
        except httpx.HTTPError as error:
            logger.warning("eureka_registration_failed", error=str(error))
            return False

    async def heartbeat(self) -> bool:
        url = f"{self._registration_url}/{self.instance_id}"
        try:
            response = await self._client.put(url)
            if response.status_code == 404:
                return await self.register()
            response.raise_for_status()
            return True
        except httpx.HTTPError as error:
            logger.warning("eureka_heartbeat_failed", error=str(error))
            return False

    async def deregister(self) -> None:
        try:
            response = await self._client.delete(
                f"{self._registration_url}/{self.instance_id}"
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
            logger.info("eureka_deregistered", instance_id=self.instance_id)
        except httpx.HTTPError as error:
            logger.warning("eureka_deregistration_failed", error=str(error))

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.eureka_heartbeat_interval_seconds)
            await self.heartbeat()
