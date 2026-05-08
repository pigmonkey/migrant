#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13", "httpx>=0.27"]
# ///
"""
netcheck.py — Network connectivity probe for migrant VMs.

Usage: uv run netcheck.py [options]
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import traceback as _traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, TypeVar

import httpx
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Result:
    status: Literal["PASS", "FAIL", "SKIP", "INFO"]
    message: str
    detail: str | None = None


@dataclass
class TestCase:
    category: str
    description: str
    fn: Callable[["Context"], Result]
    # None = INFO test; no expected outcome, never affects exit code
    expect: Literal["PASS", "FAIL"] | None = None


@dataclass
class Context:
    args: argparse.Namespace
    gateway: str
    resolvers: list[str]
    vm_ips: dict[str, list[str]]


@dataclass
class ToolResult:
    available: bool
    path: str | None = None
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

TESTS: list[TestCase] = []

_F = TypeVar("_F", bound="Callable[[Context], Result]")


def test(
    category: str,
    description: str,
    *,
    expect: Literal["PASS", "FAIL"] | None = "PASS",
) -> Callable[[_F], _F]:
    """Decorator that registers a test function."""
    def decorator(fn: _F) -> _F:
        TESTS.append(TestCase(
            category=category,
            description=description,
            fn=fn,
            expect=expect,
        ))
        return fn
    return decorator


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_host_port(spec: str) -> tuple[str, int]:
    """Parse 'tcp/9999' or 'udp/9999' into (proto, port).

    Raises ValueError on invalid input.
    """
    parts = spec.split("/", 1)
    if len(parts) != 2 or parts[0] not in ("tcp", "udp"):
        raise ValueError(
            f"Invalid host-port spec {spec!r}: expected tcp/<port> or udp/<port>"
        )
    try:
        port = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid port {parts[1]!r}: must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError(f"Port {port} out of range 1–65535")
    return parts[0], port


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Network connectivity probe for migrant VMs"
    )
    p.add_argument("--lan-ip", metavar="IP",
                   help="LAN host IP to probe (skips interactive prompt)")
    p.add_argument("--host-port", metavar="SPEC",
                   help="Host port, e.g. tcp/9999 (skips interactive prompt)")
    p.add_argument("--peer-ip", metavar="IP",
                   help="Another migrant VM IP to probe (skips interactive prompt)")
    p.add_argument("--no-interactive", action="store_true",
                   help="Skip all interactive prompts; run only automatic tests")
    p.add_argument("--check-tools", action="store_true",
                   help="List required external tools and their availability; exit")
    return p


# ---------------------------------------------------------------------------
# Context detection
# ---------------------------------------------------------------------------

def parse_resolvers(resolv_conf: str) -> list[str]:
    """Return nameserver IPs from resolv.conf content."""
    result = []
    for line in resolv_conf.splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2:
                result.append(parts[1])
    return result


def parse_gateway(route_output: str) -> str | None:
    """Extract default IPv4 gateway from 'ip -4 route show' output."""
    for line in route_output.splitlines():
        parts = line.split()
        if parts and parts[0] == "default":
            try:
                idx = parts.index("via")
                return parts[idx + 1]
            except (ValueError, IndexError):
                pass
    return None


def parse_vm_ips(ip_addr_output: str) -> dict[str, list[str]]:
    """Parse interface → list[IP] from 'ip addr show' output."""
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in ip_addr_output.splitlines():
        m = re.match(r"^\d+:\s+(\S+):", line)
        if m:
            # strip @peer suffix (e.g. "veth0@if5")
            current = m.group(1).split("@")[0]
            result[current] = []
        elif current:
            m = re.match(r"^\s+inet6?\s+([^\s/]+)", line)
            if m:
                result[current].append(m.group(1))
    return result


def detect_context(args: argparse.Namespace) -> Context:
    resolv_path = Path("/etc/resolv.conf")
    resolv = resolv_path.read_text(errors="replace") if resolv_path.exists() else ""
    resolvers = parse_resolvers(resolv)

    route_out = subprocess.run(
        ["ip", "-4", "route", "show"], capture_output=True, text=True
    ).stdout
    gateway = parse_gateway(route_out) or "192.168.200.1"

    addr_out = subprocess.run(
        ["ip", "addr", "show"], capture_output=True, text=True
    ).stdout
    vm_ips = parse_vm_ips(addr_out)

    return Context(args=args, gateway=gateway, resolvers=resolvers, vm_ips=vm_ips)


# ---------------------------------------------------------------------------
# Tool helper
# ---------------------------------------------------------------------------

# Fallback aliases tried when the primary binary is not found.
_TOOL_ALIASES: dict[str, list[str]] = {
    "traceroute": ["tracepath"],
    "tracepath":  ["traceroute"],
}


def find_tool(binary: str) -> str | None:
    """Return the path to `binary` or a known alias, or None."""
    if path := shutil.which(binary):
        return path
    for alias in _TOOL_ALIASES.get(binary, []):
        if path := shutil.which(alias):
            return path
    return None


def run_tool(binary: str, args: list[str], timeout: int = 5) -> ToolResult:
    """Run an external tool, returning a ToolResult.

    Returns ToolResult(available=False) if the binary (and all aliases) are
    missing. Timeouts are captured rather than raised.
    """
    path = find_tool(binary)
    if not path:
        return ToolResult(available=False)
    try:
        proc = subprocess.run(
            [path, *args], capture_output=True, text=True, timeout=timeout
        )
        return ToolResult(
            available=True, path=path,
            stdout=proc.stdout, stderr=proc.stderr,
            returncode=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(available=True, path=path, stderr="(timeout)", returncode=-1)


# ---------------------------------------------------------------------------
# --check-tools mode
# ---------------------------------------------------------------------------

# (binary, purpose, arch-pkg, debian-pkg)
_TOOLS: list[tuple[str, str, str, str]] = [
    ("ping",       "ICMP probes (IPv4 and IPv6), MTU",  "inetutils",      "iputils-ping"),
    ("traceroute", "Hop-by-hop path tracing",            "traceroute",     "traceroute"),
    ("tracepath",  "Fallback for traceroute",            "(iputils)",      "iputils-tracepath"),
    ("dig",        "DNS queries with flag inspection",   "bind-tools",     "dnsutils"),
    ("nc",         "TCP/UDP port connectivity test",     "openbsd-netcat", "netcat-openbsd"),
]


def _detect_distro() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                return line[3:].strip().strip('"').lower()
    except FileNotFoundError:
        pass
    return "unknown"


def cmd_check_tools() -> None:
    distro = _detect_distro()
    t = Table(title="External tool availability", show_header=True, header_style="bold")
    t.add_column("Tool")
    t.add_column("Purpose")
    t.add_column("Status")
    t.add_column("Install")

    for binary, purpose, arch_pkg, deb_pkg in _TOOLS:
        path = find_tool(binary)
        if path:
            status = f"[green]present[/green]  {path}"
            install = ""
        else:
            status = "[red]missing[/red]"
            if distro == "arch":
                install = f"pacman -S {arch_pkg}"
            elif distro in ("debian", "ubuntu"):
                install = f"apt install {deb_pkg}"
            else:
                install = f"arch: {arch_pkg} / debian: {deb_pkg}"
        t.add_row(binary, purpose, status, install)

    console.print(t)


def _warn_missing_tools() -> None:
    """Emit a one-line warning if any tools are absent."""
    missing = [b for b, *_ in _TOOLS if not find_tool(b)]
    if missing:
        console.print(
            f"[yellow]Warning:[/yellow] missing tools (affected tests will SKIP): "
            + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def gather_inputs(args: argparse.Namespace) -> argparse.Namespace:
    """Prompt for optional inputs not supplied via CLI. Returns updated args."""
    import copy
    args = copy.copy(args)

    console.print("\n[bold]Optional test inputs[/bold] (press Enter to skip)\n")

    if args.lan_ip is None:
        val = console.input("  LAN host IP to probe (for allow-lan-host test): ").strip()
        args.lan_ip = val or None

    if args.host_port is None:
        val = console.input(
            "  Host port to probe, e.g. tcp/9999"
            " (you will be asked to run nc on the host first): "
        ).strip()
        if val:
            try:
                parse_host_port(val)
                args.host_port = val
            except ValueError as exc:
                console.print(f"  [yellow]Skipping host-port test: {exc}[/yellow]")
                args.host_port = None

    if args.peer_ip is None:
        val = console.input(
            "  Another migrant VM IP (for peer isolation test): "
        ).strip()
        args.peer_ip = val or None

    if args.host_port:
        proto, port = parse_host_port(args.host_port)
        nc_cmd = f"nc -u -l {port}" if proto == "udp" else f"nc -l {port}"
        console.print(
            f"\n  [yellow]Host-port test: run `{nc_cmd}` on the host now.[/yellow]"
        )
        console.input("  Press Enter when the listener is ready: ")

    console.print()
    return args


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _format_result(tc: TestCase, result: Result) -> tuple[str, str]:
    """Return (rich badge, effective_status).

    effective_status is 'PASS' or 'FAIL' for exit-code purposes.
    INFO/SKIP tests always return effective_status='PASS' (no effect on exit code).
    """
    s = result.status

    if s == "SKIP":
        return "[yellow]SKIP[/yellow]", "PASS"
    if s == "INFO" or tc.expect is None:
        return "[blue]INFO[/blue]", "PASS"

    if tc.expect == "PASS":
        if s == "PASS":
            return "[green]✓ PASS[/green]", "PASS"
        return "[red]✗ FAIL[/red]", "FAIL"

    # tc.expect == "FAIL"
    if s == "FAIL":
        return "[green]✓ blocked (expected)[/green]", "PASS"
    return "[red bold]✗ UNEXPECTED — connection succeeded[/red bold]", "FAIL"


def run_all(ctx: Context) -> list[tuple[TestCase, Result]]:
    results: list[tuple[TestCase, Result]] = []
    last_category = ""

    for tc in TESTS:
        if tc.category != last_category:
            console.rule(f"[bold]{tc.category}[/bold]")
            last_category = tc.category

        try:
            result = tc.fn(ctx)
        except Exception as exc:
            result = Result(
                "FAIL",
                f"unhandled exception: {exc}",
                detail=_traceback.format_exc(),
            )

        badge, _ = _format_result(tc, result)
        console.print(f"  {badge}  {tc.description}")
        console.print(f"       [dim]{result.message}[/dim]")
        if result.detail:
            console.print(f"       [dim]{result.detail[:400]}[/dim]")

        results.append((tc, result))

    return results


def print_summary(results: list[tuple[TestCase, Result]]) -> None:
    t = Table(title="\nSummary", show_header=True, header_style="bold")
    t.add_column("Category")
    t.add_column("Test")
    t.add_column("Result")
    t.add_column("Message", no_wrap=False)

    for tc, result in results:
        badge, _ = _format_result(tc, result)
        t.add_row(tc.category, tc.description, badge, result.message)

    console.print(t)


def compute_exit_code(results: list[tuple[TestCase, Result]]) -> int:
    for tc, result in results:
        _, effective = _format_result(tc, result)
        if effective == "FAIL":
            return 1
    return 0


# ---------------------------------------------------------------------------
# Tests: 1. Inventory
# ---------------------------------------------------------------------------

@test("Inventory", "Interfaces and IPs", expect=None)
def _inv_interfaces(ctx: Context) -> Result:
    # Use 'ip addr show' full output as detail so MACs are visible
    out = subprocess.run(["ip", "addr", "show"], capture_output=True, text=True).stdout
    # One-line summary: non-loopback interfaces and their IPs
    lines = [
        f"{iface}: {', '.join(ips) or '(no addresses)'}"
        for iface, ips in ctx.vm_ips.items()
        if iface != "lo"
    ]
    return Result(
        "INFO",
        "; ".join(lines) or "(no non-loopback interfaces)",
        detail=out.strip(),
    )


@test("Inventory", "IPv4 routing table", expect=None)
def _inv_routes_v4(ctx: Context) -> Result:
    out = subprocess.run(["ip", "-4", "route", "show"], capture_output=True, text=True).stdout
    return Result("INFO", "(see detail)", detail=out.strip() or "(empty)")


@test("Inventory", "IPv6 routing table", expect=None)
def _inv_routes_v6(ctx: Context) -> Result:
    out = subprocess.run(["ip", "-6", "route", "show"], capture_output=True, text=True).stdout
    return Result("INFO", "(see detail)", detail=out.strip() or "(empty)")


@test("Inventory", "DNS resolvers", expect=None)
def _inv_resolvers(ctx: Context) -> Result:
    return Result(
        "INFO",
        ", ".join(ctx.resolvers) if ctx.resolvers else "(none found in /etc/resolv.conf)",
    )


@test("Inventory", "Detected gateway", expect=None)
def _inv_gateway(ctx: Context) -> Result:
    return Result("INFO", ctx.gateway)


@test("Inventory", "ARP / neighbor table", expect=None)
def _inv_arp(ctx: Context) -> Result:
    out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True).stdout
    entries = [ln for ln in out.splitlines() if ln.strip()]
    n = len(entries)
    return Result("INFO", f"{n} neighbor {'entry' if n == 1 else 'entries'}", detail=out.strip() or "(empty)")


# ---------------------------------------------------------------------------
# Tests: 2. DNS
# ---------------------------------------------------------------------------

def _dig_query(target: str, server: str, qtype: str = "A", timeout: int = 8) -> dict:
    """Run dig and return a dict with keys: ips (set[str]), ad (bool), raw (str), error (str)."""
    r = run_tool("dig", [f"+time={timeout - 1}", qtype, target, f"@{server}"], timeout=timeout)
    if not r.available:
        return {"ips": set(), "ad": False, "raw": "", "error": "dig not found"}
    if r.returncode != 0:
        # Extract SERVFAIL/REFUSED status from output if present, else use stderr
        status_m = re.search(r"status:\s*(\w+)", r.stdout)
        error_msg = status_m.group(1) if status_m else (r.stderr.strip()[:120] or f"exit {r.returncode}")
        return {"ips": set(), "ad": False, "raw": r.stdout or r.stderr, "error": error_msg}
    raw = r.stdout
    # Parse 'ad' flag: look for "flags: ... ad ..." before the first semicolon after "flags:"
    ad = False
    if m := re.search(r"flags:([^;]+);", raw):
        ad = bool(re.search(r'\bad\b', m.group(1)))
    # Parse answer IPs from the ANSWER SECTION
    ips: set[str] = set()
    if "ANSWER SECTION" in raw:
        answer_block = raw.split("ANSWER SECTION")[1].split(";;")[0]
        ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", answer_block))
    return {"ips": ips, "ad": ad, "raw": raw, "error": ""}


@test("DNS", "Resolve via default resolver (A record)", expect="PASS")
def _dns_default_a(ctx: Context) -> Result:
    resolver = ctx.resolvers[0] if ctx.resolvers else ctx.gateway
    d = _dig_query("cloudflare.com", resolver, "A")
    if d["error"] == "dig not found":
        return Result("SKIP", "dig not found")
    if not d["ips"]:
        return Result("FAIL", f"no A record returned: {d['error'] or '(empty answer)'}")
    return Result("PASS", f"{', '.join(sorted(d['ips']))}")


@test("DNS", "Resolve via default resolver (AAAA record)", expect="PASS")
def _dns_default_aaaa(ctx: Context) -> Result:
    """Query travels over IPv4; tests AAAA record resolution, not IPv6 connectivity."""
    resolver = ctx.resolvers[0] if ctx.resolvers else ctx.gateway
    d = _dig_query("cloudflare.com", resolver, qtype="AAAA", timeout=9)
    if d["error"] == "dig not found":
        return Result("SKIP", "dig not found")
    if not d["raw"]:
        return Result("SKIP", "no response from resolver")
    if "ANSWER SECTION" not in d["raw"]:
        # NOERROR with no AAAA records is valid in IPv4-only environments
        status_m = re.search(r"status:\s*(\w+)", d["raw"])
        status = status_m.group(1) if status_m else "unknown"
        return Result("SKIP", f"no AAAA answer (status: {status})")
    # Parse valid IPv6 addresses from the answer block
    answer_block = d["raw"].split("ANSWER SECTION")[1].split(";;")[0]
    candidates = re.findall(r"[0-9a-fA-F:]+", answer_block)
    aaaa_addrs = []
    for c in candidates:
        try:
            addr = ipaddress.ip_address(c)
            if isinstance(addr, ipaddress.IPv6Address):
                aaaa_addrs.append(str(addr))
        except ValueError:
            pass
    if not aaaa_addrs:
        return Result("FAIL", "ANSWER SECTION present but no valid IPv6 addresses parsed")
    return Result("PASS", aaaa_addrs[0])


@test("DNS", "Direct query to 8.8.8.8", expect="PASS")
def _dns_direct_8888(ctx: Context) -> Result:
    """Under WireGuard+DNS this query is DNATed to the VPN resolver."""
    d = _dig_query("cloudflare.com", "8.8.8.8")
    if d["error"] == "dig not found":
        return Result("SKIP", "dig not found")
    if not d["ips"]:
        return Result("FAIL", f"no answer from 8.8.8.8: {d['error']}")
    ad_note = " [ad]" if d["ad"] else " [no ad]"
    return Result("PASS", f"{', '.join(sorted(d['ips']))}{ad_note}")


@test("DNS", "Direct query to 1.1.1.1", expect="PASS")
def _dns_direct_1111(ctx: Context) -> Result:
    """Under WireGuard+DNS this query is DNATed to the VPN resolver."""
    d = _dig_query("cloudflare.com", "1.1.1.1")
    if d["error"] == "dig not found":
        return Result("SKIP", "dig not found")
    if not d["ips"]:
        return Result("FAIL", f"no answer from 1.1.1.1: {d['error']}")
    ad_note = " [ad]" if d["ad"] else " [no ad]"
    return Result("PASS", f"{', '.join(sorted(d['ips']))}{ad_note}")


@test("DNS", "DNS interception detection", expect=None)
def _dns_interception(ctx: Context) -> Result:
    """Compare answers and 'ad' flag across resolvers to detect DNAT redirection.

    Absence of discrepancy does NOT rule out interception — the VPN resolver
    may return identical answers.
    """
    resolver = ctx.resolvers[0] if ctx.resolvers else ctx.gateway
    default = _dig_query("cloudflare.com", resolver)
    g = _dig_query("cloudflare.com", "8.8.8.8")
    cf = _dig_query("cloudflare.com", "1.1.1.1")

    findings: list[str] = []
    if g["ips"] and g["ips"] != default["ips"]:
        findings.append("8.8.8.8 returns different IPs than default resolver")
    if cf["ips"] and cf["ips"] != default["ips"]:
        findings.append("1.1.1.1 returns different IPs than default resolver")
    if g["ips"] and g["ad"] != default["ad"]:
        findings.append(
            f"'ad' flag differs vs 8.8.8.8 "
            f"(default: {default['ad']}, direct: {g['ad']})"
        )
    if cf["ips"] and cf["ad"] != default["ad"]:
        findings.append(
            f"'ad' flag differs vs 1.1.1.1 "
            f"(default: {default['ad']}, direct: {cf['ad']})"
        )

    if findings:
        msg = "Interception likely: " + "; ".join(findings)
    else:
        msg = (
            "No discrepancy detected "
            "(interception not ruled out — VPN resolver may return identical answers)"
        )
    return Result("INFO", msg)


@test("DNS", "Resolver source IP (whoami)", expect=None)
def _dns_whoami(ctx: Context) -> Result:
    """Reports the IP address that DNS queries appear to originate from."""
    resolver = ctx.resolvers[0] if ctx.resolvers else ctx.gateway
    r = run_tool(
        "dig", ["+short", "+time=7", "TXT", "o-o.myaddr.l.google.com", f"@{resolver}"],
        timeout=9,
    )
    if not r.available:
        return Result("SKIP", "dig not found")
    ip = r.stdout.strip().strip('"')
    if not ip:
        return Result("INFO", "no response (resolver may not support this query)")
    return Result("INFO", f"DNS queries appear to originate from: {ip}")


# ---------------------------------------------------------------------------
# Network probe helpers
# ---------------------------------------------------------------------------

def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> str:
    """Return 'CONNECTED', 'REJECTED', 'TIMEOUT', or 'NOROUTE'."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
        return "CONNECTED"
    except ConnectionRefusedError:
        return "REJECTED"
    except socket.timeout:
        return "TIMEOUT"
    except OSError as exc:
        if exc.errno == 111:   # ECONNREFUSED (some platforms)
            return "REJECTED"
        if exc.errno == 113:   # EHOSTUNREACH — kernel has no route; distinct from a DROP timeout
            return "NOROUTE"
        return f"ERROR:{exc.errno}"


# ---------------------------------------------------------------------------
# Tests: 3. Internet connectivity
# ---------------------------------------------------------------------------

# uv downloads a standalone CPython whose OpenSSL doesn't load system CAs by
# default.  Explicitly load the system bundle so HTTPS tests work correctly.
_SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if _SYSTEM_CA_BUNDLE.exists():
        ctx.load_verify_locations(cafile=str(_SYSTEM_CA_BUNDLE))
    return ctx


@test("Internet", "HTTP reachability (IPv4)", expect="PASS")
def _inet_http(ctx: Context) -> Result:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get("http://example.com")
        return Result("PASS", f"HTTP {resp.status_code}")
    except Exception as exc:
        return Result("FAIL", str(exc)[:120])


@test("Internet", "HTTPS reachability (IPv4)", expect="PASS")
def _inet_https(ctx: Context) -> Result:
    try:
        with httpx.Client(verify=_ssl_ctx(), timeout=10) as client:
            resp = client.get("https://am.i.mullvad.net/")
        return Result("PASS", f"HTTPS {resp.status_code}")
    except Exception as exc:
        return Result("FAIL", str(exc)[:120])


@test("Internet", "IPv6 external reachability", expect="FAIL")
def _inet_ipv6(ctx: Context) -> Result:
    """Always blocked at FORWARD regardless of NETWORK_ISOLATION setting.
    A PASS result here is a security finding.
    """
    has_global_v6 = any(
        ip != "::1" and ":" in ip and not ip.startswith("fe80")
        for ips in ctx.vm_ips.values()
        for ip in ips
    )
    if not has_global_v6:
        return Result("FAIL", "no global IPv6 address assigned (expected — IPv6 not configured)")
    target_ip = "2606:4700:4700::1111"  # Cloudflare IPv6 DNS
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect((target_ip, 53))
        return Result(
            "PASS",
            f"Connected to [{target_ip}]:53 — UNEXPECTED; verify ip6tables FORWARD rule",
        )
    except socket.timeout:
        return Result("FAIL", "timeout (blocked at FORWARD or no route — expected)")
    except OSError as exc:
        return Result("FAIL", f"blocked (expected): {exc}")


@test("Internet", "Public IP / tunnel info", expect=None)
def _inet_public_ip(ctx: Context) -> Result:
    """Queries am.i.mullvad.net for IP, location, ISP, and Mullvad exit status."""
    try:
        with httpx.Client(verify=_ssl_ctx(), timeout=15) as client:
            resp = client.get("https://am.i.mullvad.net/json")
        data = resp.json()
        ip = data.get("ip", "unknown")
        city = data.get("city", "")
        country = data.get("country", "")
        org = data.get("organization", "")
        mullvad = data.get("mullvad_exit_ip", False)
        location = ", ".join(filter(None, [city, country]))
        suffix = "  [Mullvad exit ✓]" if mullvad else ""
        return Result(
            "INFO",
            f"{ip} — {org} — {location}{suffix}",
            detail=resp.text,
        )
    except Exception as exc:
        return Result("INFO", f"could not reach am.i.mullvad.net: {exc}")


@test("Internet", "Traceroute to 8.8.8.8 (first 5 hops)", expect=None)
def _inet_traceroute(ctx: Context) -> Result:
    # traceroute and tracepath have incompatible flags; try each with its own args.
    if shutil.which("traceroute"):
        r = run_tool("traceroute", ["-4", "-m", "5", "-w", "2", "8.8.8.8"], timeout=40)
    elif shutil.which("tracepath"):
        r = run_tool("tracepath", ["-4", "-m", "5", "8.8.8.8"], timeout=40)
    else:
        return Result("SKIP", "neither traceroute nor tracepath found")
    return Result("INFO", "(see detail)", detail=r.stdout.strip() or r.stderr.strip())


@test("Internet", "MTU probe (1400-byte IP packet, don't-fragment)", expect="PASS")
def _inet_mtu(ctx: Context) -> Result:
    """Send a 1400-byte IP packet (1372-byte payload + 28-byte IP/ICMP header).
    WireGuard lowers effective MTU to ~1420, so a 1400-byte IP packet should pass.
    """
    r = run_tool("ping", ["-M", "do", "-s", "1372", "-c", "1", "-W", "5", "8.8.8.8"], timeout=10)
    if not r.available:
        return Result("SKIP", "ping not found")
    if r.returncode == 0:
        return Result("PASS", "1400-byte IP don't-fragment packet transmitted successfully")
    output = (r.stdout + r.stderr).strip()
    return Result("FAIL", f"MTU issue detected: {output[:120]}")


@test("Internet", "ICMP echo to 8.8.8.8", expect=None)
def _inet_icmp(ctx: Context) -> Result:
    """Dedicated ICMP reachability probe, distinct from the MTU test.
    REACHABLE indicates ICMP FORWARD is open to the internet — a viable
    covert channel if other exfiltration paths are blocked.
    """
    r = run_tool("ping", ["-c", "1", "-W", "5", "8.8.8.8"], timeout=10)
    if not r.available:
        return Result("SKIP", "ping not found")
    if r.returncode == 0:
        return Result("INFO", "REACHABLE — ICMP FORWARD open to internet")
    output = (r.stdout + r.stderr).strip()
    return Result("INFO", f"BLOCKED — {output[:120]}")


# ---------------------------------------------------------------------------
# Tests: 4. Isolation
# ---------------------------------------------------------------------------

_ISOLATION_NOTES = {
    "CONNECTED": "NI not active (or this path is explicitly allowed)",
    "REJECTED":  "iptables rule confirmed (expected under NETWORK_ISOLATION=true)",
    "TIMEOUT":   "ambiguous — no host at this IP, or silent DROP rule",
    "NOROUTE":   "kernel has no route to this host (unusual in a migrant VM with a default route)",
}


@test("Isolation", "Ping libvirt gateway (192.168.200.1)", expect=None)
def _iso_ping_gateway(ctx: Context) -> Result:
    """Under NI, ICMP hits the INPUT REJECT rule — only DHCP/DNS match LIBVIRT_INP."""
    r = run_tool("ping", ["-c", "1", "-W", "3", ctx.gateway], timeout=8)
    if not r.available:
        return Result("SKIP", "ping not found")
    if r.returncode == 0:
        return Result("INFO", "REACHABLE — NI not active")
    return Result("INFO", "BLOCKED — NI INPUT chain active (ICMP rejected, expected)")


@test("Isolation", "TCP to gateway port 80", expect=None)
def _iso_tcp_gateway_80(ctx: Context) -> Result:
    outcome = _tcp_probe(ctx.gateway, 80)
    return Result("INFO", f"{outcome} — {_ISOLATION_NOTES.get(outcome, outcome)}")


@test("Isolation", "TCP to gateway high port (19999)", expect=None)
def _iso_tcp_gateway_high(ctx: Context) -> Result:
    outcome = _tcp_probe(ctx.gateway, 19999)
    return Result("INFO", f"{outcome} — {_ISOLATION_NOTES.get(outcome, outcome)}")


@test("Isolation", "RFC1918 probe — 10.0.0.1:80", expect=None)
def _iso_rfc1918_10(ctx: Context) -> Result:
    outcome = _tcp_probe("10.0.0.1", 80)
    return Result("INFO", f"{outcome} — {_ISOLATION_NOTES.get(outcome, outcome)}")


@test("Isolation", "RFC1918 probe — 172.16.0.1:80", expect=None)
def _iso_rfc1918_172(ctx: Context) -> Result:
    outcome = _tcp_probe("172.16.0.1", 80)
    return Result("INFO", f"{outcome} — {_ISOLATION_NOTES.get(outcome, outcome)}")


@test("Isolation", "RFC1918 probe — 192.168.1.1:80", expect=None)
def _iso_rfc1918_192(ctx: Context) -> Result:
    outcome = _tcp_probe("192.168.1.1", 80)
    return Result("INFO", f"{outcome} — {_ISOLATION_NOTES.get(outcome, outcome)}")


@test("Isolation", "IPv6 external reachability", expect="FAIL")
def _iso_ipv6_external(ctx: Context) -> Result:
    """Always blocked at ip6tables FORWARD regardless of NETWORK_ISOLATION setting.
    A PASS result here is a security finding.
    Note: this test also appears in the Internet section; it is included here
    explicitly as an isolation verification.
    """
    has_global_v6 = any(
        ip != "::1" and ":" in ip and not ip.startswith("fe80")
        for ips in ctx.vm_ips.values()
        for ip in ips
    )
    if not has_global_v6:
        return Result("FAIL", "no global IPv6 address assigned (expected — IPv6 not configured)")
    target_ip = "2606:4700:4700::1111"  # Cloudflare IPv6 DNS
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect((target_ip, 53))
        return Result(
            "PASS",
            f"Connected to [{target_ip}]:53 — UNEXPECTED; verify ip6tables FORWARD rule",
        )
    except socket.timeout:
        return Result("FAIL", "timeout (blocked at FORWARD or no route — expected)")
    except OSError as exc:
        return Result("FAIL", f"blocked (expected): {exc}")


_GATEWAY_SURVEY_PORTS: list[tuple[int, str]] = [
    (22,   "SSH"),
    (53,   "DNS"),
    (80,   "HTTP"),
    (111,  "rpcbind"),
    (443,  "HTTPS"),
    (8080, "HTTP-alt"),
    (8443, "HTTPS-alt"),
    (9090, "misc"),
]


@test("Isolation", "Gateway port survey", expect=None)
def _iso_gateway_ports(ctx: Context) -> Result:
    """Probe common ports on the libvirt gateway (the host's bridge interface).
    CONNECTED means the host has a listening service reachable from the VM.
    REJECTED means traffic reached the host but no service answered.
    All TIMEOUT indicates the INPUT chain is rejecting or dropping everything.
    """
    rows: list[str] = []
    reachable: list[str] = []
    for port, name in _GATEWAY_SURVEY_PORTS:
        outcome = _tcp_probe(ctx.gateway, port, timeout=2.0)
        rows.append(f"{port:5d} ({name:<12}): {outcome}")
        if outcome in ("CONNECTED", "REJECTED"):
            reachable.append(f"{port}/{name}:{outcome}")
    summary = (
        f"reachable: {', '.join(reachable)}"
        if reachable
        else "no ports reachable (all TIMEOUT — INPUT chain active)"
    )
    return Result("INFO", summary, detail="\n".join(rows))


def _primary_iface(vm_ips: dict[str, list[str]]) -> str | None:
    """Return the first non-loopback, non-virtual interface name."""
    skip_prefixes = ("lo", "wg", "tun", "tap", "virbr", "docker", "veth")
    for iface in vm_ips:
        if not any(iface.startswith(p) for p in skip_prefixes):
            return iface
    return None


@test("Isolation", "IPv6 link-local gateway reachability", expect=None)
def _iso_ipv6_linklocal(ctx: Context) -> Result:
    """Probe the host's link-local (fe80::) IPv6 address on the VM's bridge segment.
    Link-local traffic does not traverse the FORWARD chain — it arrives at the
    host's INPUT chain.  Reachability here means the host's ip6tables INPUT
    rules govern exposure, not the FORWARD isolation rules.
    """
    iface = _primary_iface(ctx.vm_ips)
    if not iface:
        return Result("INFO", "no non-loopback interface found")

    # Ping the all-nodes multicast address to populate the neighbor cache.
    run_tool("ping", ["-6", "-c", "2", "-W", "2", f"ff02::1%{iface}"], timeout=8)

    neigh_out = subprocess.run(
        ["ip", "-6", "neigh", "show"], capture_output=True, text=True
    ).stdout
    fe80_neighbors: list[str] = []
    for ln in neigh_out.splitlines():
        m = re.match(r"^(fe80:[^\s]+)", ln)
        if m and "FAILED" not in ln:
            fe80_neighbors.append(m.group(1))

    if not fe80_neighbors:
        return Result(
            "INFO",
            "no fe80 neighbors discovered (neighbor cache empty)",
            detail=neigh_out.strip() or "(empty)",
        )

    findings: list[str] = []
    for addr in fe80_neighbors:
        try:
            scope_id = socket.if_nametoindex(iface)
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect((addr, 22, 0, scope_id))
            findings.append(f"{addr} :22 CONNECTED")
        except ConnectionRefusedError:
            findings.append(f"{addr} :22 REJECTED (host reachable, no SSH listener)")
        except socket.timeout:
            findings.append(f"{addr} :22 TIMEOUT")
        except OSError as exc:
            findings.append(f"{addr} :22 ERROR:{exc.errno}")

    return Result("INFO", "; ".join(findings), detail=neigh_out.strip())


# ---------------------------------------------------------------------------
# Tests: 5. Exfiltration paths
# ---------------------------------------------------------------------------

@test("Exfiltration paths", "DNS over TCP to 8.8.8.8", expect=None)
def _exfil_dns_tcp(ctx: Context) -> Result:
    """UDP/53 outbound is DNAT'd by the hook; TCP/53 may follow a different
    iptables codepath.  If TCP/53 reaches an external resolver unintercepted,
    DNS tunneling over TCP is viable regardless of the UDP DNAT rule.
    """
    r = run_tool("dig", ["+tcp", "+time=7", "cloudflare.com", "@8.8.8.8"], timeout=10)
    if not r.available:
        return Result("SKIP", "dig not found")
    ips: set[str] = set()
    if "ANSWER SECTION" in r.stdout:
        answer_block = r.stdout.split("ANSWER SECTION")[1].split(";;")[0]
        ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", answer_block))
    if r.returncode != 0 or not ips:
        status_m = re.search(r"status:\s*(\w+)", r.stdout)
        status = status_m.group(1) if status_m else (r.stderr.strip()[:80] or f"exit {r.returncode}")
        return Result("INFO", f"BLOCKED/FAILED — {status}")
    ad = False
    if m := re.search(r"flags:([^;]+);", r.stdout):
        ad = bool(re.search(r'\bad\b', m.group(1)))
    ad_note = " [ad]" if ad else " [no ad]"
    return Result(
        "INFO",
        f"RESOLVED via TCP/53 — {', '.join(sorted(ips))}{ad_note} "
        "(confirm whether DNAT rule covers TCP/53)",
    )


@test("Exfiltration paths", "UDP/123 NTP probe", expect=None)
def _exfil_ntp(ctx: Context) -> Result:
    """NTP (UDP/123) is commonly permitted and rarely monitored — a viable
    covert-channel carrier.  Sends a minimal NTP client request to Cloudflare
    NTP (162.159.200.1) and checks whether a response arrives.
    """
    packet = b'\x1b' + b'\x00' * 47  # LI=0, VN=3, Mode=3 (client request)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(packet, ("162.159.200.1", 123))
            data, addr = sock.recvfrom(1024)
        if len(data) >= 48:
            tx_secs = struct.unpack("!I", data[40:44])[0]
            dt = datetime.fromtimestamp(tx_secs - 2208988800, tz=timezone.utc)
            return Result(
                "INFO",
                f"REACHABLE — server time {dt.isoformat()} "
                "(UDP/123 is an open exfiltration path)",
            )
        return Result(
            "INFO", f"REACHABLE — short response ({len(data)} bytes) from {addr[0]}"
        )
    except socket.timeout:
        return Result("INFO", "TIMEOUT — UDP/123 appears blocked or filtered")
    except OSError as exc:
        return Result("INFO", f"BLOCKED — {exc}")


_EXFIL_TCP_PORTS: list[tuple[int, str]] = [
    (22,   "SSH"),
    (25,   "SMTP"),
    (465,  "SMTPS"),
    (587,  "submission"),
    (853,  "DoT"),
    (8080, "HTTP-alt"),
    (8443, "HTTPS-alt"),
    (9418, "git"),
]


@test("Exfiltration paths", "Outbound TCP port survey", expect=None)
def _exfil_tcp_survey(ctx: Context) -> Result:
    """Probe non-standard outbound TCP ports against 1.1.1.1 (stable, well-known target).
    CONNECTED or REJECTED both mean traffic reached 1.1.1.1 — the port is unblocked
    at FORWARD (REJECTED means no service listens at that port on 1.1.1.1).
    TIMEOUT indicates a DROP rule is in the path.
    HTTP (80) and HTTPS (443) are covered by the Internet section.
    """
    rows: list[str] = []
    unblocked: list[str] = []
    for port, name in _EXFIL_TCP_PORTS:
        outcome = _tcp_probe("1.1.1.1", port, timeout=3.0)
        rows.append(f"{port:5d} ({name:<12}): {outcome}")
        if outcome in ("CONNECTED", "REJECTED"):
            unblocked.append(f"{port}/{name}")
    if unblocked:
        summary = f"unblocked (reached 1.1.1.1): {', '.join(unblocked)}"
    else:
        summary = "all TIMEOUT — outbound TCP ports appear fully blocked"
    return Result("INFO", summary, detail="\n".join(rows))


# ---------------------------------------------------------------------------
# Tests: 6. Host access
# ---------------------------------------------------------------------------

@test("Host access", "Connect to host port", expect="PASS")
def _host_port(ctx: Context) -> Result:
    if not ctx.args.host_port:
        return Result("SKIP", "no --host-port specified (skipped)")
    try:
        proto, port = parse_host_port(ctx.args.host_port)
    except ValueError as exc:
        return Result("SKIP", str(exc))

    if proto == "tcp":
        outcome = _tcp_probe(ctx.gateway, port, timeout=5.0)
        if outcome == "CONNECTED":
            return Result("PASS", f"TCP connected to {ctx.gateway}:{port}")
        return Result(
            "FAIL",
            f"{outcome} — verify allow-host-port {proto}/{port} is set in Migrantfile",
        )

    # UDP: fire-and-forget; confirm no immediate OS error
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            sock.sendto(b"netcheck", (ctx.gateway, port))
        return Result("PASS", f"UDP datagram sent to {ctx.gateway}:{port} without error")
    except OSError as exc:
        return Result("FAIL", f"UDP send failed: {exc}")


# ---------------------------------------------------------------------------
# Tests: 7. LAN and peer
# ---------------------------------------------------------------------------

@test("LAN / peer", "Ping LAN host", expect="PASS")
def _lan_ping(ctx: Context) -> Result:
    if not ctx.args.lan_ip:
        return Result("SKIP", "no --lan-ip specified (skipped)")
    r = run_tool("ping", ["-c", "1", "-W", "5", ctx.args.lan_ip], timeout=10)
    if not r.available:
        return Result("SKIP", "ping not found")
    if r.returncode == 0:
        return Result("PASS", f"ping to {ctx.args.lan_ip} succeeded")
    return Result(
        "FAIL",
        f"ping to {ctx.args.lan_ip} failed — verify allow-lan-host is set in Migrantfile",
    )


@test("LAN / peer", "TCP connect to LAN host (port 80)", expect="PASS")
def _lan_tcp(ctx: Context) -> Result:
    if not ctx.args.lan_ip:
        return Result("SKIP", "no --lan-ip specified (skipped)")
    outcome = _tcp_probe(ctx.args.lan_ip, 80, timeout=5.0)
    if outcome == "CONNECTED":
        return Result("PASS", f"TCP to {ctx.args.lan_ip}:80 succeeded")
    return Result(
        "FAIL",
        f"{outcome} to {ctx.args.lan_ip}:80 — verify allow-lan-host in Migrantfile",
    )


@test("LAN / peer", "Ping peer migrant VM", expect="FAIL")
def _lan_peer_ping(ctx: Context) -> Result:
    """The migrant subnet (192.168.200.0/24) is RFC1918; blocked at FORWARD under NI."""
    if not ctx.args.peer_ip:
        return Result("SKIP", "no --peer-ip specified (skipped)")
    r = run_tool("ping", ["-c", "1", "-W", "3", ctx.args.peer_ip], timeout=8)
    if not r.available:
        return Result("SKIP", "ping not found")
    if r.returncode != 0:
        return Result(
            "FAIL",
            f"ping to peer {ctx.args.peer_ip} failed (expected — NI blocks migrant subnet)",
        )
    return Result(
        "PASS",
        f"ping to peer {ctx.args.peer_ip} SUCCEEDED — unexpected under NETWORK_ISOLATION=true",
    )


def main() -> None:
    # argparse exits with code 2 automatically on bad arguments.
    args = build_parser().parse_args()

    if args.check_tools:
        cmd_check_tools()
        return

    # Validate --host-port early so we fail before prompting.
    if args.host_port:
        try:
            parse_host_port(args.host_port)
        except ValueError as exc:
            sys.stderr.write(f"Error: --host-port: {exc}\n")
            sys.exit(2)

    _warn_missing_tools()

    try:
        ctx = detect_context(args)
    except Exception as exc:
        sys.stderr.write(f"Setup error: {exc}\n")
        sys.exit(2)

    if not args.no_interactive:
        args = gather_inputs(args)
        ctx = Context(
            args=args,
            gateway=ctx.gateway,
            resolvers=ctx.resolvers,
            vm_ips=ctx.vm_ips,
        )

    results = run_all(ctx)
    print_summary(results)
    sys.exit(compute_exit_code(results))


if __name__ == "__main__":
    main()
