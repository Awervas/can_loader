import argparse
import random
import sys
from typing import List

import can
try:
    from can.interfaces.seeedstudio import SeeedBus
    from zlgcan.zlgcan import ZCanTxMode, ZCANDeviceType
except ImportError:
    pass
from isotp import WrongSequenceNumberError
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.client import Client
from udsoncan.common.MemoryLocation import MemoryLocation
from udsoncan.exceptions import NegativeResponseException, TimeoutException
import udsoncan.configs
import isotp
import time

UDS_ERASE_FLASH_ROUTINE_ID = 0xFF00
UDS_CRC_CHECK_ROUTINE_ID = 0xFF01
TX_ID = 0x0ADAF3F1
RX_ID = 0x0ADAF1F3

START_ROUTINE = 1
STOP_ROUTINE = 2
REQUEST_ROUTINE_RESULTS = 3


class Block:
    data: bytearray
    address: int

    def __init__(self, address: int):
        self.address = address
        self.data = bytearray()

    def append(self, data: bytearray):
        self.data += data

    def size(self) -> int:
        return len(self.data)


blocks: List[Block] = []


def append_data_to_block(address: int, data: bytearray):
    global blocks
    if not blocks:
        blocks.append(Block(0))
    if blocks[-1].size() + blocks[-1].address != address:
        blocks.append(Block(address))
    blocks[-1].append(data)


def main(args):
    my_block_size = args.block_size
    with open(args.path, "rb") as fd:
        address = 0
        while True:
            data = fd.read(my_block_size)
            if not data:
                break
            if not all(byte == 0xFF for byte in data):
                append_data_to_block(address, data)
            address += my_block_size

    isotp_params = {
        "stmin": 1,
        "blocksize": 8,
        "wftmax": 0,
        "tx_data_length": 8,
        "tx_data_min_length": None,
        "tx_padding": 0,
        "rx_flowcontrol_timeout": 10000,
        "rx_consecutive_frame_timeout": 15000,
        "override_receiver_stmin": None,
        "max_frame_size": 4095,
        "can_fd": False,
        "bitrate_switch": False,
        "rate_limit_enable": False,
        "rate_limit_max_bitrate": 1000000,
        "rate_limit_window_size": 0.2,
        "listen_mode": False,
    }

    uds_config = udsoncan.configs.default_client_config.copy()
    uds_config['p2_timeout'] = 2
    uds_config['request_timeout'] = 5
    #

    if args.port == 'can0':
        bus = can.interface.Bus(
            channel="can0",
            bustype="socketcan"
        )
    elif args.port == 'systec':
        bus = can.interface.Bus(
            bustype="systec",
            channel=0,
            bitrate=500000
        )
    elif args.port == 'pcan':
        bus = can.interface.Bus(
            bustype="pcan",
            channel="PCAN_USBBUS1",  # имя канала, см. ниже
            bitrate=500000  # нужная скорость (500k, 250k и т.д.)
        )
    elif args.port == 'zlgcan':

        bus = can.interface.Bus(
            bustype="zlgcan",
            libpath=sys.path[0]+"/library/",
            channel=0,
            device_type=ZCANDeviceType.ZCAN_USBCAN_E_U,
            configs=[{'bitrate': 500000, 'resistance': 1}]
        )

    elif 'COM' in args.port.upper():
        bus = SeeedBus(channel=args.port, frame_type="EXT", bitrate=500000, timeout=2)

    else:
        raise ValueError

    print('INIT BUS')
    time.sleep(0.55)
    notifier = can.Notifier(bus, [])
    tp_addr = isotp.Address(
        isotp.AddressingMode.Normal_29bits,
        txid=TX_ID if args.mode == 'firmware' else 0x8ADAF3F1,
        rxid=RX_ID if args.mode == 'firmware' else 0x8ADAF1F3,
    )
    stack = isotp.NotifierBasedCanStack(
        bus=bus, notifier=notifier, address=tp_addr, params=isotp_params
    )
    conn = PythonIsoTpConnection(stack)

    def write_block(block: Block):
        memory = MemoryLocation(
            0x08020000 + block.address,
            block.size(),
            address_format=32,
            memorysize_format=32,
        )
        for _ in range(3):
            try:
                response = client.request_download(memory)
                break
            except NegativeResponseException:
                continue
            except TimeoutException:
                continue
        else:
            raise TimeoutException

        block_size = int.from_bytes(response.get_payload()[2:], byteorder='big')
        block_size = min(block_size, args.block_size)
        block_num = -(-block.size() // block_size)
        print(f"block size: {block_size}, total blocks: {block_num}")

        for i in range(1, block_num + 1):
            start = (i - 1) * block_size
            stop = i * block_size
            data: bytes = bytes(block.data[start:stop])
            for _ in range(10):
                try:
                    print(f"Send {i}, attempt: {_ + 1}")
                    client.transfer_data(i & 0xFF, data)
                    time.sleep(args.transfer_delay)
                    break
                except TimeoutException:
                    time.sleep(random.random())
                    continue
                except NegativeResponseException:
                    break

            else:
                raise TimeoutException

    with Client(conn, config=uds_config) as client:
        print("Change session to programming")
        for _ in range(3):
            try:
                client.change_session(2)
                break
            except TimeoutException:
                continue

        print("Reset ECU")
        for _ in range(3):
            try:
                client.ecu_reset(3)
                break
            except TimeoutException:
                continue

        time.sleep(5)
        print("Erasing flash")
        for erase_try in range(5):
            try:
                client.routine_control(UDS_ERASE_FLASH_ROUTINE_ID, START_ROUTINE)
                break
            except TimeoutException:
                continue
            except NegativeResponseException:
                break
        for _ in range(10):
            time.sleep(0.5)
            try:
                result = client.routine_control(UDS_ERASE_FLASH_ROUTINE_ID, REQUEST_ROUTINE_RESULTS)
            except TimeoutException:
                continue
            except NegativeResponseException:
                continue
            if result:
                payload = result.get_payload()
                if payload[-1] == 0x02:
                    print("Finished")
                    break
                elif payload[-1] == 0x01:
                    print("Erasing...")
                else:
                    print("Error")
                    break
        time.sleep(0.5)
        for block in blocks:
            write_block(block)
            client.request_transfer_exit()

        routine_id = 0xFF01
        try:
            client.routine_control(routine_id, 1)
        except NegativeResponseException:
            pass
        for _ in range(10):
            time.sleep(0.5)
            try:
                result = client.routine_control(routine_id, 3)
            except TimeoutException:
                pass
            except NegativeResponseException:
                pass
            if result:
                payload = result.get_payload()
                if payload[-1] == 0x02:
                    print("CRC is correct")
                    break
                elif payload[-1] == 0x01:
                    print("Checking...")
                else:
                    print("CRC is incorrect")
                    break
        time.sleep(2)
        print("Change session to default")
        client.change_session(1)
        print("Reset ECU")
        client.ecu_reset(3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="CAN LOADER")
    parser.add_argument('--path', default='smc.bin')
    parser.add_argument('--port', default='can0')
    parser.add_argument('--block-size', default=256, type=int)
    parser.add_argument('--transfer-delay', type=float, default=0.01)
    parser.add_argument('--mode', default='firmware', choices=['firmware', 'bootloader'])
    args = parser.parse_args()

    main(args)
