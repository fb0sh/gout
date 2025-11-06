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
            self.data_port = data["data_port"]
            log(f"forward server: {self.server_ip}:{self.server_port}")
            log(f"data port: {self.data_port}")

        except Exception as e:
            log(f"client config error: {e}")
            self.control_conn.close()
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
                t1 = threading.Thread(target=_fwd, args=(data_conn, local_conn), daemon=True)
                t2 = threading.Thread(target=_fwd, args=(local_conn, data_conn), daemon=True)
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
                        threading.Thread(target=handle_new_connection, daemon=True).start()
            except Exception as e:
                log(f"Control connection error: {e}")
                break


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python gout.py <protocol> <forward_port>")
        sys.exit(1)
    protocol = sys.argv[1]
    forward_port = int(sys.argv[2])
    host = CLIENT_CONFIG["host"]
    port = CLIENT_CONFIG["port"]
    client = ForwardClient(host, port, protocol, forward_port)
