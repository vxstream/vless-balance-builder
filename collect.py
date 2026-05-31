# AI CODE

import requests
import json
from urllib.parse import urlparse, parse_qs, unquote

def parse_vless_link(link, index):
    try:
        parsed = urlparse(link)
        
        # UUID — часть перед @
        uuid = unquote(link.split('://')[1].split('@')[0])
        
        # Адрес и порт после @
        after_at = link.split('@')[1].split('?')[0]
        if ':' in after_at:
            address, port = after_at.split(':')
        else:
            address = after_at
            port = 443

        params = parse_qs(parsed.query)
        
        # VLESS-специфичные параметры
        encryption = params.get('encryption', ['none'])[0]
        flow = params.get('flow', [''])[0]
        security = params.get('security', ['tls'])[0]
        sni = params.get('sni', [address])[0]
        fp = params.get('fp', ['chrome'])[0]
        path = params.get('path', ['/'])[0]
        host = params.get('host', [sni])[0]
        type_ = params.get('type', ['tcp'])[0]

        tag = f"vless-{index}"

        # Формируем streamSettings
        stream_settings = {
            "network": type_,
        }

        # ========== REALITY ==========
        if security == "reality":
            stream_settings["security"] = "reality"
            
            # Извлекаем pbk и sid из URL-параметров
            pbk = params.get('pbk', [''])[0]
            sid = params.get('sid', [''])[0]
            spiderx = params.get('spx', ['/'])[0]
            
            reality_settings = {
                "serverName": sni,
                "fingerprint": fp,
                "publicKey": pbk,
                "shortId": sid,
                "spiderX": spiderx,
                "show": False
            }
            stream_settings["realitySettings"] = reality_settings

        # ========== TLS ==========
        elif security == "tls":
            stream_settings["security"] = "tls"
            stream_settings["tlsSettings"] = {
                "serverName": sni,
                "allowInsecure": False,
                "fingerprint": fp
            }

        # ========== NONE / OTHER ==========
        else:
            stream_settings["security"] = security if security else "none"

        # Настройки транспорта
        if type_ == "ws":
            stream_settings["wsSettings"] = {
                "path": path,
                "headers": {"Host": host}
            }
        elif type_ == "grpc":
            service_name = params.get('serviceName', [''])[0]
            stream_settings["grpcSettings"] = {
                "serviceName": service_name
            }
        elif type_ == "tcp":
            stream_settings["tcpSettings"] = {
                "header": {
                    "type": "http",
                    "request": {
                        "path": [path],
                        "headers": {"Host": [host]}
                    }
                }
            }
        elif type_ == "xhttp":
            stream_settings["xhttpSettings"] = {
                "path": path
            }

        # Формируем пользователя
        user = {
            "id": uuid,
            "encryption": encryption,
            "level": 0
        }
        if flow:
            user["flow"] = flow

        return {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": address,
                        "port": int(port),
                        "users": [user]
                    }
                ]
            },
            "streamSettings": stream_settings
        }
    except Exception as e:
        print(f"⚠️ Ошибка парсинга строки: {link[:60]}... — {e}")
        return None


# ====================== Генерация ======================
url = "https://github.com/terik21/HiddifySubs-VlessKeys/raw/refs/heads/main/WhiteKeys"

print("Скачиваем kizyakbeta6.txt...")
response = requests.get(url)
response.raise_for_status()
lines = response.text.splitlines()

vless_outbounds = []
vless_tags = []

print("Парсим VLESS конфиги...")
for i, line in enumerate(lines, 1):
    line = line.strip()
    if line.startswith("vless://"):
        outbound = parse_vless_link(line, len(vless_outbounds) + 1)
        if outbound:
            vless_outbounds.append(outbound)
            vless_tags.append(outbound["tag"])

print(f"Найдено VLESS-серверов: {len(vless_outbounds)}")

if not vless_outbounds:
    print("❌ Не найдено ни одной VLESS-ссылки!")
    exit(1)

# ==================== Финальный конфиг ====================
config = {
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "socks-in",
      "listen": "127.0.0.1",
      "port": 10808,
      "protocol": "socks",
      "settings": { "auth": "noauth", "udp": True },
      "sniffing": { "enabled": True, "destOverride": ["tls", "http", "quic"] }
    },
    {
      "tag": "http-in",
      "listen": "127.0.0.1",
      "port": 10809,
      "protocol": "http",
      "settings": { "auth": "noauth", "udp": True },
      "sniffing": { "enabled": True, "destOverride": ["tls", "http", "quic"] }
    }
  ],
  "outbounds": vless_outbounds + [
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "balancers": [
      {
        "tag": "vless-balancer",
        "selector": ["vless-"],
        "strategy": { "type": "leastPing" }
      }
    ],
    "rules": [
      {
        "type": "field",
        "ip": ["geoip:private"],
        "outboundTag": "direct"
      },
      {
        "type": "field",
        "network": "tcp,udp",
        "balancerTag": "vless-balancer"
      }
    ]
  },
  "burstObservatory": {
    "subjectSelector": ["vless-"],
    "pingConfig": {
      "destination": "http://www.google.com/generate_204",
      "interval": "10s",
      "timeout": "8s",
      "burstSize": 5,
      "burstInterval": "800ms"
    }
  }
}

with open("main.json", "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print("✅ Готово!")
print(f"Сохранено в: main.json")
print(f"Серверов в балансере: {len(vless_tags)}")
