import argparse
from typing import List

import can
from can.interfaces.seeedstudio import SeeedBus
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.client import Client
from udsoncan.common.MemoryLocation import MemoryLocation
from udsoncan.exceptions import NegativeResponseException, TimeoutException
import udsoncan.configs
import isotp
import time


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
    my_block_size = 1024
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
        "rx_flowcontrol_timeout": 5000,
        "rx_consecutive_frame_timeout": 5000,
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
    uds_config['request_timeout'] = 15

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

    elif 'COM' in args.port.upper():
        bus = SeeedBus(channel=args.port, frame_type="EXT")

    else:
        raise ValueError


    print('INIT BUS')
    time.sleep(0.55)
    notifier = can.Notifier(bus, [])
    tp_addr = isotp.Address(
        isotp.AddressingMode.NormalFixed_29bits,
        target_address=0xF3,
        source_address=0xF1,
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
        response = client.request_download(memory)
        block_size = int.from_bytes(response.get_payload()[2:], byteorder='big')
        block_num = -(-block.size() // block_size)
        print(f"block size: {block_size}, total blocks: {block_num}")

        for i in range(1, block_num + 1):
            start = (i - 1) * block_size
            stop = i * block_size
            data: bytes = bytes(block.data[start:stop])
            client.transfer_data(i & 0xFF, data)
            print(f"Send {i}")

    with Client(conn, config=uds_config) as client:
        print("Change session to programming")
        client.change_session(2)
        print("Reset ECU")
        client.ecu_reset(3)
        time.sleep(5)
        print("Erasing flash")
        routine_id = 0xFF00
        for erase_try in range(5):
            try:
                client.routine_control(routine_id, 1)
                break
            except TimeoutException:
                continue
            except NegativeResponseException:
                break
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
    args = parser.parse_args()

    main(args)
