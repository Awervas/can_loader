import argparse

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


blocks: list[Block] = []


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
    uds_config['request_timeout'] = 5

    # bus = SeeedBus(channel="COM6", frame_type="EXT")

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
    else:
        raise ValueError
    print('INIT BUS')
    time.sleep(0.25)
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
        base_addr = 0x08020000 + block.address
        memory = MemoryLocation(
            base_addr,
            block.size(),
            address_format=32,
            memorysize_format=32,
        )

        response = client.request_download(memory)
        max_block = response.service_data.max_length
        block_size = min(max_block, 512)

        block_num = -(-block.size() // block_size)
        print(f"[Block] addr=0x{base_addr:08X}, size={block.size()}, "
              f"max_from_ecu={max_block}, using={block_size}, total_blocks={block_num}")

        for block_index in range(block_num):
            start = block_index * block_size
            stop = start + block_size
            data = bytes(block.data[start:stop])
            if not data:
                continue

            seq = (block_index + 1) & 0xFF
            cur_addr = base_addr + start

            print(f"  Send {block_index + 1}, seq={seq}, len={len(data)}, addr=0x{cur_addr:08X}")
            client.transfer_data(seq, data)
            time.sleep(0.04)

    with Client(conn, config=uds_config) as client:
        print("Change session to programming")
        client.change_session(2)
        print("Reset ECU")
        client.ecu_reset(3)
        time.sleep(5)
        print("Erasing flash")
        routine_id = 0xFF00
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

    notifier.stop()
    bus.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="CAN LOADER")
    parser.add_argument('--path', default='smc.bin')
    parser.add_argument('--port', default='can0')
    args = parser.parse_args()

    main(args)
