import http.server
import ipaddress
import platform
import re
import socket
import subprocess
from pathlib import Path
from typing import List, Optional

PORT = 8000
ROOT = Path(__file__).resolve().parent

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

def _is_usable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def _pick_best_ip(candidates: List[str]) -> Optional[str]:
    usable = [ip for ip in candidates if _is_usable_ip(ip)]
    if not usable:
        return None
    # Prefer RFC1918 private addresses (192.168/16, 10/8, 172.16/12),
    # which is what a PS5 on the same LAN will be able to reach.
    for ip in usable:
        try:
            if ipaddress.IPv4Address(ip).is_private:
                return ip
        except ipaddress.AddressValueError:
            continue
    return usable[0]


def _ip_from_udp_socket() -> Optional[str]:
    # Cross-platform (Windows/macOS/Linux): opening a UDP socket and
    # "connecting" does not send any packet, but reveals the source
    # address the OS would use for outbound traffic.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        return ip if _is_usable_ip(ip) else None
    except OSError:
        return None
    finally:
        sock.close()


def _ips_from_ipconfig_windows() -> List[str]:
    try:
        output = subprocess.check_output(
            ["ipconfig"],
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"IPv4[^:]*:\s*([\d.]+)", output)


def _run(cmd: List[str]) -> str:
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _ips_from_macos() -> List[str]:
    ips: List[str] = []
    # Ask the routing table which interface has the default route,
    # then ask that interface for its address.
    route = _run(["route", "-n", "get", "default"])
    match = re.search(r"interface:\s*(\S+)", route)
    interfaces = []
    if match:
        interfaces.append(match.group(1))
    # Common service interfaces as fallback (Wi-Fi, Ethernet, iPhone USB, ...).
    interfaces += ["en0", "en1", "en2", "en3"]
    for iface in interfaces:
        ip = _run(["ipconfig", "getifaddr", iface]).strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            ips.append(ip)
    if not ips:
        ips += _ips_from_ifconfig()
    return ips


def _ips_from_linux() -> List[str]:
    ips: List[str] = []
    # `ip route get` reveals the preferred source address.
    route = _run(["ip", "route", "get", "8.8.8.8"])
    match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", route)
    if match:
        ips.append(match.group(1))
    # `hostname -I` lists all addresses (no DNS lookup).
    for ip in _run(["hostname", "-I"]).split():
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            ips.append(ip)
    # Full enumeration of IPv4 addresses.
    addr = _run(["ip", "-4", "-o", "addr", "show"])
    ips += re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", addr)
    if not ips:
        ips += _ips_from_ifconfig()
    return ips


def _ips_from_ifconfig() -> List[str]:
    # `ifconfig` exists on macOS and most BSD/Linux systems.
    output = _run(["ifconfig"])
    return re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", output)


def _ips_from_hostname_resolve() -> List[str]:
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
        return [ip for ip in addresses if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)]
    except OSError:
        return []


def local_ip():
    # 1. Fast cross-platform method, works on Windows/macOS/Linux.
    ip = _ip_from_udp_socket()
    if ip:
        return ip

    # 2. OS-specific fallbacks (no outbound route / offline network).
    system = platform.system()
    if system == "Windows":
        candidates = _ips_from_ipconfig_windows()
    elif system == "Darwin":
        candidates = _ips_from_macos()
    elif system == "Linux":
        candidates = _ips_from_linux()
    else:  # Other Unix (BSD, etc.)
        candidates = _ips_from_ifconfig()
    best = _pick_best_ip(candidates)
    if best:
        return best

    # 3. Last resort: hostname resolution.
    best = _pick_best_ip(_ips_from_hostname_resolve())
    if best:
        return best

    return "localhost"

if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as server:
        print(f"http://{local_ip()}:{PORT}/")
        server.serve_forever()