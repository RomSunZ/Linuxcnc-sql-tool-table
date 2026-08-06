#!/usr/bin/env python3
"""
tool_db_client.py — клиент unix-сокета tool_db_daemon.py.

Одно соединение. Ответы ("ok") и push ("event") по одному каналу.
request() синхронный с таймаутом; push копятся в notify_queue.
Сокет с timeout — никогда не блокирует GUI навечно.
"""
import socket
import json
import threading
import queue
import time


class ToolDBClient:
    def __init__(self, sock_path, timeout=1.5):
        self.sock_path = sock_path
        self.timeout = float(timeout)
        self._sock = None
        self._send_lock = threading.Lock()
        self._responses = queue.Queue()
        self.notify_queue = queue.Queue()
        self._stop = False
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    def _ensure_connected(self):
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self._sock = s

    def _drop(self):
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    def _reader_loop(self):
        buf = b""
        while not self._stop:
            try:
                self._ensure_connected()
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise OSError("connection closed")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if "event" in obj:
                        try:
                            self.notify_queue.put_nowait(obj)
                        except queue.Full:
                            pass
                    else:
                        self._responses.put(obj)
            except (OSError, ConnectionError, socket.timeout):
                self._drop()
                buf = b""
                # сбросить зависшие waiters
                try:
                    self._responses.put_nowait({"ok": False, "error": "disconnected"})
                except queue.Full:
                    pass
                if not self._stop:
                    time.sleep(0.5)

    def request(self, op, **kwargs):
        payload = {"op": op}
        payload.update(kwargs)
        line = (json.dumps(payload) + "\n").encode("utf-8")
        with self._send_lock:
            # очистить старые ответы, чтобы не схватить чужой
            try:
                while True:
                    self._responses.get_nowait()
            except queue.Empty:
                pass
            for _ in range(2):
                try:
                    self._ensure_connected()
                    self._sock.sendall(line)
                    return self._responses.get(timeout=self.timeout)
                except (OSError, ConnectionError, socket.timeout, queue.Empty):
                    self._drop()
            return {"ok": False, "error": "daemon unreachable"}

    def list_tools(self):
        return self.request("list")

    def get_tool(self, toolno):
        resp = self.list_tools()
        if not resp.get("ok"):
            return {}
        tools = resp.get("tools") or {}
        # ключи после JSON — строки
        return tools.get(str(int(toolno))) or tools.get(int(toolno)) or {}

    def update_tool(self, toolno, fields):
        return self.request("update", toolno=int(toolno), fields=fields or {})

    def create_tool(self, toolno, fields=None):
        return self.request("create", toolno=int(toolno), fields=fields or {})

    def delete_tool(self, toolno):
        return self.request("delete", toolno=int(toolno))

    def rename_tool(self, old_toolno, new_toolno):
        return self.request("rename", toolno=old_toolno, new_toolno=new_toolno)
