#!/usr/bin/env python3
"""
║
║           VLESS Config Builder for Xray/V2Ray
║
║  Собирает VLESS-конфиги из нескольких подписок,
║  генерирует единый JSON с балансировкой нагрузки.
║
║  Включает обход РКН/ТСПУ: фрагментация, noise, mux,
║  маскировка под легитимный трафик (браузер, стриминг).
║
║  GitHub: https://github.com/vxstream/vless-balance-builder
║  License: MIT
║
"""

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# ─── Зависимости ────────────────────────────────────────────────────────────

try:
    import requests
except ImportError:
    sys.exit(
        "Не установлен пакет 'requests'.\n"
        "   Установите его командой: pip install requests"
    )

# ─── Константы ──────────────────────────────────────────────────────────────

VERSION         = "2.0.0"
DEFAULT_OUTPUT  = "main.json"
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 3
RETRY_DELAY     = 2

# ── Фрагментация (anti-DPI/ТСПУ) ────────────────────────────────────────────
# tlshello — фрагментирует только TLS Client Hello (рекомендуется)
# 1-3      — первые 1-3 TCP-сегмента
FRAGMENT_ENABLED  = True
FRAGMENT_PACKETS  = "tlshello"
FRAGMENT_LENGTH   = "100-200"
FRAGMENT_INTERVAL = "10-20"

# ── Noise (мусорные пакеты до handshake, сбивает ТСПУ-классификаторы) ───────
# base64-строка произвольного "шума", который клиент шлёт перед TLS
# rand — случайные байты нужной длины (рекомендуется для мобильных сетей)
NOISE_ENABLED = True
NOISE_TYPE    = "rand"       # rand | str | base64
NOISE_PACKET  = "10-20"      # длина в байтах (для rand) или сама строка
NOISE_DELAY   = "5-10"       # задержка в мс между noise-пакетами

# ── Mux (мультиплексирование — снижает количество TLS handshake'ов) ─────────
MUX_ENABLED     = True
MUX_CONCURRENCY = 8          # потоков на одно соединение
MUX_XUDP_CONCURRENCY = 16
MUX_XUDP_PROXY_UDP310 = True # h2mux совместимость

# ── Fingerprint браузера (для маскировки под легитимный HTTPS) ───────────────
# chrome / firefox / safari / ios / android / edge / random
# random — каждый раз новый, затрудняет fingerprinting ТСПУ
DEFAULT_FINGERPRINT = "random"

# ── DNS (используем зарубежный DoH чтобы не словить DNS-блок) ───────────────
DNS_SERVERS = [
    "https://1.1.1.1/dns-query",   # Cloudflare DoH
    "https://8.8.8.8/dns-query",   # Google DoH
    "localhost:7874",               # локальный fallback
]
DNS_QUERY_STRATEGY = "UseIPv4"

# ─── ANSI-цвета ─────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):   return _c("32", t)
def yellow(t):  return _c("33", t)
def red(t):     return _c("31", t)
def cyan(t):    return _c("36", t)
def bold(t):    return _c("1",  t)
def dim(t):     return _c("2",  t)
def magenta(t): return _c("35", t)

# ════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ ДЛЯ РАБОТЫ С РЕМАРКАМИ / ФЛАГАМИ
# ════════════════════════════════════════════════════════════════════════════

def extract_remark(link: str) -> str:
    if "#" not in link:
        return ""
    return unquote(link.split("#", 1)[1]).strip()

def extract_flags(remark: str) -> list[str]:
    import re
    parts = re.split(r"[\s|_\-]+", remark)
    return [p.lower() for p in parts if p]

def remark_matches_flags(remark: str, flags: list[str]) -> bool:
    remark_lower = remark.lower()
    for flag in flags:
        if flag.lower() in remark_lower:
            return True
    return False

def filter_by_flags(
    links: list[str],
    *,
    require_remark: bool = False,
    include_flags:  list[str] | None = None,
    exclude_flags:  list[str] | None = None,
) -> tuple[list[str], int]:
    result: list[str] = []
    dropped = 0

    for link in links:
        remark = extract_remark(link)

        if require_remark and not remark:
            dropped += 1
            continue

        if include_flags:
            if not remark:
                dropped += 1
                continue
            if not remark_matches_flags(remark, include_flags):
                dropped += 1
                continue

        if exclude_flags and remark:
            if remark_matches_flags(remark, exclude_flags):
                dropped += 1
                continue

        result.append(link)

    return result, dropped

# ════════════════════════════════════════════════════════════════════════════
# ЗАГРУЗКА ПОДПИСОК
# ════════════════════════════════════════════════════════════════════════════

def _fetch_raw(url: str) -> str:
    headers = {"User-Agent": f"vless-config-builder/{VERSION}"}
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == REQUEST_RETRIES:
                raise
            print(yellow(f"   ⚠  Попытка {attempt}/{REQUEST_RETRIES} не удалась ({exc}). "
                         f"Повтор через {RETRY_DELAY}с…"))
            time.sleep(RETRY_DELAY)

def _decode_if_base64(text: str) -> str:
    stripped = text.strip()
    if "vless://" in stripped:
        return stripped

    for variant in (stripped, stripped.replace("-", "+").replace("_", "/")):
        padded = variant + "=" * (-len(variant) % 4)
        try:
            decoded = base64.b64decode(padded).decode("utf-8", errors="strict")
            if "vless://" in decoded:
                return decoded
        except Exception:
            continue

    return stripped

def load_subscription(url: str) -> list[str]:
    raw = _fetch_raw(url)
    text = _decode_if_base64(raw)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines

# ════════════════════════════════════════════════════════════════════════════
# ANTI-DPI: ФРАГМЕНТАЦИЯ + NOISE
# ════════════════════════════════════════════════════════════════════════════

def build_sockopt(args: argparse.Namespace | None = None) -> dict:
    """
    Собирает sockopt с фрагментацией и noise.

    Фрагментация tlshello разбивает TLS Client Hello на части —
    ТСПУ видит неполный handshake и не может классифицировать трафик.

    Noise шлёт мусорные пакеты перед handshake, сбивая DPI-классификаторы
    мобильных операторов (МТС/Билайн/Мегафон используют timing + size анализ).
    """
    sockopt: dict = {
        "mark": 255,
        "tcpFastOpen": True,       # быстрое переподключение
        "tcpKeepAliveIdle": 100,
        "tcpNoDelay": True,
    }

    # Фрагментация
    frag_enabled  = FRAGMENT_ENABLED
    frag_packets  = FRAGMENT_PACKETS
    frag_length   = FRAGMENT_LENGTH
    frag_interval = FRAGMENT_INTERVAL

    if args is not None:
        if getattr(args, "no_fragment", False):
            frag_enabled = False
        elif getattr(args, "fragment", False):
            frag_enabled = True
        frag_packets  = getattr(args, "fragment_packets",  frag_packets)
        frag_length   = getattr(args, "fragment_length",   frag_length)
        frag_interval = getattr(args, "fragment_interval", frag_interval)

    if frag_enabled:
        sockopt["fragment"] = {
            "packets":  frag_packets,
            "length":   frag_length,
            "interval": frag_interval,
        }

    # Noise (Xray 1.8.10+)
    noise_enabled = NOISE_ENABLED
    if args is not None:
        if getattr(args, "no_noise", False):
            noise_enabled = False

    if noise_enabled:
        sockopt["noises"] = [
            {
                "type":   NOISE_TYPE,
                "packet": NOISE_PACKET,
                "delay":  NOISE_DELAY,
            }
        ]

    return sockopt


def build_mux() -> dict | None:
    """
    Mux снижает количество TLS handshake'ов — меньше «подозрительных»
    соединений в единицу времени, что снижает вероятность блокировки
    по поведенческому анализу ТСПУ.
    """
    if not MUX_ENABLED:
        return None
    return {
        "enabled":           True,
        "concurrency":       MUX_CONCURRENCY,
        "xudpConcurrency":   MUX_XUDP_CONCURRENCY,
        "xudpProxyUDP310":   MUX_XUDP_PROXY_UDP310,
    }

# ════════════════════════════════════════════════════════════════════════════
# ПАРСЕР VLESS-ССЫЛОК
# ════════════════════════════════════════════════════════════════════════════

def _safe_split_address(after_at: str) -> tuple[str, int]:
    if after_at.startswith("["):
        bracket_end = after_at.index("]")
        address = after_at[1:bracket_end]
        rest = after_at[bracket_end + 1:]
        port = int(rest.lstrip(":")) if ":" in rest else 443
    elif after_at.count(":") == 1:
        address, port_str = after_at.split(":")
        port = int(port_str)
    else:
        address = after_at
        port = 443
    return address, port


def parse_vless_link(link: str, index: int, args: argparse.Namespace | None = None) -> dict | None:
    try:
        link_no_remark = link.split("#")[0]
        parsed = urlparse(link_no_remark)

        body = link_no_remark[len("vless://"):]
        uuid = unquote(body.split("@")[0])

        after_at = body.split("@")[1].split("?")[0]
        address, port = _safe_split_address(after_at)

        params = parse_qs(parsed.query)

        def p(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        encryption   = p("encryption", "none")
        flow         = p("flow")
        security     = p("security",   "tls")
        sni          = p("sni",        address)
        # Fingerprint: берём из ссылки или глобальный дефолт
        fp           = p("fp") or DEFAULT_FINGERPRINT
        path         = p("path",       "/")
        host         = p("host",       sni)
        net_type     = p("type",       "tcp")
        pbk          = p("pbk")
        sid          = p("sid")
        spiderx      = p("spx",        "/")
        service_name = p("serviceName")

        remark = extract_remark(link)
        tag    = f"vless-{index}"

        # ── streamSettings ────────────────────────────────────────────────
        stream: dict = {"network": net_type}

        if security == "reality":
            stream["security"] = "reality"
            stream["realitySettings"] = {
                "serverName":  sni,
                "fingerprint": fp,
                "publicKey":   pbk,
                "shortId":     sid,
                "spiderX":     spiderx,
                "show":        False,
            }
        elif security == "tls":
            stream["security"] = "tls"
            stream["tlsSettings"] = {
                "serverName":    sni,
                "allowInsecure": False,
                "fingerprint":   fp,
                # ALPN: h2,http/1.1 — имитирует обычный браузерный HTTPS
                "alpn":          ["h2", "http/1.1"],
            }
        else:
            stream["security"] = security if security else "none"

        # ── Транспорт ─────────────────────────────────────────────────────
        if net_type == "ws":
            stream["wsSettings"] = {
                "path":    path,
                "headers": {"Host": host},
            }
        elif net_type == "grpc":
            stream["grpcSettings"] = {"serviceName": service_name}
        elif net_type == "tcp":
            header_type = p("headerType", "none")
            if header_type == "http":
                stream["tcpSettings"] = {
                    "header": {
                        "type": "http",
                        "request": {
                            "path":    [path],
                            "headers": {"Host": [host]},
                        },
                    }
                }
        elif net_type == "xhttp":
            stream["xhttpSettings"] = {"path": path}
        elif net_type == "h2":
            stream["httpSettings"] = {
                "path": path,
                "host": [host],
            }
        elif net_type == "quic":
            stream["quicSettings"] = {
                "security": p("quicSecurity", "none"),
                "key":      p("key"),
                "header":   {"type": p("headerType", "none")},
            }
        elif net_type == "kcp":
            stream["kcpSettings"] = {
                "header": {"type": p("headerType", "none")},
                "seed":   p("seed"),
            }

        # ── Фрагментация + Noise ──────────────────────────────────────────
        # Применяем только к TCP/WS — для gRPC и QUIC не нужно
        if net_type not in ("grpc", "quic"):
            stream["sockopt"] = build_sockopt(args)

        # ── Пользователь ──────────────────────────────────────────────────
        user: dict = {
            "id":         uuid,
            "encryption": encryption,
            "level":      0,
        }
        if flow:
            user["flow"] = flow

        outbound: dict = {
            "tag":      tag,
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": address,
                        "port":    port,
                        "users":   [user],
                    }
                ]
            },
            "streamSettings": stream,
        }

        # Mux (не совместим с XTLS flow, пропускаем если есть flow)
        if not flow:
            mux = build_mux()
            if mux:
                outbound["mux"] = mux

        if remark:
            outbound["_remark"] = remark

        return outbound

    except Exception as exc:
        preview = link[:72] + ("…" if len(link) > 72 else "")
        print(yellow(f"   ⚠  Пропущена ссылка: {preview}"))
        print(dim(f"      Причина: {exc}"))
        return None

# ════════════════════════════════════════════════════════════════════════════
# СБОРКА КОНФИГА
# ════════════════════════════════════════════════════════════════════════════

def build_config(outbounds: list[dict]) -> dict:
    """Собирает итоговый конфиг Xray из списка outbound-объектов."""

    clean_outbounds = []
    for ob in outbounds:
        ob_copy = {k: v for k, v in ob.items() if k != "_remark"}
        clean_outbounds.append(ob_copy)

    return {
        "log": {"loglevel": "warning"},

        # ── DNS: DoH через зарубежные резолверы ──────────────────────────
        # Предотвращает DNS-блокировки РКН, не зависит от операторского DNS
        "dns": {
            "queryStrategy": DNS_QUERY_STRATEGY,
            "servers": [
                {
                    # Российские домены резолвим через системный DNS
                    # (чтобы не ломать доступ к локальным ресурсам)
                    "address":  "223.5.5.5",
                    "domains":  ["geosite:ru", "geosite:category-ru"],
                    "skipFallback": True,
                },
                {
                    # Всё остальное — через Cloudflare DoH
                    "address": DNS_SERVERS[0],
                    "domains": ["geosite:geolocation-!cn"],
                },
                # Fallback
                DNS_SERVERS[1],
            ],
            "disableFallbackIfMatch": True,
        },

        "inbounds": [
            {
                "tag":      "socks-in",
                "listen":   "127.0.0.1",
                "port":     10808,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled":      True,
                    "destOverride": ["tls", "http", "quic", "fakedns"],
                    # metadataOnly: False — полный sniffing, нужен для точного роутинга
                    "metadataOnly": False,
                },
            },
            {
                "tag":      "http-in",
                "listen":   "127.0.0.1",
                "port":     10809,
                "protocol": "http",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled":      True,
                    "destOverride": ["tls", "http", "quic", "fakedns"],
                    "metadataOnly": False,
                },
            },
        ],

        "outbounds": clean_outbounds + [
            {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}},
            {"tag": "block",  "protocol": "blackhole"},
        ],

        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "balancers": [
                {
                    "tag":      "vless-balancer",
                    "selector": ["vless-"],
                    "strategy": {"type": "leastPing"},
                }
            ],
            "rules": [
                # Локальные адреса — напрямую
                {
                    "type":        "field",
                    "ip":          ["geoip:private"],
                    "outboundTag": "direct",
                },
                # Российские домены — напрямую (не ломаем банки/госуслуги)
                {
                    "type":        "field",
                    "domain":      ["geosite:ru", "geosite:category-ru"],
                    "outboundTag": "direct",
                },
                # Российские IP — напрямую
                {
                    "type":        "field",
                    "ip":          ["geoip:ru"],
                    "outboundTag": "direct",
                },
                # Весь остальной трафик — через балансировщик VPN
                {
                    "type":        "field",
                    "network":     "tcp,udp",
                    "balancerTag": "vless-balancer",
                },
            ],
        },

        # ── Мониторинг задержек для балансировщика ───────────────────────
        "burstObservatory": {
            "subjectSelector": ["vless-"],
            "pingConfig": {
                "destination":   "https://t.me/",
                "interval":      "30s",
                "timeout":       "15s",
                "burstSize":     10,
                "burstInterval": "1000ms",
            },
        },
    }

# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vless-builder",
        description=(
            "Собирает Xray/V2Ray JSON-конфиг из одной или нескольких VLESS-подписок.\n"
            "Поддерживает plain-text и Base64-кодированные подписки.\n"
            "Включает обход РКН/ТСПУ: фрагментация TLS, noise, mux, DoH DNS."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
примеры:

  Одна подписка
    python vless_builder.py -s https://example.com/sub

  Несколько подписок
    python vless_builder.py -s https://sub1.example.com/vless https://sub2.example.com/vless

  Подписки из файла
    python vless_builder.py -f subscriptions.txt

  Только конфиги DE или NL, без Premium
    python vless_builder.py -s https://example.com/sub --include-flags DE NL --exclude-flags Premium

  Отключить noise (если сервер его не поддерживает)
    python vless_builder.py -s https://example.com/sub --no-noise

  Агрессивная фрагментация для жёстких блокировок
    python vless_builder.py -s https://example.com/sub \\
      --fragment-packets 1-5 --fragment-length 50-100 --fragment-interval 5-15

  Свой файл вывода
    python vless_builder.py -f subscriptions.txt -o config.json
""",
    )

    # ── Источники подписок ────────────────────────────────────────────────
    src = parser.add_argument_group("источники подписок")
    src.add_argument(
        "-s", "--subscriptions",
        metavar="URL",
        nargs="+",
        default=[],
        help="URL-адреса подписок (через пробел).",
    )
    src.add_argument(
        "-f", "--file",
        metavar="FILE",
        help="Текстовый файл со списком URL подписок (по одному на строку).",
    )

    # ── Фильтры по флагам/ремаркам ────────────────────────────────────────
    flt = parser.add_argument_group("фильтры по ремаркам / флагам")
    flt.add_argument(
        "--require-remark",
        action="store_true",
        help="Пропускать ссылки БЕЗ ремарки (фрагмента после «#»).",
    )
    flt.add_argument(
        "--include-flags",
        metavar="FLAG",
        nargs="+",
        default=None,
        help="Оставлять ТОЛЬКО ссылки, чья ремарка содержит хотя бы один флаг.",
    )
    flt.add_argument(
        "--exclude-flags",
        metavar="FLAG",
        nargs="+",
        default=None,
        help="Отбрасывать ссылки, чья ремарка содержит хотя бы один флаг.",
    )

    # ── Anti-DPI / ТСПУ ───────────────────────────────────────────────────
    dpi = parser.add_argument_group("обход РКН/ТСПУ (anti-DPI)")
    dpi.add_argument(
        "--fragment",
        action="store_true",
        help="Принудительно включить фрагментацию TLS (по умолчанию включена).",
    )
    dpi.add_argument(
        "--no-fragment",
        action="store_true",
        help="Отключить фрагментацию.",
    )
    dpi.add_argument(
        "--fragment-packets",
        metavar="MODE",
        default=FRAGMENT_PACKETS,
        help=f"Режим: tlshello | 1-3 | 1-5 (по умолч.: {FRAGMENT_PACKETS}).",
    )
    dpi.add_argument(
        "--fragment-length",
        metavar="RANGE",
        default=FRAGMENT_LENGTH,
        help=f"Размер фрагмента в байтах, напр. 100-200 (по умолч.: {FRAGMENT_LENGTH}).",
    )
    dpi.add_argument(
        "--fragment-interval",
        metavar="RANGE",
        default=FRAGMENT_INTERVAL,
        help=f"Интервал в мс между фрагментами (по умолч.: {FRAGMENT_INTERVAL}).",
    )
    dpi.add_argument(
        "--no-noise",
        action="store_true",
        help="Отключить noise-пакеты (если Xray < 1.8.10).",
    )
    dpi.add_argument(
        "--no-mux",
        action="store_true",
        help="Отключить мультиплексирование (mux).",
    )
    dpi.add_argument(
        "--fingerprint",
        metavar="FP",
        default=DEFAULT_FINGERPRINT,
        choices=["chrome", "firefox", "safari", "ios", "android", "edge", "random"],
        help=f"TLS fingerprint браузера (по умолч.: {DEFAULT_FINGERPRINT}).",
    )

    # ── Прочее ───────────────────────────────────────────────────────────
    misc = parser.add_argument_group("прочие параметры")
    misc.add_argument(
        "-o", "--output",
        metavar="FILE",
        default=DEFAULT_OUTPUT,
        help=f"Имя выходного JSON-файла (по умолчанию: {DEFAULT_OUTPUT}).",
    )
    misc.add_argument(
        "--no-dedup",
        action="store_true",
        help="Не удалять дублирующиеся VLESS-ссылки.",
    )
    misc.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    return parser


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.subscriptions)

    if args.file:
        path = Path(args.file)
        if not path.exists():
            sys.exit(red(f"❌ Файл не найден: {path}"))
        file_urls = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        urls.extend(file_urls)
        print(dim(f"   📄 Загружено {len(file_urls)} URL из файла «{path}»"))

    if not urls:
        sys.exit(
            red("❌ Не указано ни одной подписки.\n") +
            dim("   Используйте -s <URL> или -f <file>. Подробнее: --help")
        )

    return urls


def print_filter_summary(args: argparse.Namespace) -> None:
    active = []

    if args.require_remark:
        active.append(f"  {cyan('•')} Требуется ремарка          : {bold('да')}")

    if args.include_flags:
        flags_str = ", ".join(args.include_flags)
        active.append(f"  {cyan('•')} Включить флаги (OR)        : {bold(flags_str)}")

    if args.exclude_flags:
        flags_str = ", ".join(args.exclude_flags)
        active.append(f"  {cyan('•')} Исключить флаги (OR)       : {bold(flags_str)}")

    if active:
        print(bold("\n🔍 Активные фильтры:"))
        for line in active:
            print(line)
    else:
        print(dim("\n   Фильтры не заданы — берём все VLESS-ссылки."))


def print_dpi_summary(args: argparse.Namespace) -> None:
    frag_on    = not args.no_fragment
    noise_on   = not args.no_noise
    mux_on     = not args.no_mux

    print(bold("\n🛡  Обход РКН/ТСПУ:"))
    print(f"  {cyan('•')} Фрагментация TLS       : "
          f"{bold(green('вкл')) if frag_on else bold(red('выкл'))} "
          f"({args.fragment_packets}, {args.fragment_length}b, {args.fragment_interval}ms)")
    print(f"  {cyan('•')} Noise-пакеты           : "
          f"{bold(green('вкл')) if noise_on else bold(red('выкл'))}")
    print(f"  {cyan('•')} Mux                    : "
          f"{bold(green('вкл')) if mux_on else bold(red('выкл'))}")
    print(f"  {cyan('•')} TLS Fingerprint        : {bold(args.fingerprint)}")
    print(f"  {cyan('•')} DNS                    : {bold('DoH (Cloudflare + Google)')}")

# ════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Применяем fingerprint из CLI к глобальной переменной
    global DEFAULT_FINGERPRINT, MUX_ENABLED
    DEFAULT_FINGERPRINT = args.fingerprint
    if getattr(args, "no_mux", False):
        MUX_ENABLED = False

    # ── Шапка ────────────────────────────────────────────────────────────
    print(bold(cyan(f"\n  VLESS Config Builder  v{VERSION}")))
    print(dim("  ─────────────────────────────────\n"))

    # ── Сбор URL ─────────────────────────────────────────────────────────
    urls = collect_urls(args)
    print(bold(f"📡 Подписок к обработке: {len(urls)}"))

    # ── Фильтры и DPI-сводка ─────────────────────────────────────────────
    print_filter_summary(args)
    print_dpi_summary(args)

    # ── Загрузка подписок ─────────────────────────────────────────────────
    all_links: list[str] = []
    print(bold(f"\n⬇  Загрузка подписок…"))

    for i, url in enumerate(urls, 1):
        short = url[:60] + ("…" if len(url) > 60 else "")
        print(f"  [{i}/{len(urls)}] {dim(short)}", end=" ")
        try:
            links = load_subscription(url)
            vless_links = [ln for ln in links if ln.startswith("vless://")]
            all_links.extend(vless_links)
            print(green(f"✓ {len(vless_links)} конфигов"))
        except Exception as exc:
            print(red(f"✗ ошибка: {exc}"))

    if not all_links:
        sys.exit(red("\n❌ Не получено ни одного VLESS-конфига."))

    print(dim(f"\n   Итого ссылок до фильтрации: {len(all_links)}"))

    # ── Дедупликация ──────────────────────────────────────────────────────
    if not args.no_dedup:
        before = len(all_links)
        all_links = list(dict.fromkeys(all_links))
        dupes = before - len(all_links)
        if dupes:
            print(dim(f"   Удалено дублей: {dupes}"))

    # ── Фильтрация по флагам ──────────────────────────────────────────────
    all_links, dropped = filter_by_flags(
        all_links,
        require_remark = args.require_remark,
        include_flags  = args.include_flags,
        exclude_flags  = args.exclude_flags,
    )
    if dropped:
        print(dim(f"   Отброшено фильтром: {dropped}"))

    print(bold(f"\n✅ Конфигов к сборке: {len(all_links)}"))

    # ── Парсинг ──────────────────────────────────────────────────────────
    print(bold("\n⚙  Парсинг VLESS-ссылок…"))
    outbounds: list[dict] = []
    for idx, link in enumerate(all_links):
        ob = parse_vless_link(link, idx, args)
        if ob:
            outbounds.append(ob)

    if not outbounds:
        print(red("\n❌ Ни одна ссылка не была успешно разобрана."))
        exit(0)

    skipped = len(all_links) - len(outbounds)
    if skipped:
        print(yellow(f"   Пропущено (ошибка парсинга): {skipped}"))

    # ── Сборка и запись ───────────────────────────────────────────────────
    print(bold(f"\n📦 Сборка конфига ({len(outbounds)} outbound'ов)…"))
    config = build_config(outbounds)

    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Итог ──────────────────────────────────────────────────────────────
    size_kb = output_path.stat().st_size / 1024
    print(bold(green(f"\n✓ Конфиг сохранён: {output_path}  ({size_kb:.1f} KB)")))
    print(dim(f"   Outbound'ов: {len(outbounds)}  •  Балансировщик: leastPing"))
    print(dim( "   Запуск:      xray run -c " + str(output_path)))
    print()


if __name__ == "__main__":
    main()
