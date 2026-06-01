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
VERSION = "1.3.0"
DEFAULT_OUTPUT = "main.json"
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 3
RETRY_DELAY     = 2

# ─── ANSI-цвета ─────────────────────────────────────────────────────────────
USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)
def magenta(t): return _c("35", t)


# ════════════════════════════════════════════════════════════════════════════
#   УТИЛИТЫ ДЛЯ РАБОТЫ С РЕМАРКАМИ / ФЛАГАМИ
# ════════════════════════════════════════════════════════════════════════════

def extract_remark(link: str) -> str:
    """
    Извлекает ремарку из VLESS-ссылки.
    Ремарка — это фрагмент после «#» в URL.
    Возвращает пустую строку, если ремарки нет.

    Пример:
        vless://uuid@host:443?...#🇩🇪 DE | Server 1
        → '🇩🇪 DE | Server 1'
    """
    if "#" not in link:
        return ""
    return unquote(link.split("#", 1)[1]).strip()


def extract_flags(remark: str) -> list[str]:
    """
    Извлекает «флаги» из ремарки.

    Флагом считается любое слово или эмодзи-последовательность,
    разделённые пробелами, «|», «-», «_».
    Возвращает список флагов в нижнем регистре (для сравнения без учёта регистра).

    Примеры:
        '🇩🇪 DE | Server 1'  → ['🇩🇪', 'de', 'server', '1']
        'NL-Premium-Fast'   → ['nl', 'premium', 'fast']
        '🇺🇸 US 01'         → ['🇺🇸', 'us', '01']
    """
    import re
    # Разбиваем по разделителям: пробел, |, -, _
    parts = re.split(r"[\s|_\-]+", remark)
    return [p.lower() for p in parts if p]


def remark_matches_flags(remark: str, flags: list[str]) -> bool:
    """
    Проверяет, содержит ли ремарка хотя бы один из указанных флагов.
    Сравнение без учёта регистра.
    Флаг может быть подстрокой ремарки.

    Пример:
        remark='🇩🇪 DE | Berlin', flags=['de', 'nl']  → True
        remark='🇺🇸 US | NY',     flags=['de', 'nl']  → False
    """
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
    """
    Фильтрует список VLESS-ссылок по правилам флагов/ремарок.

    Параметры
    ─────────
    require_remark  — если True, пропускать ссылки БЕЗ ремарки.
    include_flags   — если задан, оставлять ТОЛЬКО ссылки, чья ремарка
                      содержит хотя бы один флаг из списка.
    exclude_flags   — если задан, отбрасывать ссылки, чья ремарка
                      содержит хотя бы один флаг из списка.

    Возвращает (отфильтрованный_список, кол-во_отброшенных).
    """
    result: list[str] = []
    dropped = 0

    for link in links:
        remark = extract_remark(link)

        # 1. Нужна ли ремарка вообще?
        if require_remark and not remark:
            dropped += 1
            continue

        # 2. Белый список флагов (include)
        if include_flags:
            if not remark:
                # Нет ремарки — не можем проверить, пропускаем
                dropped += 1
                continue
            if not remark_matches_flags(remark, include_flags):
                dropped += 1
                continue

        # 3. Чёрный список флагов (exclude)
        if exclude_flags and remark:
            if remark_matches_flags(remark, exclude_flags):
                dropped += 1
                continue

        result.append(link)

    return result, dropped


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
    """
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
    """Разбирает «host:port» или «[ipv6]:port»."""
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


def parse_vless_link(link: str, index: int) -> dict | None:
    """
    Разбирает одну VLESS-ссылку и возвращает outbound-объект для Xray.
    При ошибке возвращает None.
    """
    try:
        # Убираем ремарку перед парсингом параметров
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
        fp           = p("fp",         "chrome")
        path         = p("path",       "/")
        host         = p("host",       sni)
        net_type     = p("type",       "tcp")
        pbk          = p("pbk")
        sid          = p("sid")
        spiderx      = p("spx",        "/")
        service_name = p("serviceName")

        # Ремарка в тег не идёт, но можно добавить как комментарий
        remark = extract_remark(link)
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

        outbound = {
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

        # Сохраняем ремарку как метаданные (Xray игнорирует неизвестные поля)
        if remark:
            outbound["_remark"] = remark

        return outbound

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

    # Убираем служебное поле _remark перед записью в JSON
    clean_outbounds = []
    for ob in outbounds:
        ob_copy = {k: v for k, v in ob.items() if k != "_remark"}
        clean_outbounds.append(ob_copy)

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
                    "enabled":      True,
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
                    "enabled":      True,
                    "destOverride": ["tls", "http", "quic"],
                },
            },
        ],

        "outbounds": clean_outbounds + [
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
                "destination":   "https://t.me/",
                "interval":      "30s",
                "timeout":       "15s",
                "burstSize":     10,
                "burstInterval": "1000ms",
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
  # Одна подписка
  python vless_builder.py -s https://example.com/sub

  # Несколько подписок
  python vless_builder.py -s https://sub1.example.com/vless https://sub2.example.com/vless

  # Подписки из файла
  python vless_builder.py -f subscriptions.txt

  # Только конфиги с ремаркой
  python vless_builder.py -s https://example.com/sub --require-remark

  # Только конфиги с флагами DE или NL в ремарке
  python vless_builder.py -s https://example.com/sub --include-flags DE NL

  # Все конфиги КРОМЕ имеющих флаги RU или IR в ремарке
  python vless_builder.py -s https://example.com/sub --exclude-flags RU IR

  # Совмещение: только DE/NL, но без Premium
  python vless_builder.py -s https://example.com/sub --include-flags DE NL --exclude-flags Premium

  # Свой файл вывода
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
        help=(
            "Оставлять ТОЛЬКО ссылки, чья ремарка содержит хотя бы один\n"
            "из указанных флагов (регистр не важен).\n"
            "Пример: --include-flags DE NL 🇩🇪"
        ),
    )
    flt.add_argument(
        "--exclude-flags",
        metavar="FLAG",
        nargs="+",
        default=None,
        help=(
            "Отбрасывать ссылки, чья ремарка содержит хотя бы один\n"
            "из указанных флагов (регистр не важен).\n"
            "Пример: --exclude-flags RU IR Premium"
        ),
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


def print_filter_summary(args: argparse.Namespace) -> None:
    """Выводит информацию об активных фильтрах."""
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

    # ── Информация о фильтрах ─────────────────────────────────────────────
    print_filter_summary(args)

    # ── Скачивание и парсинг ─────────────────────────────────────────────
    seen_links:    set[str]   = set()
    all_outbounds: list[dict] = []
    total_raw    = 0
    total_skip   = 0
    total_filtered = 0

    for sub_idx, url in enumerate(urls, 1):
        short_url = url if len(url) <= 60 else url[:57] + "…"
        print(f"\n  [{sub_idx}/{len(urls)}] {cyan(short_url)}")

        try:
            lines = load_subscription(url)
        except Exception as exc:
            print(red(f"   ✗ Не удалось загрузить подписку: {exc}"))
            continue

        vless_lines = [ln for ln in lines if ln.startswith("vless://")]
        print(dim(f"   Найдено VLESS-ссылок  : {len(vless_lines)}"))
        total_raw += len(vless_lines)

        # ── Дедупликация ──────────────────────────────────────────────────
        if not args.no_dedup:
            before_dedup = len(vless_lines)
            unique_lines = []
            for ln in vless_lines:
                if ln not in seen_links:
                    seen_links.add(ln)
                    unique_lines.append(ln)
                else:
                    total_skip += 1
            vless_lines = unique_lines
            dupes = before_dedup - len(vless_lines)
            if dupes:
                print(dim(f"   После дедупликации   : {len(vless_lines)} (-{dupes} дублей)"))

        # ── Фильтрация по флагам ──────────────────────────────────────────
        need_filter = (
            args.require_remark
            or args.include_flags is not None
            or args.exclude_flags is not None
        )

        if need_filter:
            before_filter = len(vless_lines)
            vless_lines, dropped = filter_by_flags(
                vless_lines,
                require_remark=args.require_remark,
                include_flags=args.include_flags,
                exclude_flags=args.exclude_flags,
            )
            total_filtered += dropped
            if dropped:
                print(dim(f"   После фильтра флагов : {len(vless_lines)} (-{dropped} отброшено)"))

        # ── Парсинг ───────────────────────────────────────────────────────
        added = 0
        for line in vless_lines:
            outbound = parse_vless_link(line, len(all_outbounds) + 1)
            if outbound:
                all_outbounds.append(outbound)
                added += 1

        status = green(f"+{added}") if added else yellow("0")
        print(f"   Добавлено            : {status}")

    # ── Итог сбора ───────────────────────────────────────────────────────
    print(f"\n{'─' * 52}")
    print(bold(f"  Всего найдено    : {total_raw}"))
    if not args.no_dedup:
        print(dim(f"  Дубликатов       : {total_skip}"))
    if total_filtered:
        print(dim(f"  Отброшено фильтром: {total_filtered}"))
    print(bold(f"  Принято в конфиг : {len(all_outbounds)}"))
    print(f"{'─' * 52}")

    if not all_outbounds:
        print(red("\n❌ Не найдено ни одной рабочей VLESS-ссылки. Конфиг не создан."))
        exit(0)

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
    print(green(f"\n✅ Конфиг успешно сохранён: {bold(str(output_path))}"))
    print(dim(f"   Серверов в балансере : {len(all_outbounds)}"))
    print(dim(f"   Стратегия балансера  : leastLoad"))
    print(dim(f"   SOCKS5               : 127.0.0.1:10808"))
    print(dim(f"   HTTP proxy           : 127.0.0.1:10809"))
    print()


if __name__ == "__main__":
    main()
