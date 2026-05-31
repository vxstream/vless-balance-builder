#!/usr/bin/env python3
"""
║
║           VLESS Config Builder for Xray/V2Ray                   
║                                                                  
║  Собирает VLESS-конфиги из нескольких подписок,                 
║  генерирует единый JSON с балансировкой нагрузки.               
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
VERSION = "1.2.0"
DEFAULT_OUTPUT = "main.json"
REQUEST_TIMEOUT = 15          # секунды
REQUEST_RETRIES = 3
RETRY_DELAY    = 2            # секунды между повторами

# ─── ANSI-цвета (отключаются автоматически если не TTY) ─────────────────────
USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)


# ════════════════════════════════════════════════════════════════════════════
#   ЗАГРУЗКА ПОДПИСОК
# ════════════════════════════════════════════════════════════════════════════

def _fetch_raw(url: str) -> str:
    """
    Скачивает текст по URL.  
    Повторяет попытку REQUEST_RETRIES раз при сетевых ошибках.
    """
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
    """
    Если содержимое закодировано в Base64 — декодирует и возвращает строку.
    Определяем по наличию символов перевода строки внутри одного «блока»
    и отсутствию «vless://» / «#» в сыром тексте.
    """
    stripped = text.strip()

    # Быстрая проверка: если явно видны VLESS-ссылки — декодировать не нужно
    if "vless://" in stripped:
        return stripped

    # Пробуем декодировать Base64
    # Подписки бывают стандартным b64 и url-safe b64
    for variant in (stripped, stripped.replace("-", "+").replace("_", "/")):
        # Добавляем паддинг если нужно
        padded = variant + "=" * (-len(variant) % 4)
        try:
            decoded = base64.b64decode(padded).decode("utf-8", errors="strict")
            if "vless://" in decoded:
                return decoded
        except Exception:
            continue

    # Декодировать не удалось или не нужно — возвращаем как есть
    return stripped


def load_subscription(url: str) -> list[str]:
    """
    Скачивает подписку и возвращает список строк.
    Автоматически определяет Base64-кодирование.
    """
    raw = _fetch_raw(url)
    text = _decode_if_base64(raw)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines


# ════════════════════════════════════════════════════════════════════════════
#   ПАРСЕР VLESS-ССЫЛОК
# ════════════════════════════════════════════════════════════════════════════

def _safe_split_address(after_at: str) -> tuple[str, int]:
    """
    Разбирает «host:port» или «[ipv6]:port».
    Возвращает (address, port).
    """
    if after_at.startswith("["):          # IPv6: [::1]:443
        bracket_end = after_at.index("]")
        address = after_at[1:bracket_end]
        rest = after_at[bracket_end + 1:]
        port = int(rest.lstrip(":")) if ":" in rest else 443
    elif after_at.count(":") == 1:        # IPv4 / domain: host:port
        address, port_str = after_at.split(":")
        port = int(port_str)
    else:                                 # без порта
        address = after_at
        port = 443
    return address, port


def parse_vless_link(link: str, index: int) -> dict | None:
    """
    Разбирает одну VLESS-ссылку и возвращает outbound-объект для Xray.
    При ошибке возвращает None.
    """
    try:
        parsed = urlparse(link)

        # UUID — часть между «vless://» и «@»
        body = link[len("vless://"):]
        uuid = unquote(body.split("@")[0])

        # Хост и порт
        after_at = body.split("@")[1].split("?")[0]
        address, port = _safe_split_address(after_at)

        params = parse_qs(parsed.query)

        def p(key: str, default: str = "") -> str:
            return params.get(key, [default])[0]

        encryption  = p("encryption", "none")
        flow        = p("flow")
        security    = p("security",   "tls")
        sni         = p("sni",        address)
        fp          = p("fp",         "chrome")
        path        = p("path",       "/")
        host        = p("host",       sni)
        net_type    = p("type",       "tcp")
        pbk         = p("pbk")
        sid         = p("sid")
        spiderx     = p("spx",        "/")
        service_name = p("serviceName")

        tag = f"vless-{index}"

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

        # ── Пользователь ──────────────────────────────────────────────────
        user: dict = {
            "id":         uuid,
            "encryption": encryption,
            "level":      0,
        }
        if flow:
            user["flow"] = flow

        return {
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

    except Exception as exc:
        preview = link[:72] + ("…" if len(link) > 72 else "")
        print(yellow(f"   ⚠  Пропущена ссылка: {preview}"))
        print(dim(f"      Причина: {exc}"))
        return None


# ════════════════════════════════════════════════════════════════════════════
#   СБОРКА КОНФИГА
# ════════════════════════════════════════════════════════════════════════════

def build_config(outbounds: list[dict]) -> dict:
    """Собирает итоговый конфиг Xray из списка outbound-объектов."""
    return {
        "log": {"loglevel": "warning"},

        "inbounds": [
            {
                "tag":      "socks-in",
                "listen":   "127.0.0.1",
                "port":     10808,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled":     True,
                    "destOverride": ["tls", "http", "quic"],
                },
            },
            {
                "tag":      "http-in",
                "listen":   "127.0.0.1",
                "port":     10809,
                "protocol": "http",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled":     True,
                    "destOverride": ["tls", "http", "quic"],
                },
            },
        ],

        "outbounds": outbounds + [
            {"tag": "direct", "protocol": "freedom"},
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
                {
                    "type":        "field",
                    "ip":          ["geoip:private"],
                    "outboundTag": "direct",
                },
                {
                    "type":        "field",
                    "network":     "tcp,udp",
                    "balancerTag": "vless-balancer",
                },
            ],
        },

        "burstObservatory": {
            "subjectSelector": ["vless-"],
            "pingConfig": {
                "destination":   "http://www.google.com/generate_204",
                "interval":      "10s",
                "timeout":       "8s",
                "burstSize":     5,
                "burstInterval": "800ms",
            },
        },
    }


# ════════════════════════════════════════════════════════════════════════════
#   CLI
# ════════════════════════════════════════════════════════════════════════════

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vless-builder",
        description=(
            "Собирает Xray/V2Ray JSON-конфиг из одной или нескольких VLESS-подписок.\n"
            "Поддерживает plain-text и Base64-кодированные подписки."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
примеры:
  # Одна подписка (URL)
  python vless_builder.py -s https://example.com/sub

  # Несколько подписок
  python vless_builder.py -s https://sub1.example.com/vless https://sub2.example.com/vless

  # Подписки из файла (по одному URL на строку)
  python vless_builder.py -f subscriptions.txt

  # Совмещение + своё имя файла
  python vless_builder.py -f subscriptions.txt -s https://extra.example.com/vless -o config.json
""",
    )
    parser.add_argument(
        "-s", "--subscriptions",
        metavar="URL",
        nargs="+",
        default=[],
        help="URL-адреса подписок (через пробел).",
    )
    parser.add_argument(
        "-f", "--file",
        metavar="FILE",
        help="Текстовый файл со списком URL подписок (по одному на строку).",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        default=DEFAULT_OUTPUT,
        help=f"Имя выходного JSON-файла (по умолчанию: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Не удалять дублирующиеся VLESS-ссылки.",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def collect_urls(args: argparse.Namespace) -> list[str]:
    """Собирает все URL из аргументов командной строки и файла."""
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


# ════════════════════════════════════════════════════════════════════════════
#   ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # ── Шапка ────────────────────────────────────────────────────────────
    print(bold(cyan(f"\n  VLESS Config Builder  v{VERSION}")))
    print(dim("  ─────────────────────────────────\n"))

    # ── Сбор URL ─────────────────────────────────────────────────────────
    urls = collect_urls(args)
    print(bold(f"📡 Подписок к обработке: {len(urls)}"))

    # ── Скачивание и парсинг ─────────────────────────────────────────────
    seen_links:  set[str]  = set()
    all_outbounds: list[dict] = []
    total_raw  = 0
    total_skip = 0

    for sub_idx, url in enumerate(urls, 1):
        short_url = url if len(url) <= 60 else url[:57] + "…"
        print(f"\n  [{sub_idx}/{len(urls)}] {cyan(short_url)}")

        try:
            lines = load_subscription(url)
        except Exception as exc:
            print(red(f"   ✗ Не удалось загрузить подписку: {exc}"))
            continue

        vless_lines = [ln for ln in lines if ln.startswith("vless://")]
        print(dim(f"   Найдено VLESS-ссылок: {len(vless_lines)}"))
        total_raw += len(vless_lines)

        added = 0
        for line in vless_lines:
            if not args.no_dedup:
                if line in seen_links:
                    total_skip += 1
                    continue
                seen_links.add(line)

            outbound = parse_vless_link(line, len(all_outbounds) + 1)
            if outbound:
                all_outbounds.append(outbound)
                added += 1

        status = green(f"+{added}") if added else yellow("0")
        print(f"   Добавлено: {status}")

    # ── Итог сбора ───────────────────────────────────────────────────────
    print(f"\n{'─' * 48}")
    print(bold(f"  Всего найдено : {total_raw}"))
    if not args.no_dedup:
        print(dim(f"  Дубликатов   : {total_skip}"))
    print(bold(f"  Уникальных   : {len(all_outbounds)}"))
    print(f"{'─' * 48}")

    if not all_outbounds:
        sys.exit(red("\n❌ Не найдено ни одной рабочей VLESS-ссылки. Конфиг не создан."))

    # ── Генерация JSON ───────────────────────────────────────────────────
    config = build_config(all_outbounds)
    output_path = Path(args.output)

    try:
        output_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        sys.exit(red(f"\n❌ Не удалось записать файл: {exc}"))

    # ── Финальное сообщение ──────────────────────────────────────────────
    print(green(f"\nКонфиг успешно сохранён: {bold(str(output_path))}"))
    print(dim(f"   Серверов в балансере : {len(all_outbounds)}"))
    print(dim(f"   SOCKS5               : 127.0.0.1:10808"))
    print(dim(f"   HTTP proxy           : 127.0.0.1:10809"))
    print()


if __name__ == "__main__":
    main()
