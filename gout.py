#!/usr/bin/env python3
import socket
import sys
import threading
import datetime
import json
import struct

CLIENT_CONFIG = {
    "host": "127.0.0.1",
    "port": 3147,
    "verify_password": "passwd@gout",
}


def log(msg):
    """统一日志打印，带时间"""
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y_%m_%d-%H:%M.") + f"{now.microsecond // 100:04d}"
    # 解释：microsecond//100得到前4位毫秒精度
    print(f"[gout {timestamp}] {msg}")


class ForwardClient:
    def __init__(
        self, host: str, port: int, protocol: str = "tcp", forward_port: int = None
    ):
        self.host = host
        self.port = port
        self.forward_port = forward_port
        self.control_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control_conn.connect((host, port))
        client_config = {
            "protocol": protocol,
            "port": forward_port,
            "password": CLIENT_CONFIG["verify_password"],
        }

        try:
            self.control_conn.send(json.dumps(client_config).encode())
            data = json.loads(self.control_conn.recv(1024).decode())
            self.server_ip = data["ip"]
            self.server_port = data["port"]
            self.protocol = protocol
            log(f"forward server: {self.server_ip}:{self.server_port}")

            if protocol == "udp":
                log("UDP mode")
            else:
                self.data_port = data.get("data_port")
                log(f"data port: {self.data_port}")

        except Exception as e:
            log(f"client config error: {e}")
            self.control_conn.close()
            return

        if self.protocol == "udp":
            self.start_udp_tunnel()
        else:
            self.start_tunnel()

    def start_tunnel(self):
        def _fwd(src: socket.socket, dst: socket.socket):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass  # 连接关闭是正常行为，不需要记录
            finally:
                # 安全地关闭 socket，忽略所有错误
                for sock in [src, dst]:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except:
                        pass
                    try:
                        sock.close()
                    except:
                        pass

        def handle_new_connection():
            """处理每个新连接：连接到服务器数据端口和本地服务"""
            try:
                # 连接到服务器数据端口
                data_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                data_conn.connect((self.host, self.data_port))

                # 连接到本地服务
                local_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                local_conn.connect(("127.0.0.1", self.forward_port))

                # 双向转发
                t1 = threading.Thread(
                    target=_fwd, args=(data_conn, local_conn), daemon=True
                )
                t2 = threading.Thread(
                    target=_fwd, args=(local_conn, data_conn), daemon=True
                )
                t1.start()
                t2.start()
            except Exception as e:
                log(f"Handle new connection error: {e}")

        log("start tunnel, waiting for connections...")

        # 持续监听控制连接上的通知
        buffer = b""
        while True:
            try:
                data = self.control_conn.recv(1024)
                if not data:
                    log("Control connection closed")
                    break

                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line == b"NEW_CONN":
                        log("New connection request received")
                        threading.Thread(
                            target=handle_new_connection, daemon=True
                        ).start()
            except Exception as e:
                log(f"Control connection error: {e}")
                break

    def start_udp_tunnel(self):
        """UDP 转发：通过 TCP 控制连接接收/发送 UDP 数据"""

        def encode_udp_packet(addr: tuple, data: bytes) -> bytes:
            """编码 UDP 包：4字节长度 + IP + 端口 + 数据"""
            ip_bytes = socket.inet_aton(addr[0])
            port_bytes = struct.pack("!H", addr[1])
            data_len = struct.pack("!I", len(data))
            return data_len + ip_bytes + port_bytes + data

        def decode_udp_packet(packet: bytes) -> tuple:
            """解码 UDP 包：返回 (addr, data)"""
            data_len = struct.unpack("!I", packet[:4])[0]
            ip = socket.inet_ntoa(packet[4:8])
            port = struct.unpack("!H", packet[8:10])[0]
            data = packet[10 : 10 + data_len]
            return (ip, port), data

        # 维护客户端会话映射：本地端口 -> 远程客户端地址
        session_map = {}
        # 为每个外部客户端创建一个本地 socket
        client_sockets = {}

        log("start UDP tunnel, waiting for packets...")

        # 从服务器接收 UDP 数据并转发到本地
        def server_to_local():
            buffer = b""
            while True:
                try:
                    data = self.control_conn.recv(4096)
                    if not data:
                        log("Control connection closed")
                        break
                    buffer += data

                    # 处理完整的包
                    while len(buffer) >= 4:
                        packet_len = struct.unpack("!I", buffer[:4])[0]
                        if len(buffer) < 4 + packet_len:
                            break

                        packet = buffer[4 : 4 + packet_len]
                        buffer = buffer[4 + packet_len :]

                        remote_addr, udp_data = decode_udp_packet(packet)
                        remote_key = f"{remote_addr[0]}:{remote_addr[1]}"

                        # 为每个远程客户端创建一个本地 socket
                        if remote_key not in client_sockets:
                            local_sock = socket.socket(
                                socket.AF_INET, socket.SOCK_DGRAM
                            )
                            local_sock.bind(("127.0.0.1", 0))
                            local_port = local_sock.getsockname()[1]
                            client_sockets[remote_key] = local_sock
                            session_map[local_port] = remote_addr

                            # 启动接收线程
                            def recv_from_local(sock, l_port):
                                while True:
                                    try:
                                        reply_data, _ = sock.recvfrom(65535)
                                        # 根据本地端口找到对应的远程客户端
                                        if l_port in session_map:
                                            r_addr = session_map[l_port]
                                            packet = encode_udp_packet(
                                                r_addr, reply_data
                                            )
                                            self.control_conn.sendall(
                                                struct.pack("!I", len(packet)) + packet
                                            )
                                            log(
                                                f"UDP reply to {r_addr[0]}:{r_addr[1]}, {len(reply_data)} bytes"
                                            )
                                    except Exception as e:
                                        log(f"Recv from local error: {e}")
                                        break

                            threading.Thread(
                                target=recv_from_local,
                                args=(local_sock, local_port),
                                daemon=True,
                            ).start()

                        # 转发到本地服务
                        local_sock = client_sockets[remote_key]
                        local_sock.sendto(udp_data, ("127.0.0.1", self.forward_port))
                        log(
                            f"UDP from {remote_addr[0]}:{remote_addr[1]} -> local:{self.forward_port}, {len(udp_data)} bytes"
                        )

                except Exception as e:
                    log(f"Server to local error: {e}")
                    break

        t1 = threading.Thread(target=server_to_local, daemon=True)
        t1.start()
        t1.join()


def print_help():
    """Print help message"""
    help_text = """
Gout Client - Port Forwarding Client
====================================

USAGE:
    python gout.py <protocol> <local_port>
    python gout.py -h | --help

ARGUMENTS:
    protocol     Protocol type: tcp or udp
    local_port   Local port to forward (must be running a service on this port)

OPTIONS:
    -h, --help   Show this help message

CONFIGURATION:
    Edit CLIENT_CONFIG in gout.py to change:
    - host: Server address (default: 127.0.0.1)
    - port: Server port (default: 3147)
    - verify_password: Authentication password

EXAMPLES:
    # Forward local TCP port 80 (HTTP server)
    python gout.py tcp 80

    # Forward local TCP port 3306 (MySQL)
    python gout.py tcp 3306

    # Forward local UDP port 53 (DNS)
    python gout.py udp 53

    # Forward local UDP port 5353
    python gout.py udp 5353

HOW IT WORKS:
    1. Client connects to gout_server
    2. Server allocates a public port
    3. External traffic to public port is forwarded to your local service
    4. Client maintains persistent connection for multiple requests

NOTES:
    - Make sure gout_server is running before starting the client
    - Local service must be running on the specified port
    - TCP mode: Supports multiple concurrent connections
    - UDP mode: Supports multiple concurrent clients with session management
"""
    print(help_text)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ["-h", "--help"]:
        print_help()
        sys.exit(0)

    if len(sys.argv) != 3:
        print("Error: Invalid number of arguments\n")
        print_help()
        sys.exit(1)

    protocol = sys.argv[1].lower()
    if protocol not in ["tcp", "udp"]:
        print(f"Error: Invalid protocol '{protocol}'. Must be 'tcp' or 'udp'\n")
        print_help()
        sys.exit(1)

    try:
        forward_port = int(sys.argv[2])
        if not (1 <= forward_port <= 65535):
            raise ValueError("Port must be between 1 and 65535")
    except ValueError as e:
        print(f"Error: Invalid port '{sys.argv[2]}'. {e}\n")
        print_help()
        sys.exit(1)

    host = CLIENT_CONFIG["host"]
    port = CLIENT_CONFIG["port"]

    print("Gout Client Starting...")
    print(f"Server: {host}:{port}")
    print(f"Protocol: {protocol.upper()}")
    print(f"Local port: {forward_port}")
    print()

    try:
        client = ForwardClient(host, port, protocol, forward_port)
    except KeyboardInterrupt:
        print("\nClient stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nFailed to start client: {e}")
        sys.exit(1)
