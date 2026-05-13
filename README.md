# Steam Trading Bot — ARCHITECTURE.md
> Вставляй этот файл в начало каждой новой сессии с ИИ вместо кода.

---

## Стек
- Python 3.11+, asyncio
- **aiosteampy** ≥ 0.7 — Steam API (login, inventory, listings, orders, history, trade offers)
- **aiohttp** (HTTP-клиент)
- **SQLite** (через stdlib sqlite3) — локальный кеш
- **protobuf** ≥ 5.26 — CS2 asset properties
- **python-dotenv** — env-переменные

---

## Файловая структура
```
simple.py           — главный скрипт: CLI-меню, логин, sweep, все команды
cache.py            — SQLite-кеш: аккаунты, балансы, листинги, ордера, инвентарь, история
item_info.py        — просмотр предмета: histogram, price_history, ASCII-график,
                       листинги (Steam Market v2 POST API: float/seed в ответе,
                       фильтры wear/seed/quality/exterior — серверные)
patterns.py         — детектор редких паттернов CS2 (7patterns.txt / 7patterns.json)
steam_errors.py     — классификатор ошибок Steam → SteamError(category, short, fatal_for_batch)

accounts/<name>/
  account.json      — {label, username, password, steam_id}
  *.maFile          — Steam Desktop Authenticator file

data/
  cache.sqlite3     — SQLite база
  7patterns.txt     — base_name'ы скинов с редкими паттернами (через запятую)
  7patterns.json    — точные paint_seed по тирам

proxies.txt         — список прокси (по одному на строку, http/socks5)
.steam_session/     — кешированные cookies (username.cookies)
```

---

## cache.py — публичный API

```python
open_db() -> sqlite3.Connection
record_account(username, label)
get_account_steam_id(username) -> str | None          # SteamID64 из БД
record_balance(username, balance_cents, on_hold_cents, currency_code)
record_listings(username, listings_iter, currency_code, partial=True) -> int
record_buy_orders(username, orders_iter, currency_code) -> int
record_history_events(username, events_iter, currency_code, price_extractor=None) -> int
record_inventory(username, app_context_name, items_iter, *, paint_seed_extractor,
                 paint_wear_extractor, state_extractor, partial=False) -> int
update_hidden_from_public(username, app_context, public_asset_ids) -> int
  # Проставляет hidden_from_public=1/0 — diff приватного инвентаря с публичным
get_last_refresh(username, resource) -> datetime | None
get_latest_balance(username) -> dict | None
get_listed_asset_ids(username) -> set[str]
find_listing_by_asset_id(username, asset_id) -> str | None
get_listing_by_asset_id(username, asset_id) -> dict | None
insert_placed_listing(username, listing_id, *, unowned_id, market_hash_name, price_cents, currency_code)
delete_listing(username, listing_id)
delete_buy_order(username, order_id)
mark_inventory_state_by_asset_id(username, asset_id, new_state) -> int
iter_inventory(username=None, app_context=None) -> list[dict]
iter_account_summaries() -> list[dict]
iter_all_market_history(limit=None) -> list[dict]     # cross-account история
get_buy_orders_total(username) -> dict | None
get_known_event_ids(username) -> set[str]
get_cached_nameid(app_id, market_hash_name) -> int | None
cache_nameid(app_id, market_hash_name, item_nameid)
get_cached_gid(app_id, market_hash_name) -> str | None
cache_gid(app_id, market_hash_name, gid)
```

### SQLite таблицы
- `accounts` (username PK, label, label_num, last_seen_at)
- `wallet_snapshots` (username, snapshot_at, balance_cents, on_hold_cents, currency_code)
- `listings_cache` (username+listing_id PK, asset_id, unowned_id, market_hash_name, price_cents, currency_code, time_created)
- `buy_orders_cache` (username+order_id PK, market_hash_name, price_cents, qty_remaining, qty_total)
- `market_history` (username+event_id PK, event_type, market_hash_name, time_event, price_cents) — append-only
- `inventory_cache` (username+app_context+asset_id PK, market_hash_name, amount, paint_seed, paint_wear, state, tradable_after, extra_json, **hidden_from_public** INTEGER, **last_public_check_at** TEXT)
- `market_nameids` (app_id+market_hash_name PK, item_nameid)
- `market_gids` (app_id+market_hash_name PK, gid) — новый Steam Market 2026: GID = базовый ид скина
- `refresh_log` (username+resource PK, last_refresh_at)

### inventory_cache.state
- `"free"` — свободен, можно выставлять
- `"on_market"` — уже на листинге
- `"trade_protect"` — получен через трейд, 7-дн. защита (context=16)
- `"trade_hold"` — market-hold после покупки с ТП

### inventory_cache.hidden_from_public
- `1` — есть в приватном инвентаре, но нет в публичной выдаче (display cooldown ~3 дня)
- `0` — виден публично
- `NULL` — ещё не проверялся sweep'ом

---

## patterns.py
```python
load_pattern_db(force=False) -> dict   # {danger_zone: set[str], rare_patterns: dict}
is_rare_pattern(market_hash_name, paint_seed) -> RarePatternResult
  # .is_rare: True | False | None(uncertain)
  # .tier_note: "Tier 1" если редкий
is_charm(market_hash_name) -> bool
```

## steam_errors.py
```python
classify_steam_error(exc) -> SteamError
  # .category: max_wallet | rate_limited | item_unavailable | price_too_low |
  #            price_too_high | need_mobile_confirm | session_expired | not_logged_in | network | unknown
  # .fatal_for_batch: True → прерывать bulk-цикл
  # .retryable: True → имеет смысл повторить
format_for_log(err, prefix="") -> str
```

## item_info.py
```python
await show_item_info_menu(client, market_hash_name, app, currency_enum, currency_code, ask=None, currency_sym="")
await resolve_item_nameid(client, app_id, market_hash_name) -> int | None
await resolve_gid(client, app_id, market_hash_name) -> str | None
  # Новый Steam Market 2026: GID вида G[0-9A-Fa-f]+ вместо market_hash_name­в-URL'е.
await _fetch_listings_page(session, app_id, gid, *, start, sort_field=0, sort_dir=1,
                            category_filters=None, wear_range=None, seed_range=None,
                            price_range=None, text_query=None, currency_code=None)
  # POST /market/listings/<app>/<gid>; все фильтры серверные.
  # Ответ (пропускаем через _parse_listings_v2) уже включает float (propertyid=2),
  # paint_seed (propertyid=1) и d-value (propertyid=6) для inspect-URL.
render_histogram_block / render_price_chart_block / render_sales_volume_block
render_data_table / render_full_stack_block / render_listings_page
```

---

## simple.py — ключевые функции

### Логин
```python
_discover_accounts() -> list[dict]
_connect_account(account, force_relogin, proxy=None) -> (client, currency_code, cookies_file) | None
_try_resume(client, cookies_file) -> bool
```

### CS2-экстракция
```python
_cs2_extract_wear_seed(item) -> (float|None, int|None)   # propertyid: 1=seed, 2=wear, 4=sticker_wear
_cs2_extract_stickers(item) -> [(name, wear|None), ...]
_cs2_extract_charms(item)   -> [(name, pattern|None), ...]
```

### Place sell — умный ретрай
```python
await _place_sell_listing_with_retry(client, item_or_asset_id, app_context, *, price, what)
  # При "Failed to perform confirmation action":
  #   → ищет pending-листинг в to_confirm/active по asset_id
  #   → cancel → перевыставляет (1 раз)

await _cancel_all_pending_confirmations(client, label="") -> (n_found, n_cancelled)
  # Чистит ВСЕ pending перед bulk-выставлением
```

### Sweep
```python
await _run_sweep(accounts, sessions, force_relogin)
await _sweep_one_account(account, sessions, force_relogin, fetch_history=True, proxy=None) -> dict
  # balance → listings → buy_orders → inventories (4 ctx + CS2_PROTECTED)
  # → _fetch_public_inventory_asset_ids (hidden_from_public diff) → history delta

await _fetch_public_inventory_asset_ids(session, steam_id_64, ctx_str, proxy=None)
  # -> (set[asset_id] | None, error_reason | None)
  # Чистая aiohttp-сессия БЕЗ кук (DummyCookieJar) через steamcommunity.com/inventory/
  # Backoff: 0→1→3→7с на 429/5xx, max 10 страниц по 2000
```

### Прокси
```python
_load_proxy_pool() -> list[str]
_mask_proxy_for_log(url) -> str
await _ask_use_proxy(label) -> list[str]   # интерактивно спрашивает «использовать прокси?»

class _ProxyRotator:                       # round-robin с failover
    current() -> str|None                  # текущий прокси
    mark_bad()                             # помечает bad, переходит к следующему
    advance()                              # переходит без bad-mark
    # если все bad — сбрасывает чёрный список
```

### Bulk-операции
```python
await _bulk_list_group(client, group, currency_enum, currency_code, ...)
await _bulk_cancel_listings(client, listings, ask_confirm=True) -> int
await _bulk_sell_cross_account(name, candidates, accounts_lookup, sessions, ...)
await _bulk_cancel_cross_account(name, listed_rows, accounts_lookup, sessions, ...)

await _collect_all_listings(accounts, sessions, force_relogin)
  # Однократный сбор ВСЕХ sell-листингов по всем аккам
  # → _ProxyRotator → get_my_listings страницами по 100
  # → record_listings(partial=False) — полная перезапись кеша
  # Пауза 4-8с (random) между аккаунтами
```

### Авто-трейд (задача 2)
```python
_AUTOTRADE_TASK                        # asyncio.Task | None
_AUTOTRADE_STATE: dict                 # {usernames, interval_sec, accepted, errors, last_poll, started_at}

await _autotrade_loop(usernames, sessions, accounts_lookup, force_relogin, label_lookup, interval_sec)
  # Фоновый asyncio-цикл каждые interval_sec
  # Принимает ТОЛЬКО офферы где items_to_give=[] (юзер ничего не отдаёт)
  # Если бот просит что-то отдать — пропускаем

await _start_autotrade(accounts, sessions, force_relogin)
  # Меню: старт / стоп / статус
```

### Меню глобальной статистики
```python
await _show_global_stats(accounts, sessions, force_relogin)
await _show_cs2_subgroups(rows, ...)
await _show_grouped_items(rows, title, ...)   # i<N>/s<N>/c<N>
await _show_recently_unlocked(accounts, sessions, force_relogin, label_lookup)
  # Cross-account список hidden_from_public=1
  # Fallback: tradable_after ∈ [now-3d, now]
  # s<N> → bulk-sell всех state=free экземпляров
await _show_global_market_history(accounts, limit=200)
  # Таблица последних N событий из market_history
  # m<N> → показать больше
```

### Пагинация
```python
await _paginate(items, page_size, render, extra_commands, bulk_commands)
await _paginate_lazy(total, page_size, fetch_more, render, extra_commands)
```

---

## Конфигурация
```python
STEAM_PASSWORD / MAFILE_PATH / FORCE_RELOGIN
INVENTORY_PAGE_SIZE=25 / HISTORY_PAGE_SIZE=10 / LISTINGS_PAGE_SIZE=20 (фиксирован Steam'ом)
BUY_ORDER_LIMIT_MULTIPLIER=10

# Env:
STEAM_PASSWORD_<USERNAME>
SWEEP_PROXY / SWEEP_PROXY_FILE
```

---

## Важные нюансы
1. **aiosteampy 0.7** — monkey-patch `ItemDescription._set_d_id`
2. **Троттлинг**: 0.3–0.4с между place_sell, 0.5с между аккаунтами в sweep, 4-8с (random) при сборе листингов
3. **429 retry**: `_with_retry` — 3 попытки, задержки 3/8/20с
4. **listed_asset_ids**: Steam оставляет предмет в инвентаре после выставления — cross-ref по unowned_id
5. **Steam Market v2 (2026)**: float/paint_seed приходят в ответе POST `/market/listings/<app>/<GID>` — CSFloat убран, page size зашит у Steam'а в 20 шт., фильтры все серверные
6. **Public inventory diff**: чистая сессия БЕЗ кук залогиненного акка — иначе Steam 401/403
7. **_place_sell_listing_with_retry**: при confirm-сбое → cancel pending → перевыставляет 1 раз
8. **Авто-трейд**: items_to_give=[] — строгая проверка, иначе пропуск

---

## Текущая задача
<!-- ЗАПОЛНЯЙ ЭТО ПОЛЕ КАЖДУЮ СЕССИЮ -->
_Опиши что сейчас делаешь: какой модуль правишь, какая фича нужна, что не работает_
