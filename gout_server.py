#!/usr/bin/env python3
import socket
import datetime
import threading
import json
import struct
import sys

SERVER_CONFIG = {
    "return_ip": "127.0.0.1",  # 如果在内网，无需获取公网IP
    "host": "0.0.0.0",
    "port": 3147,
    "verify_password": "passwd@gout",
    "max_connections": 100,
    "min_port": 1024,
    "max_port": 65535,
}


def log(msg):
    """统一日志打印，带时间"""
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y_%m_%d-%H:%M.") + f"{now.microsecond // 100:04d}"
    # 解释：microsecond//100得到前4位毫秒精度
    print(f"[gout_server {timestamp}] {msg}")


def get_public_ip() -> str:
    if SERVER_CONFIG["return_ip"]:
        return SERVER_CONFIG["return_ip"]

    import requests

    services = [
        "https://ifconfig.co/ip",
        "https://icanhazip.com",
    ]
    for url in services:
        try:
            r = requests.get(url, timeout=3)
            ip = r.text.strip()
            if ip:
                return ip
        except Exception:
            continue
    return None


PUBLIC_IP = get_public_ip()
if not PUBLIC_IP:
    raise RuntimeError("Failed to get public IP")


def get_free_port(min_port: int, max_port: int) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    if min_port <= port <= max_port:
        return port
    raise RuntimeError("Failed to get free port in range")


class ForwardServer:
    def __init__(self, host: str, port: int, max_connections: int = 100):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(max_connections)

    def start_tunnel(self, control_conn: socket.socket, client_config: dict):
        def _fwd(src: socket.socket, dst: socket.socket):
            """TCP 双向转发，不使用事件"""
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

        def handle_external_connection(external_conn: socket.socket):
            """处理每个外部连接：通知客户端并等待数据连接"""
            try:
                # 通知客户端有新连接
                control_conn.sendall(b"NEW_CONN\n")

                # 等待客户端建立数据连接到服务器
                data_conn, _ = data_srv.accept()

                # 双向转发
                t1 = threading.Thread(
                    target=_fwd, args=(external_conn, data_conn), daemon=True
                )
                t2 = threading.Thread(
                    target=_fwd, args=(data_conn, external_conn), daemon=True
                )
                t1.start()
                t2.start()
            except Exception as e:
                log(f"Handle external connection error: {e}")
                external_conn.close()

        # 创建数据连接监听端口
        data_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        data_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        data_srv.bind(("0.0.0.0", 0))
        data_srv.listen(100)
        data_port = data_srv.getsockname()[1]

        # 创建公网访问端口
        target_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        free_port = get_free_port(SERVER_CONFIG["min_port"], SERVER_CONFIG["max_port"])
        target_srv.bind(("0.0.0.0", free_port))
        target_srv.listen(100)
        log(
            f"new tunnel {PUBLIC_IP}:{free_port} -> {control_conn.getpeername()[0]}:{client_config['port']}"
        )

        # 返回配置给客户端
        response = {"ip": PUBLIC_IP, "port": free_port, "data_port": data_port}
        control_conn.sendall(json.dumps(response).encode())

        # 持续接受外部连接
        while True:
            try:
                external_conn, _ = target_srv.accept()
                threading.Thread(
                    target=handle_external_connection,
                    args=(external_conn,),
                    daemon=True,
                ).start()
            except Exception as e:
                log(f"Accept external connection error: {e}")
                break

    def start_udp_tunnel(self, control_conn: socket.socket, client_config: dict):
        """UDP 转发：服务器接收 UDP，通过 TCP 控制连接传输给客户端"""

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

        # 创建公网 UDP socket
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        free_port = get_free_port(SERVER_CONFIG["min_port"], SERVER_CONFIG["max_port"])
        udp_sock.bind(("0.0.0.0", free_port))

        log(
            f"new UDP tunnel {PUBLIC_IP}:{free_port} -> {control_conn.getpeername()[0]}:{client_config['port']}"
        )

        # 返回配置给客户端
        response = {"ip": PUBLIC_IP, "port": free_port, "protocol": "udp"}
        control_conn.sendall(json.dumps(response).encode())

        # 从外部接收 UDP 并发送给客户端
        def udp_to_client():
            while True:
                try:
                    data, addr = udp_sock.recvfrom(65535)
                    packet = encode_udp_packet(addr, data)
                    # 发送给客户端：先发送包长度，再发送数据
                    control_conn.sendall(struct.pack("!I", len(packet)) + packet)
                except Exception as e:
                    log(f"UDP to client error: {e}")
                    break

        # 从客户端接收并发送到外部 UDP
        def client_to_udp():
            buffer = b""
            while True:
                try:
                    data = control_conn.recv(4096)
                    if not data:
                        break
                    buffer += data

                    # 处理完整的包
                    while len(buffer) >= 4:
                        packet_len = struct.unpack("!I", buffer[:4])[0]
                        if len(buffer) < 4 + packet_len:
                            break

                        packet = buffer[4 : 4 + packet_len]
                        buffer = buffer[4 + packet_len :]

                        addr, udp_data = decode_udp_packet(packet)
                        udp_sock.sendto(udp_data, addr)
                except Exception as e:
                    log(f"Client to UDP error: {e}")
                    break

        t1 = threading.Thread(target=udp_to_client, daemon=True)
        t2 = threading.Thread(target=client_to_udp, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def handle_client(self, client: socket.socket):
        data = json.loads(client.recv(1024).decode())
        try:
            client_config = {
                "protocol": data["protocol"],
                "port": data["port"],
                "password": data["password"],
            }
            if client_config["password"] != SERVER_CONFIG["verify_password"]:
                log(f"invalid password from {client.getpeername()}")
                client.close()
                return

            # 根据协议类型选择不同的处理方式
            if client_config["protocol"] == "udp":
                self.start_udp_tunnel(client, client_config)
            else:
                self.start_tunnel(client, client_config)
        except Exception as e:
            log(f"client config error: {e}")
            client.close()
            return

    def run(self):
        log(f"public IP: {PUBLIC_IP}")
        log(f"listening {SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")

        while True:
            try:
                client, addr = self.srv.accept()
                log(f"new connection from {addr}")
                threading.Thread(target=self.handle_client, args=(client,)).start()
            except Exception as e:
                log(f"accept error: {e}")
                continue


def print_help():
    """Print help message"""
    help_text = """
Gout Server - Port Forwarding Server
====================================

USAGE:
    python gout_server.py
    python gout_server.py -h | --help

OPTIONS:
    -h, --help   Show this help message

CONFIGURATION:
    Edit SERVER_CONFIG in gout_server.py to change:
    - return_ip: Public IP to return to clients (set to None for auto-detect)
    - host: Listen address (default: 0.0.0.0, all interfaces)
    - port: Listen port (default: 3147)
    - verify_password: Authentication password
    - max_connections: Maximum concurrent connections
    - min_port: Minimum port for dynamic allocation (default: 1024)
    - max_port: Maximum port for dynamic allocation (default: 65535)

FEATURES:
    - TCP port forwarding with multiple concurrent connections
    - UDP port forwarding with session management
    - Password authentication
    - Dynamic port allocation
    - Multi-client support

SUPPORTED PROTOCOLS:
    - TCP: Full duplex forwarding with connection multiplexing
    - UDP: Stateless forwarding with client session tracking

HOW IT WORKS:
    1. Server listens on configured port (default: 3147)
    2. Clients connect and request port forwarding
    3. Server allocates public port(s) for each client
    4. External traffic is forwarded to client, then to local service
    5. Multiple clients can run simultaneously

SECURITY:
    - Password authentication required for all clients
    - Configure verify_password in SERVER_CONFIG
    - Default password: passwd@gout (CHANGE THIS!)

EXAMPLES:
    # Start server with default settings
    python gout_server.py

    # View help
    python gout_server.py --help

NOTES:
    - Server must be accessible from clients
    - Firewall rules may need configuration for allocated ports
    - Port range (min_port to max_port) must allow binding
    - For public access, ensure return_ip is set to public IP
"""
    print(help_text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
        print_help()
        sys.exit(0)

    print("=" * 60)
    print("Gout Server - Port Forwarding Server")
    print("=" * 60)
    print(f"Listen: {SERVER_CONFIG['host']}:{SERVER_CONFIG['port']}")
    print(f"Port range: {SERVER_CONFIG['min_port']}-{SERVER_CONFIG['max_port']}")
    print(f"Max connections: {SERVER_CONFIG['max_connections']}")
    print("=" * 60)
    print()

    try:
        server = ForwardServer(
            SERVER_CONFIG["host"],
            SERVER_CONFIG["port"],
            SERVER_CONFIG["max_connections"],
        )
        server.run()
    except KeyboardInterrupt:
        print("\nServer stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nFailed to start server: {e}")
        sys.exit(1)
