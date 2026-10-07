# MIanalOG

Самостоятельно размещаемая карта наблюдаемой ликвидности **Binance BTCUSDT Spot**
и CVD по размеру исполнений. Python + HTML/CSS/Canvas, без платных API,
торговых ключей, Telegram и серверной генерации изображений.
Проект независим от Material Indicators и не воспроизводит их закрытый алгоритм.

## Возможности

- Карта лимитных заявок и цена; режимы 1 день / 1 неделя, автообновление 15 секунд.
- Фильтр объёма в USDT или перцентилях; ценовой шаг 25–2000 USDT,
  диапазон ±0.5–20%. Настройки отдельных периодов независимы до перезагрузки страницы.
- Общий и групповой CVD, абсолютный или нормализованный.
- Мышь, клавиатура, мобильные жесты, перекрестие и лупа.
- Непрерывный сбор публичных depth/aggTrade, архив 14 дней, восстановление
  соединения, очистка и ежедневные локальные резервные копии.

## Требования

Проверенная среда: Ubuntu 24.04, Python 3.12, systemd; Python 3.11+ необходим
для tomllib. Рекомендуемая стартовая конфигурация: 2 vCPU, 2–4 GiB RAM,
не менее 20 GiB свободного диска. Это ориентир, не замер полной 14-дневной
истории; следите за data/ и нагрузкой. При свободном диске менее 3 GiB сбор
останавливается; резервные копии требуют дополнительного свободного места.
Нужен доступ VPS к публичным Binance REST/WebSocket: доступность зависит от региона.
Node.js нужен только для JS-тестов, не для работы сайта.

## Установка на свой VPS

Под root установите зависимости и создайте отдельного пользователя:

```bash
apt update
apt install -y git python3 python3-venv ca-certificates
adduser mianalog
loginctl enable-linger mianalog
```

Используйте путь checkout без пробелов. Откройте **SSH-сессию под mianalog** (чтобы был доступен user systemd).

```bash
git clone https://github.com/94la/MIanalOG.git
cd MIanalOG
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python deploy/install-supervisor.py
```

Установщик определяет путь checkout автоматически, генерирует unit в data/deploy/
и подключает его к user systemd. Не запускайте отдельный collect параллельно:
writer только один. Сбор начинается с момента установки, прошлой карты нет.

Под root установите web service, заменив путь своим:

```bash
python3 /home/mianalog/MIanalOG/deploy/install-web.py --user mianalog
curl --fail http://127.0.0.1:8790/healthz
```

Backend слушает только 127.0.0.1. Установщик web не меняет Caddy и другие сайты.
Если раньше запускали web-start вручную, сначала выполните web-stop под
владельцем проекта. config.toml содержит параметры сбора; при установке используйте
стандартный data_dir="data". Базовый шаг 25 нельзя менять для уже созданного архива.

## HTTPS через Caddy

Установите Caddy по [официальной инструкции](https://caddyserver.com/docs/install).
Создайте DNS A-запись вашего домена на IPv4 VPS (AAAA только при работающем IPv6).
Порты 80 и 443 должны быть доступны извне; 8790 открывать не нужно.
Добавьте блок из deploy/Caddyfile.example в **существующий** Caddyfile,
заменив charts.example.com своим доменом. Сохраните остальные блоки:

```caddyfile
charts.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8790
}
```

Для стандартной установки Caddy под root:

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
```

При нестандартной установке используйте её путь и способ перезагрузки.
Caddy получает TLS-сертификат автоматически. **Сайт открыт всем по ссылке**;
если нужен закрытый доступ, настройте его на уровне reverse proxy.
API только читает архив, файловая система проекта через HTTP не публикуется.

## Управление и диагностика

Под владельцем checkout, из его папки:

```bash
.venv/bin/python -m mtanalog status
.venv/bin/python -m mtanalog stop
.venv/bin/python -m mtanalog start
.venv/bin/python -m mtanalog backup
systemctl --user status mtanalog-supervisor.service
tail -n 50 data/collector.log
```

Web под root: `systemctl status mtanalog-web.service` и
`journalctl -u mtanalog-web.service -n 50`. /healthz проверяет web;
/readyz возвращает 503 при остановке/отставании сборщика.
`verify` и `prune` запускайте только после остановки сборщика.
Обновление: остановите supervisor, git pull --ff-only, установите requirements.lock.txt,
запустите supervisor; перезапустите web service под root. Не удаляйте data/.

## Как понимать данные

Начальный стакан ограничен 5000 уровнями с каждой стороны; дальше видны
наблюдаемые обновления, а не гарантированно полный стакан. Пунктир обозначает
границы известного полного диапазона. После потери depth выполняется новая
синхронизация; неизвестная история остаётся серой. Восстановить её текущим
снимком невозможно. Исполнения по возможности восполняются REST aggTrades.

Цвет — средний за время USDT-объём наблюдаемых заявок после ценовой агрегации,
не сумма всех сообщений. Низкие значения скрываются только при отображении.
CVD группирует aggTrade по размеру исполнения, **не отслеживает кошельки
и капитал конкретных участников**. Нормализация каждой линии по min/max окна.

Raw, checkpoints и SQLite хранятся в data/. Минутные summaries ускоряют сайт,
а исходные события сохраняются 14 дней с минимальной опорой для границы окна.
data/backups/ содержит три последние локальные копии SQLite и tracked исходников;
raw не включён. Копии на том же VPS не защищают от потери VPS — переносите нужные
данные на своё внешнее хранилище. История и настройки локальной установки не включены
в репозиторий. Полный 14-дневный цикл и перезагрузка нового VPS пока не проверены.

## Разработка

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q mtanalog deploy
node tests/chart.mjs
```

Для браузерных тестов установите Playwright отдельно и задайте PLAYWRIGHT_MODULE,
CHROMIUM_EXECUTABLE (при необходимости), MTANALOG_BROWSER_URL (по умолчанию
http://127.0.0.1:8790). Запуск: `node tests/browser.mjs`, результаты в output/.
Тесты Python используют временные каталоги и не удаляют рабочий архив.
Локальный шрифт Manrope распространяется по SIL OFL: mtanalog/static/Manrope-OFL.txt.

## Лицензия

MIT — см. [LICENSE](LICENSE). Шрифт Manrope имеет отдельную лицензию SIL OFL.
