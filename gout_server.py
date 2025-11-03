#!/usr/bin/env python3
import socket
import requests
import datetime
import threading
import json

SERVER_CONFIG = {
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
    return "127.0.0.1"  # 仅用于测试
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

    def start_tunnel(self, client: socket.socket, client_config: dict):
        def _fwd(src: socket.socket, dst: socket.socket):
            """TCP 双向转发，不使用事件"""
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception as e:
                log(f"Forward error: {e}")
            finally:
                try:
                    src.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                try:
                    dst.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                src.close()
                dst.close()

        target_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        free_port = get_free_port(SERVER_CONFIG["min_port"], SERVER_CONFIG["max_port"])
        target_srv.bind(("0.0.0.0", free_port))
        target_srv.listen(1)
        log(
            f"new tunnel {PUBLIC_IP}:{free_port} -> {client.getpeername()[0]}:{client_config['port']}"
        )
        # first replay
        client.sendall(json.dumps({"ip": PUBLIC_IP, "port": free_port}).encode())

        while True:
            real_client, _ = target_srv.accept()
            threading.Thread(
                target=_fwd, args=(client, real_client), daemon=True
            ).start()
            threading.Thread(
                target=_fwd, args=(real_client, client), daemon=True
            ).start()

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


if __name__ == "__main__":
    server = ForwardServer(
        SERVER_CONFIG["host"], SERVER_CONFIG["port"], SERVER_CONFIG["max_connections"]
    )
    server.run()
