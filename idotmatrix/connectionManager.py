from bleak import BleakClient, BleakScanner, AdvertisementData
from .const import UUID_READ_DATA, UUID_WRITE_DATA, BLUETOOTH_DEVICE_NAME
import asyncio
import logging
from typing import List, Optional


class SingletonMeta(type):
    logging = logging.getLogger(__name__)
    _instances: dict = {}

    def __call__(cls, *args, **kwargs) -> "SingletonMeta":
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class ConnectionManager(metaclass=SingletonMeta):
    logging = logging.getLogger(__name__)

    def __init__(self) -> None:
        self.address: Optional[str] = None
        self.client: Optional[BleakClient] = None
        # When True, every write waits for the device's acknowledgement.
        # Acknowledged writes cannot be dropped but cost roughly one BLE
        # connection interval each (~30ms measured on real hardware). With
        # the default fire-and-forget writes the device may silently drop
        # commands under sustained traffic, even when writes are paced.
        self.ack_writes: bool = False
        # Pause after unacknowledged sends so the device's command queue
        # keeps up. Ignored for acknowledged writes (they self-pace).
        self.send_delay: float = 0.01

    @staticmethod
    async def scan() -> List[str]:
        logging.info("scanning for iDotMatrix bluetooth devices...")
        devices = await BleakScanner.discover(return_adv=True)
        filtered_devices: List[str] = []
        for key, (device, adv) in devices.items():
            if (
                isinstance(adv, AdvertisementData)
                and adv.local_name
                and str(adv.local_name).startswith(BLUETOOTH_DEVICE_NAME)
            ):
                logging.info(f"found device {key} with name {adv.local_name}")
                filtered_devices.append(device.address)
        return filtered_devices

    async def connectByAddress(self, address: str) -> None:
        self.address = address
        await self.connect()

    async def connectBySearch(self) -> None:
        devices = await self.scan()
        if devices:
            # connect to first device
            self.address = devices[0]
            await self.connect()
        else:
            self.logging.error("no target devices found.")

    async def connect(self) -> None:
        if self.address:
            if not self.client:
                self.client = BleakClient(self.address)
            if not self.client.is_connected:
                await self.client.connect()
                self.logging.info(f"connected to {self.address}")
        else:
            self.logging.error("device address is not set.")

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            self.logging.info(f"disconnected from {self.address}")

    async def send(self, data, response=False, retries: int = 1, ack_last=False):
        """Send one command to the device.

        The device firmware executes only the FIRST command contained in a
        GATT write and silently discards anything after it (verified on real
        hardware). Never concatenate multiple commands into a single send();
        call send() once per command. Long single commands (e.g. PNG/GIF
        uploads) are fine: they are chunked to the MTU below and reassembled
        by the firmware using the command's declared length.

        Args:
            data: byte payload of exactly one command.
            response (bool): wait for the device to acknowledge each write.
                Slower (about one connection interval per write) but writes
                cannot be dropped. Also enabled globally via ack_writes.
            retries (int): reconnect and retry attempts on write failure.
            ack_last (bool): acknowledge only the final chunk. BLE writes are
                ordered, so this one round trip confirms the whole command
                arrived and throttles the sender to the device's pace without
                paying a round trip per chunk. Ideal for streaming frames:
                sustained throughput with a bounded device-side backlog.

        Returns:
            bool: True once sent, False if the connection could not be used.
        """
        response = response or self.ack_writes
        for attempt in range(retries + 1):
            try:
                if not (self.client and self.client.is_connected):
                    await self.connect()
                if not (self.client and self.client.is_connected):
                    return False
                self.logging.debug("sending message(s) to device")
                chunk_size = self.client.services.get_characteristic(
                    UUID_WRITE_DATA
                ).max_write_without_response_size
                for i in range(0, len(data), chunk_size):
                    is_last = i + chunk_size >= len(data)
                    await self.client.write_gatt_char(
                        UUID_WRITE_DATA,
                        data[i : i + chunk_size],
                        response=response or (ack_last and is_last),
                    )
                if not (response or ack_last) and self.send_delay > 0:
                    # non-blocking pacing; time.sleep here would stall the
                    # whole asyncio event loop for every command sent
                    await asyncio.sleep(self.send_delay)
                return True
            except Exception as error:
                if attempt >= retries:
                    self.logging.error(f"sending failed: {error}")
                    return False
                self.logging.warning(
                    f"sending failed ({error}), reconnecting and retrying"
                )
                try:
                    await self.disconnect()
                except Exception:
                    pass
        return False

    async def read(self) -> bytes:
        if self.client and self.client.is_connected:
            data = await self.client.read_gatt_char(UUID_READ_DATA)
            self.logging.info("data received")
            return data
