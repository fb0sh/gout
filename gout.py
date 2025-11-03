#!/usr/bin/env python3
import socket
import sys
import threading
import datetime
import json
import time

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
    print(f"[gout_server {timestamp}] {msg}")


class ForwardClient:
    def __init__(
        self, host: str, port: int, protocol: str = "tcp", forward_port: int = None
    ):
        self.host = host
        self.port = port
        self.forward_port = forward_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        client_config = {
            "protocol": protocol,
            "port": forward_port,
            "password": CLIENT_CONFIG["verify_password"],
        }

        try:
            self.sock.send(json.dumps(client_config).encode())
            data = json.loads(self.sock.recv(1024).decode())
            ip = data["ip"]
            port = data["port"]
            log(f"forward server: {ip}:{port}")

        except Exception as e:
            log(f"client config error: {e}")
            self.sock.close()
            return

        self.start_tunnel()

    def start_tunnel(self):
        def _fwd(src: socket.socket, dst: socket.socket):
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

        log("start tunnel")
        real_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        real_srv.connect(("127.0.0.1", self.forward_port))
        t1 = threading.Thread(target=_fwd, args=(self.sock, real_srv))
        t2 = threading.Thread(target=_fwd, args=(real_srv, self.sock))
        t1.start()
        t2.start()
        t1.join()
        t2.join()  # 阻塞直到两个线程都退出


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python gout.py <protocol> <forward_port>")
        sys.exit(1)
    protocol = sys.argv[1]
    forward_port = int(sys.argv[2])
    host = CLIENT_CONFIG["host"]
    port = CLIENT_CONFIG["port"]
    client = ForwardClient(host, port, protocol, forward_port)
