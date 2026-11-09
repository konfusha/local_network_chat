#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LAN-чат в терминале — только стандартная библиотека Python.

Быстрый старт:
    python chat.py                       # мастер настройки
    python chat.py host                  # этот компьютер ждёт подключения
    python chat.py join 192.168.1.10     # подключиться к хосту

Параметры: -p/--port (по умолчанию 50500), -n/--name.

Протокол: TCP-соединение, по одному JSON-объекту в строке.
Никаких сторонних модулей — нужен только Python 3.8+.
"""

import argparse
import codecs
import getpass
import json
import os
import select
import shutil
import socket
import sys
import threading
import time

DEFAULT_PORT = 50500
TYPING_SEND_INTERVAL = 1.5   # как часто повторять сигнал «печатает», сек
TYPING_SHOW_TIME = 3.0       # сколько держать индикатор без новых сигналов, сек
PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

class Style:
    """ANSI-цвета. enabled включается, только если терминал их поддерживает."""

    enabled = False

    @classmethod
    def wrap(cls, code, text):
        if not cls.enabled:
            return text
        return "\033[" + code + "m" + text + "\033[0m"

    @classmethod
    def dim(cls, text):
        return cls.wrap("2", text)

    @classmethod
    def green(cls, text):
        return cls.wrap("32", text)

    @classmethod
    def cyan(cls, text):
        return cls.wrap("36", text)


def configure_streams():
    """UTF-8 для консоли и перенаправленного вывода (в т.ч. на Windows)."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_terminal():
    """Включает VT/ANSI в консоли Windows. True — можно рисовать интерфейс."""
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            if not kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ):
                return False
        except Exception:
            return False
    return True


def sanitize(text):
    """Убирает управляющие символы, чтобы собеседник не «ломал» терминал."""
    return "".join(ch for ch in str(text) if ch >= " " or ch == "\t")


def format_message(name, text, mine=False):
    stamp = Style.dim("[%s]" % time.strftime("%H:%M"))
    who = Style.cyan(name) if mine else Style.green(name)
    return "%s %s: %s" % (stamp, who, text)


def format_system(text):
    return Style.dim("— " + text + " —")


def print_banner():
    print("=" * 58)
    print("  LAN-чат в терминале  ·  только стандартная библиотека")
    print("=" * 58)


def local_ips():
    """IP-адреса этого компьютера в локальной сети (основной — первым)."""
    ips = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))  # пакеты не отправляются
            ips.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


# ---------------------------------------------------------------------------
# Ввод по одному символу (stdin)
# ---------------------------------------------------------------------------

class WindowsKeySource:
    """Неблокирующее чтение клавиш в консоли Windows."""

    fd = None

    def __init__(self):
        import msvcrt

        self._msvcrt = msvcrt

    def read(self):
        events = []
        while self._msvcrt.kbhit():
            ch = self._msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # стрелки, F-клавиши и т.п. — пропускаем
                if self._msvcrt.kbhit():
                    self._msvcrt.getwch()
                continue
            events.append(("char", ch))
        return events

    def restore(self):
        pass


class UnixKeySource:
    """cbreak-режим терминала на Linux/macOS + чтение UTF-8."""

    def __init__(self):
        import termios
        import tty

        self._termios = termios
        self.fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self):
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return [("eof",)]
        if not data:
            return [("eof",)]
        return [("char", ch) for ch in self._decoder.decode(data)]

    def restore(self):
        try:
            self._termios.tcsetattr(
                self.fd, self._termios.TCSADRAIN, self._saved
            )
        except Exception:
            pass


def make_key_source():
    if not sys.stdin.isatty():
        return None
    try:
        if os.name == "nt":
            return WindowsKeySource()
        return UnixKeySource()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

class Peer:
    """TCP-соединение и построчный JSON-протокол."""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False
        self._buf = b""

    def send(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            self.sock.sendall(data)
        except OSError:
            self.closed = True

    def recv_once(self):
        """Читает уже доступные данные. Возвращает список сообщений."""
        try:
            chunk = self.sock.recv(65536)
        except OSError:
            chunk = b""
        if not chunk:
            self.closed = True
            return []
        self._buf += chunk
        messages = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                messages.append(obj)
        return messages

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed = True


def handshake(peer, my_name, timeout=30.0):
    """Обменивается именами. Возвращает (имя собеседника, прочие сообщения)."""
    peer.send({"t": "hello", "name": my_name, "v": PROTOCOL_VERSION})
    deadline = time.monotonic() + timeout
    pending = []
    while time.monotonic() < deadline:
        if peer.closed:
            break
        try:
            ready, _, _ = select.select([peer.sock], [], [], 0.5)
        except (OSError, ValueError):
            break
        if not ready:
            continue
        for msg in peer.recv_once():
            if msg.get("t") == "hello":
                name = sanitize(msg.get("name") or "").strip()[:32]
                return name or "Собеседник", pending
            pending.append(msg)
    raise ConnectionError("собеседник не ответил на приветствие")


# ---------------------------------------------------------------------------
# Экран: строка ввода + индикатор «печатает…» справа
# ---------------------------------------------------------------------------

class Screen:
    def __init__(self, prompt):
        self.prompt = prompt
        self.buffer = ""
        self.peer_name = ""
        self.peer_typing = False

    @staticmethod
    def _width():
        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except Exception:
            width = 80
        return max(20, width - 1)  # -1, чтобы не было автопереноса

    def render(self):
        text = self.prompt + self.buffer
        status = ""
        if self.peer_typing and self.peer_name:
            status = "  %s печатает…" % self.peer_name
        room = self._width() - len(status)
        if room < 20:
            status = ""
            room = self._width()
        if len(text) > room:
            text = "" if room <= 1 else "…" + text[-(room - 1):]
        out = "\r\033[K" + text
        if status:
            # Сохраняем курсор, рисуем статус справа, возвращаем курсор на место
            out += "\0337" + Style.dim(status) + "\0338"
        sys.stdout.write(out)
        sys.stdout.flush()

    def print_line(self, text):
        sys.stdout.write("\r\033[K" + text + "\n")
        self.render()


# ---------------------------------------------------------------------------
# Простой режим (терминал без интерактивных возможностей)
# ---------------------------------------------------------------------------

def run_plain(peer, my_name, peer_name, pending):
    def emit(text):
        print(text, flush=True)

    emit(format_system("Простой режим: индикатор набора недоступен."))
    emit(format_system("Собеседник: %s" % peer_name))
    emit(format_system("Enter — отправить, /quit — выход."))

    stop = threading.Event()
    typing_shown = [False]

    def handle(msg):
        kind = msg.get("t")
        if kind == "msg":
            emit(format_message(peer_name, sanitize(msg.get("text", ""))))
            typing_shown[0] = False
        elif kind == "typing":
            if not typing_shown[0]:
                typing_shown[0] = True
                emit(Style.dim("… %s печатает" % peer_name))
        elif kind == "stop_typing":
            typing_shown[0] = False
        elif kind == "bye":
            emit(format_system("%s вышел из чата" % peer_name))
            stop.set()

    for msg in pending:
        handle(msg)

    def receiver():
        while not stop.is_set():
            try:
                ready, _, _ = select.select([peer.sock], [], [], 0.3)
            except (OSError, ValueError):
                break
            if not ready:
                if peer.closed:
                    emit(format_system("Соединение разорвано"))
                    stop.set()
                    return
                continue
            for msg in peer.recv_once():
                handle(msg)
            if peer.closed and not stop.is_set():
                emit(format_system("Соединение разорвано"))
                stop.set()
                return

    threading.Thread(target=receiver, daemon=True).start()

    while not stop.is_set():
        try:
            text = input("%s> " % my_name)
        except (EOFError, KeyboardInterrupt):
            break
        text = text.strip()
        if not text:
            continue
        if text in ("/quit", "/exit", "/выход"):
            break
        peer.send({"t": "msg", "text": text})

    # Даем последнее сообщение собеседника «долететь» перед выходом
    time.sleep(0.4)
    peer.send({"t": "bye"})
    stop.set()
    peer.close()
    emit(format_system("Вы вышли из чата"))


# ---------------------------------------------------------------------------
# Интерактивный режим
# ---------------------------------------------------------------------------

def run_interactive(peer, my_name, peer_name, pending, reader):
    screen = Screen("%s> " % my_name)
    screen.peer_name = peer_name

    screen.print_line(
        format_system("Соединение установлено. Собеседник: %s" % peer_name)
    )
    screen.print_line(
        format_system(
            "Enter — отправить, Ctrl+U — очистить, Ctrl+L — экран, Ctrl+C — выход"
        )
    )

    running = True
    for msg in pending:
        kind = msg.get("t")
        if kind == "msg":
            screen.print_line(
                format_message(peer_name, sanitize(msg.get("text", "")))
            )
        elif kind == "bye":
            running = False

    typing_until = 0.0
    last_typing_sent = 0.0
    esc_pending = False
    need_render = True

    try:
        while running:
            timeout = 0.05 if reader.fd is None else 0.2
            fds = [peer.sock]
            if reader.fd is not None:
                fds.append(reader.fd)
            try:
                ready, _, _ = select.select(fds, [], [], timeout)
            except (OSError, ValueError):
                break

            # --- входящие сообщения ---
            if peer.sock in ready:
                for msg in peer.recv_once():
                    kind = msg.get("t")
                    if kind == "msg":
                        screen.print_line(
                            format_message(
                                peer_name, sanitize(msg.get("text", ""))
                            )
                        )
                        screen.peer_typing = False
                    elif kind == "typing":
                        typing_until = time.monotonic() + TYPING_SHOW_TIME
                        if not screen.peer_typing:
                            screen.peer_typing = True
                            need_render = True
                    elif kind == "stop_typing":
                        typing_until = 0.0
                        if screen.peer_typing:
                            screen.peer_typing = False
                            need_render = True
                    elif kind == "bye":
                        screen.print_line(
                            format_system("%s вышел из чата" % peer_name)
                        )
                        running = False
                        break
                if peer.closed and running:
                    screen.print_line(format_system("Соединение разорвано"))
                    running = False

            # --- клавиатура ---
            if running:
                if reader.fd is None:
                    events = reader.read()
                elif reader.fd in ready:
                    events = reader.read()
                else:
                    events = []

                for event in events:
                    if event[0] == "eof":
                        running = False
                        break
                    ch = event[1]

                    if esc_pending:  # остаток escape-последовательности
                        if ch.isalpha() or ch == "~":
                            esc_pending = False
                        continue
                    if ch == "\x1b":
                        esc_pending = True
                        continue

                    if ch in ("\r", "\n"):
                        text = screen.buffer.strip()
                        screen.buffer = ""
                        if text:
                            if text in ("/quit", "/exit", "/выход"):
                                running = False
                                break
                            screen.print_line(
                                format_message(my_name, text, mine=True)
                            )
                            peer.send({"t": "msg", "text": text})
                            peer.send({"t": "stop_typing"})
                            last_typing_sent = 0.0
                        need_render = True
                        continue

                    if ch in ("\x7f", "\b"):  # Backspace
                        if screen.buffer:
                            screen.buffer = screen.buffer[:-1]
                            need_render = True
                            if not screen.buffer:
                                peer.send({"t": "stop_typing"})
                        continue

                    if ch == "\x15":  # Ctrl+U — очистить строку
                        if screen.buffer:
                            screen.buffer = ""
                            peer.send({"t": "stop_typing"})
                            need_render = True
                        continue

                    if ch == "\x0c":  # Ctrl+L — очистить экран
                        sys.stdout.write("\033[2J\033[H")
                        need_render = True
                        continue

                    if ch in ("\x03", "\x04"):  # Ctrl+C / Ctrl+D
                        running = False
                        break

                    if ch < " ":
                        continue

                    screen.buffer += ch
                    need_render = True
                    now = time.monotonic()
                    if now - last_typing_sent >= TYPING_SEND_INTERVAL:
                        peer.send({"t": "typing"})
                        last_typing_sent = now

            # --- индикатор «печатает…» истёк ---
            if (
                running
                and typing_until
                and time.monotonic() > typing_until
            ):
                typing_until = 0.0
                if screen.peer_typing:
                    screen.peer_typing = False
                    need_render = True

            if running and need_render:
                screen.render()
                need_render = False

    except KeyboardInterrupt:
        pass
    finally:
        try:
            reader.restore()
        except Exception:
            pass
        peer.send({"t": "bye"})
        screen.print_line(format_system("Вы вышли из чата"))


def run_session(peer, my_name, peer_name, pending):
    reader = make_key_source() if Style.enabled else None
    if reader is None:
        run_plain(peer, my_name, peer_name, pending)
    else:
        run_interactive(peer, my_name, peer_name, pending, reader)


# ---------------------------------------------------------------------------
# Командная строка и запуск
# ---------------------------------------------------------------------------

def default_name():
    try:
        return getpass.getuser()[:32]
    except Exception:
        return "Пользователь"


def check_port(port):
    if not (1 <= port <= 65535):
        raise SystemExit("Некорректный порт: %d (нужно 1–65535)" % port)
    return port


def build_parser():
    parser = argparse.ArgumentParser(
        prog="chat.py",
        description="Общение по локальной сети в терминале (без сторонних модулей).",
        epilog="Без аргументов запускается мастер настройки.",
    )
    sub = parser.add_subparsers(dest="mode", metavar="{host,join}")

    p_host = sub.add_parser("host", help="ждать подключения (этот компьютер — хост)")
    p_host.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help="порт (по умолчанию %d)" % DEFAULT_PORT,
    )
    p_host.add_argument("-n", "--name", help="ваше имя")

    p_join = sub.add_parser("join", help="подключиться к другому компьютеру")
    p_join.add_argument("address", help="IP-адрес или имя компьютера-хоста")
    p_join.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help="порт (по умолчанию %d)" % DEFAULT_PORT,
    )
    p_join.add_argument("-n", "--name", help="ваше имя")
    return parser


def interactive_setup():
    """Мастер для запуска без аргументов."""
    print_banner()
    print()
    try:
        name = input("Ваше имя [%s]: " % default_name()).strip()
        if not name:
            name = default_name()
        print()
        print("  1 — ждать подключения (этот компьютер — хост)")
        print("  2 — подключиться к другому компьютеру")
        choice = input("Выберите 1 или 2 [1]: ").strip() or "1"

        if choice == "2":
            address = input("IP-адрес компьютера-хоста: ").strip()
            if not address:
                print("Адрес не указан.")
                return None
            port = input("Порт [%d]: " % DEFAULT_PORT).strip() or str(DEFAULT_PORT)
            return ["join", address, "-p", port, "-n", name]

        port = input("Порт [%d]: " % DEFAULT_PORT).strip() or str(DEFAULT_PORT)
        return ["host", "-p", port, "-n", name]
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def main(argv=None):
    configure_streams()

    if argv is None:
        argv = sys.argv[1:]

    via_wizard = False
    if not argv:
        if not sys.stdin.isatty():
            print("Запустите: python chat.py host")
            print("или:      python chat.py join <IP>")
            return 1
        argv = interactive_setup()
        if argv is None:
            return 0
        via_wizard = True

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "mode", None) is None:
        parser.print_help()
        return 1

    my_name = sanitize(args.name or default_name()).strip()[:32] or "Я"

    # --- установка соединения ---
    if args.mode == "host":
        port = check_port(args.port)
        if not via_wizard:
            print_banner()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # На Windows SO_REUSEADDR позволяет занять уже используемый порт —
            # лучше показать честную ошибку «порт занят».
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", port))
            server.listen(1)
        except OSError as exc:
            print("Не удалось запустить сервер: %s" % exc)
            print("Возможно, порт %d уже занят. Попробуйте: -p %d" % (port, port + 1))
            server.close()
            return 1

        print()
        print("Ваше имя: %s" % my_name)
        print("Порт:     %d" % port)
        ips = local_ips()
        print()
        if ips:
            print("IP-адрес этого компьютера в локальной сети:")
            for ip in ips:
                print("    %s" % ip)
            print()
            print("На втором компьютере запустите:")
            print("    python chat.py join %s -p %d" % (ips[0], port))
        else:
            print("Не удалось определить IP — узнайте его командой ipconfig.")
        print()
        print("Ожидание подключения… (Ctrl+C — отмена)")
        print("Если не подключается — разрешите Python в брандмауэре Windows.")
        print()
        try:
            conn, addr = server.accept()
        except KeyboardInterrupt:
            print("\nОжидание отменено.")
            return 0
        finally:
            server.close()
        print("Подключился: %s:%d" % (addr[0], addr[1]))

    else:  # join
        address = args.address
        port = check_port(args.port)
        if not via_wizard:
            print_banner()
        print("Подключение к %s:%d …" % (address, port))
        try:
            conn = socket.create_connection((address, port), timeout=10)
        except OSError as exc:
            print("Не удалось подключиться: %s" % exc)
            print("Проверьте адрес, порт и брандмауэр на компьютере-хосте.")
            return 1
        conn.settimeout(None)
        print("Соединение установлено.")

    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    peer = Peer(conn)
    try:
        try:
            peer_name, pending = handshake(peer, my_name)
        except (ConnectionError, OSError) as exc:
            print("Ошибка: %s" % exc)
            return 1

        Style.enabled = setup_terminal()
        run_session(peer, my_name, peer_name, pending)
    finally:
        peer.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nВыход.")
        sys.exit(0)
