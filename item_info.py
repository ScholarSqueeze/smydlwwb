# -*- coding: utf-8 -*-
"""Просмотр листингов и графика цены предмета на Steam Market.

Делает 2 запроса к Steam:
- `client.get_item_orders_histogram(item_nameid)` — топ buy- и sell-таблицы;
- `client.fetch_price_history(market_hash_name, app)` — список точек
  «дата → средняя цена → объём».

`item_nameid` (внутренний числовой ID Steam) НЕ совпадает с market_hash_name.
Получаем его одноразовым fetch'ем HTML-страницы предмета и кешируем в SQLite.

Использование (как минимум):
    from item_info import show_item_info_menu
    await show_item_info_menu(client, "AK-47 | Redline (Field-Tested)", App.CS2,
                              Currency, currency_code)
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp


# =============================================================================
# Резолвинг item_nameid (одноразовый запрос + кеш)
# =============================================================================
_NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")


async def _fetch_nameid_from_steam(
    session: aiohttp.ClientSession, app_id: int, market_hash_name: str
) -> int | None:
    """Скачивает страницу `/market/listings/{app_id}/{name}` и достаёт `item_nameid`.

    Возвращает None если страница вернулась 404 / без id в HTML.
    """
    url = (
        f"https://steamcommunity.com/market/listings/{app_id}/"
        + urllib.parse.quote(market_hash_name, safe="")
    )
    async with session.get(url, headers={"Accept-Language": "en-US,en;q=0.9"}) as resp:
        if resp.status != 200:
            return None
        html = await resp.text()
    match = _NAMEID_RE.search(html)
    if not match:
        return None
    return int(match.group(1))


async def resolve_item_nameid(
    client, app_id: int, market_hash_name: str
) -> int | None:
    """Возвращает item_nameid с использованием SQLite-кеша.

    Идёт по порядку: 1) cache.sqlite3 → 2) Steam HTML → 3) None.
    Если Steam HTML отдал id — пишем в кеш для следующих запусков.
    """
    try:
        import cache

        cached = cache.get_cached_nameid(app_id, market_hash_name)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001
        # Кеш — не критично, продолжаем с сетью.
        pass

    nameid = await _fetch_nameid_from_steam(client.session, app_id, market_hash_name)
    if nameid is None:
        return None

    try:
        import cache

        cache.cache_nameid(app_id, market_hash_name, nameid)
    except Exception:  # noqa: BLE001
        pass
    return nameid


# =============================================================================
# ASCII-график для price_history
# =============================================================================
_BLOCKS = " ▁▂▃▄▅▆▇█"  # 9 уровней высоты


@dataclass
class PriceChartSlice:
    label: str  # «7d» / «30d» / «all»
    points: list[Any]  # PriceHistoryEntry-like (.date, .price, .daily_volume)


def _slice_history(history: list, label: str) -> list:
    """Из истории отдаёт нужный отрезок.

    Steam отдаёт точки за последние ~30 дней (по 1 точке/день) + более редкие
    точки старше. `fetch_price_history` уже возвращает их по возрастанию даты.
    """
    if not history:
        return []
    now = datetime.now(timezone.utc)
    if label == "all":
        return history
    if label == "7d":
        cutoff = now.timestamp() - 7 * 24 * 3600
    elif label == "30d":
        cutoff = now.timestamp() - 30 * 24 * 3600
    else:
        return history
    out = []
    for p in history:
        dt = p.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() >= cutoff:
            out.append(p)
    return out


def _downsample(values: list[float], width: int) -> tuple[list[float], list[float], list[float]]:
    """Даунсэмплим values до width столбцов. Возвращает (mean, min, max) на сегмент.

    Если len(values) <= width — возвращает значения как mean=min=max=value.
    """
    n = len(values)
    if n <= width:
        return list(values), list(values), list(values)
    step = n / width
    means: list[float] = []
    mins: list[float] = []
    maxs: list[float] = []
    for i in range(width):
        start = int(i * step)
        end = int((i + 1) * step)
        if end <= start:
            end = start + 1
        seg = values[start:end] or [values[start]]
        means.append(sum(seg) / len(seg))
        mins.append(min(seg))
        maxs.append(max(seg))
    return means, mins, maxs


def _render_ascii_chart(
    values: list[float], width: int = 60, height: int = 8
) -> list[str]:
    """Возвращает список строк ASCII-чарта (БЕЗ Y/X-осей).

    Использовать `_render_chart_with_axes` для версии с подписями.
    """
    if not values:
        return ["(нет данных)"]
    # Даунсэмплим до width столбцов.
    n = len(values)
    if n > width:
        values, _, _ = _downsample(values, width)
    elif n < width:
        width = n

    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return ["─" * width, f"{hi:.2f} (без изменений)"]

    rng = hi - lo
    rows: list[list[str]] = [[" "] * width for _ in range(height)]
    for col, v in enumerate(values):
        norm = (v - lo) / rng
        units = norm * (height * 8)
        full_rows = int(units // 8)
        rem = int(units) - full_rows * 8
        for r in range(full_rows):
            rows[height - 1 - r][col] = _BLOCKS[8]
        if rem > 0 and full_rows < height:
            rows[height - 1 - full_rows][col] = _BLOCKS[rem]
    out = ["".join(r) for r in rows]
    out.append(f"  min: {lo:.2f}   max: {hi:.2f}   ({len(values)} точек)")
    return out


_MONTHS_RU = [
    "", "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _fmt_date_short(dt) -> str:
    """`Май 4` / `Дек 31` для оси X."""
    return f"{_MONTHS_RU[dt.month].capitalize()} {dt.day}"


def _render_chart_with_axes(  # noqa: PLR0912, PLR0915, C901
    points: list,  # PriceHistoryEntry-like
    sym: str,
    width: int = 60,
    height: int = 8,
) -> list[str]:
    """Чарт с Y-осью (цены слева) и X-осью (даты снизу).

    Использует min/max сегмента для тонкого «теневого» рендера — даёт ощущение
    высокого/низкого внутри-сегментного диапазона.
    """
    if not points:
        return ["(нет данных)"]
    values = [float(p.price) for p in points]
    n = len(values)
    # Даунсэмплим, если нужно. Заодно ужимаем даты пропорционально.
    if n > width:
        means, mins, maxs = _downsample(values, width)
        # Даты — берём центр каждого сегмента.
        step = n / width
        date_indexes = [min(n - 1, int((i + 0.5) * step)) for i in range(width)]
        dates = [points[i].date for i in date_indexes]
    else:
        means = list(values)
        mins = list(values)
        maxs = list(values)
        dates = [p.date for p in points]
        width = n

    lo = min(mins)
    hi = max(maxs)
    if hi <= lo:
        # Нет вариации.
        prefix = f"  {hi:.2f} {sym} ".rjust(10)
        return [f"{prefix}|{'─' * width}", f"  (без изменений за период)"]

    rng = hi - lo
    # Готовим сетку.
    grid: list[list[str]] = [[" "] * width for _ in range(height)]

    def _y_for(value: float) -> float:
        """Возвращает «высоту» 0..(height*8) для значения."""
        return (value - lo) / rng * (height * 8)

    for col, mv in enumerate(means):
        units = _y_for(mv)
        full_rows = int(units // 8)
        rem = int(units) - full_rows * 8
        # Mean — полностью заполненная колонка.
        for r in range(full_rows):
            grid[height - 1 - r][col] = _BLOCKS[8]
        if rem > 0 and full_rows < height:
            grid[height - 1 - full_rows][col] = _BLOCKS[rem]
        # Max — обозначаем «верхушку» тонким штрихом «·» там, где её ещё нет.
        max_units = _y_for(maxs[col])
        max_row = height - 1 - int(max_units // 8)
        if 0 <= max_row < height and grid[max_row][col] == " ":
            grid[max_row][col] = "·"

    # Y-axis labels: каждая строка = (lo + rng * (height - row_idx) / height).
    # Печатаем подписи через каждую строку (чтобы цифры не перекрывали).
    y_labels: list[str] = []
    for row in range(height):
        # Центр строки в координатах цены:
        frac = (height - row - 0.5) / height
        val = lo + frac * rng
        if row % 2 == 0:
            y_labels.append(f"{val:>7.2f} {sym}")
        else:
            y_labels.append(" " * (8 + len(sym)))

    chart_lines = [f"{y_labels[row]} │{''.join(grid[row])}" for row in range(height)]
    # Нижняя ось (горизонтальная линия).
    bottom = (" " * (8 + len(sym))) + " └" + ("─" * width)
    chart_lines.append(bottom)

    # X-axis labels.
    if dates:
        # Выберем 5-6 равноотстоящих делений.
        n_ticks = min(6, max(2, width // 12))
        tick_cols = [int(i * (width - 1) / (n_ticks - 1)) for i in range(n_ticks)]
        tick_labels = [(c, _fmt_date_short(dates[c])) for c in tick_cols]
        # Строим строку X-меток.
        line = [" "] * width
        for col, lbl in tick_labels:
            start = col
            # Сдвигаем влево, чтобы метка влезла.
            if start + len(lbl) > width:
                start = width - len(lbl)
            if start < 0:
                start = 0
            for j, ch in enumerate(lbl):
                if 0 <= start + j < width and line[start + j] == " ":
                    line[start + j] = ch
        chart_lines.append((" " * (8 + len(sym))) + "  " + "".join(line))
    # Подытог.
    chart_lines.append(
        f"  min: {lo:.2f} {sym}   max: {hi:.2f} {sym}   ({n} точек, {width} столбцов)"
    )
    return chart_lines


def _sum_volume(history: list, days: float) -> int:
    """Сумма daily_volume по всем точкам не старше `days` дней (от текущего момента).

    Steam отдаёт ~30 свежих дневных точек + более редкие точки старше; для
    «день/неделя/месяц» все нужные точки попадают в дневное окно, поэтому простая
    сумма работает корректно.
    """
    if not history:
        return 0
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - days * 24 * 3600
    total = 0
    for p in history:
        dt = p.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() < cutoff:
            continue
        v = getattr(p, "daily_volume", None) or 0
        try:
            total += int(v)
        except (TypeError, ValueError):
            continue
    return total


def render_sales_volume_block(history: list) -> list[str]:
    """Кол-во продаж за день / неделю / месяц (из price_history → daily_volume).

    Это агрегированные сделки Steam Market (то же, что показывает сам Steam
    под графиком цены — но в одном месте и сразу за три периода).
    """
    lines = ["=== Продажи (Steam aggregate) ==="]
    if not history:
        lines.append("  (нет данных истории)")
        return lines
    day = _sum_volume(history, 1)
    week = _sum_volume(history, 7)
    month = _sum_volume(history, 30)
    lines.append(f"  За сутки:   {day} шт.")
    lines.append(f"  За неделю:  {week} шт.")
    lines.append(f"  За месяц:   {month} шт.")
    # Последняя точка — показывает «свежесть» данных Steam'а.
    last = history[-1]
    last_dt = last.date
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    last_date = last_dt.strftime("%Y-%m-%d")
    last_vol = getattr(last, "daily_volume", None) or 0
    try:
        last_vol_i = int(last_vol)
    except (TypeError, ValueError):
        last_vol_i = 0
    lines.append(f"  Последняя дневная точка: {last_date} → {last_vol_i} шт.")
    return lines


def render_price_chart_block(history: list, label: str, sym: str = "") -> list[str]:
    """Готовый блок строк: заголовок + чарт для одного периода + Y/X-оси."""
    points = _slice_history(history, label)
    title_map = {
        "7d": "за неделю",
        "30d": "за месяц",
        "all": "за всё время",
    }
    title = f"=== График цены {title_map.get(label, label)} ({label}) ==="
    if not points:
        return [title, "(нет точек за этот период)"]
    lines = [title]
    lines.extend(_render_chart_with_axes(points, sym=sym or "_", width=60, height=8))
    # Дополнительно — пара цифр.
    if len(points) >= 2:
        first_p = float(points[0].price)
        last_p = float(points[-1].price)
        delta = last_p - first_p
        pct = (delta / first_p * 100) if first_p else 0
        first_date = points[0].date.strftime("%Y-%m-%d")
        last_date = points[-1].date.strftime("%Y-%m-%d")
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"  {first_date}: {first_p:.2f} {sym}  ->  "
            f"{last_date}: {last_p:.2f} {sym}  ({sign}{delta:.2f} / {sign}{pct:.1f}%)"
        )
    return lines


def render_data_table(history: list, label: str, sym: str = "", limit: int = 30) -> list[str]:
    """Сырые точки `дата → цена → объём` за период (хвост — `limit` последних)."""
    points = _slice_history(history, label)
    title_map = {"7d": "неделя", "30d": "месяц", "all": "всё время"}
    if not points:
        return [f"=== Таблица точек ({title_map.get(label, label)}) ===", "(нет точек)"]
    tail = points[-limit:]
    lines = [
        f"=== Таблица точек ({title_map.get(label, label)}); последних {len(tail)} ===",
        f"    {'Дата':<20}{'Цена':>12}    {'Объём':>8}",
    ]
    for p in tail:
        date_str = p.date.strftime("%Y-%m-%d %H:%M")
        price_str = f"{float(p.price):.2f} {sym}"
        vol = getattr(p, "daily_volume", None) or 0
        lines.append(f"    {date_str:<20}{price_str:>12}    {int(vol):>8}")
    return lines


# =============================================================================
# Рендер таблицы топ-листингов и топ-ордеров (histogram)
# =============================================================================
def render_histogram_block(
    histogram, sym: str, max_rows: int = 10
) -> list[str]:
    """Возвращает строки с топ buy- и sell-таблицами.

    histogram — это `ItemOrdersHistogram` от aiosteampy (поля sell_order_table /
    buy_order_table — список NamedTuple'ов price/price_with_fee/quantity).
    """
    lines = ["=== Глубина рынка (histogram) ==="]
    lowest_sell = histogram.lowest_sell_order
    highest_buy = histogram.highest_buy_order
    spread_str = "—"
    if lowest_sell and highest_buy:
        spread = lowest_sell - highest_buy
        spread_str = f"{spread / 100:.2f} {sym} ({spread / lowest_sell * 100:.1f}%)"
    lowest_sell_str = f"{(lowest_sell or 0) / 100:.2f} {sym}"
    highest_buy_str = f"{(highest_buy or 0) / 100:.2f} {sym}"
    lines.append(
        f"  Наименьшее sell: {lowest_sell_str}   "
        f"Наибольшее buy:   {highest_buy_str}   "
        f"Спред: {spread_str}"
    )
    lines.append(
        f"  Всего sell-листингов: {histogram.sell_order_count}   "
        f"buy-ордеров: {histogram.buy_order_count}"
    )

    price_head = "  Цена"
    qty_head = "Q-ty"

    # Sell-table («Sell orders»): цена / комиссия / кол-во.
    lines.append("")
    lines.append("  Продают:")
    lines.append(f"    {price_head:<14}{qty_head:<8}")
    for row in (histogram.sell_order_table or [])[:max_rows]:
        price = getattr(row, "price", None)
        qty = getattr(row, "quantity", None)
        if price is None or qty is None:
            continue
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}")
    if not histogram.sell_order_table:
        lines.append("    (пусто)")

    # Buy-table («Buy orders»): цена / кол-во.
    lines.append("")
    lines.append("  Покупают:")
    lines.append(f"    {price_head:<14}{qty_head:<8}")
    for row in (histogram.buy_order_table or [])[:max_rows]:
        price = getattr(row, "price", None)
        qty = getattr(row, "quantity", None)
        if price is None or qty is None:
            continue
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}")
    if not histogram.buy_order_table:
        lines.append("    (пусто)")
    return lines


def _graph_to_deltas(graph) -> list[tuple[int, int]]:
    """Конвертит `sell_order_graph` / `buy_order_graph` из CUMULATIVE в DELTAS.

    Steam отдаёт graph как [(price, cumulative_qty, repr), ...] до 100 точек
    на сторону. Для отображения «цена → сколько ордеров на ЭТОЙ цене» нам нужны
    дельты между соседними точками.

    Возвращает [(price_cents, qty_at_this_price), ...].
    """
    rows: list[tuple[int, int]] = []
    prev = 0
    for entry in graph or []:
        price = getattr(entry, "price", None)
        cum = getattr(entry, "quantity", None)
        if price is None or cum is None:
            continue
        # graph иногда может прийти в float-долларах через старые версии aiosteampy.
        # Сейчас (0.7+) — int cents. Считаем int → 0.
        price_int = int(price)
        cum_int = int(cum)
        qty = max(0, cum_int - prev)
        prev = cum_int
        if qty > 0:
            rows.append((price_int, qty))
    return rows


def render_full_stack_block(
    histogram, sym: str, *, side: str, limit: int | None = None
) -> list[str]:
    """Печатает полный стакан (до ~100 строк, как у Steam).

    side="sell" — стакан продажи (sell_order_graph).
    side="buy"  — стакан покупки (buy_order_graph).
    Steam отдаёт до 100 точек в графе, мы берём ВСЕ (или `limit`, если задан).
    Source-of-truth — graph, а не table (которая обычно =6-10 строк).
    """
    assert side in ("sell", "buy")
    graph = histogram.sell_order_graph if side == "sell" else histogram.buy_order_graph
    total_count = histogram.sell_order_count if side == "sell" else histogram.buy_order_count
    side_label = "Продают" if side == "sell" else "Покупают"
    deltas = _graph_to_deltas(graph)
    if side == "buy":
        # buy: graph упорядочен от высокой цены к низкой. Так оставляем — самая
        # выгодная для продавца сверху.
        pass
    else:
        # sell: graph упорядочен от низкой цены к высокой. Тоже оставляем —
        # самые дешёвые сверху, как у Steam.
        pass
    if limit is not None and limit > 0:
        deltas = deltas[:limit]
    lines = [f"=== Полный {side_label.lower()}-стакан (max ~100 строк от Steam) ==="]
    lines.append(
        f"  Всего: {total_count} ордеров, показываем уникальных цен: {len(deltas)}."
    )
    price_head = "  Цена"
    qty_head = "Q-ty"
    cum_head = "Σ (cum.)"
    lines.append(f"    {price_head:<14}{qty_head:<8}{cum_head:<10}")
    cum = 0
    for price, qty in deltas:
        cum += qty
        price_str = f"{price / 100:.2f} {sym}"
        lines.append(f"    {price_str:<14}{qty:<8}{cum:<10}")
    if not deltas:
        lines.append("    (пусто)")
    return lines


# =============================================================================
# Floats viewer (выставленные листинги с inspect-link'ами и опц. флоатами)
# =============================================================================
_INSPECT_M_RE = re.compile(r"\+csgo_econ_action_preview%20(M\d+A\d+D\d+)")
_INSPECT_S_RE = re.compile(r"\+csgo_econ_action_preview%20(S\d+A\d+D\d+)")
_FLOAT_VALUE_RE = re.compile(r"\"floatvalue\"\s*:\s*([0-9.]+)")
_PAINT_SEED_RE = re.compile(r"\"paintseed\"\s*:\s*(\d+)")


async def _fetch_listings_page(
    session: aiohttp.ClientSession,
    app_id: int,
    market_hash_name: str,
    *,
    start: int,
    count: int,
    currency_code: int,
) -> dict | None:
    """Скачивает страницу `/market/listings/{app_id}/{name}/render`.

    Возвращает разобранный JSON-словарь Steam'а либо None при ошибке.
    """
    url = (
        f"https://steamcommunity.com/market/listings/{app_id}/"
        + urllib.parse.quote(market_hash_name, safe="")
        + "/render/"
    )
    params = {
        "query": "",
        "start": str(start),
        "count": str(count),
        "country": "US",
        "language": "english",
        "currency": str(currency_code),
    }
    async with session.get(url, params=params,
                            headers={"Accept-Language": "en-US,en;q=0.9"}) as resp:
        if resp.status != 200:
            return None
        try:
            return await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            return None


def _parse_listings_render(data: dict) -> list[dict]:
    """Из ответа `/render/` достаёт список листингов.

    Каждый элемент: {listing_id, asset_id, price_cents, inspect_url}.
    Внутри `listinginfo` ключ = listing_id; `asset.market_actions[0].link` —
    шаблон ссылки `…M<listing_id>A<asset_id>D<dvalue>`. listing_id /
    asset_id / d-value подставляются вместо плейсхолдеров %listingid% etc.
    """
    out: list[dict] = []
    listings = (data or {}).get("listinginfo") or {}
    for listing_id, info in listings.items():
        try:
            asset = info["asset"]
            asset_id = str(asset["id"])
            # Steam отдаёт price в виде «converted_price_per_unit + converted_fee_per_unit».
            # Если их нет — fallback на price (без комиссии).
            if "converted_price_per_unit" in info:
                price = int(info["converted_price_per_unit"])
                fee = int(info.get("converted_fee_per_unit") or 0)
                price_cents = price + fee
            else:
                price_cents = int(info.get("price") or 0)
            actions = asset.get("market_actions") or []
            inspect = ""
            if actions:
                inspect_template = actions[0].get("link") or ""
                inspect = (
                    inspect_template
                    .replace("%listingid%", listing_id)
                    .replace("%assetid%", asset_id)
                )
            out.append({
                "listing_id": listing_id,
                "asset_id": asset_id,
                "price_cents": price_cents,
                "inspect_url": inspect,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


# api.csfloat.com часто отбивает «голые» запросы (без браузерных заголовков),
# поэтому добавляем User-Agent + Referer + Origin как реальный браузер.
_CSFLOAT_BASE_URL = "https://api.csfloat.com/"
_CSFLOAT_HOST = "api.csfloat.com"

# Кешируем результат DNS-резолва api.csfloat.com на время процесса:
#   None — ещё не проверяли;
#   True — резолв успешный, ходим;
#   False — резолв упал (нет интернета / DNS у провайдера, нужна VPN).
# После одного fail (None, None) возвращаем мгновенно — не насилуем сеть и
# не ждём таймаут на каждый из 10/20/50 листингов (см. fix #8 из бэклога).
_CSFLOAT_DNS_OK: bool | None = None


async def _csfloat_dns_check() -> bool:
    """True если api.csfloat.com резолвится; False если нет.

    Прогоняется один раз за процесс — результат кешируется в `_CSFLOAT_DNS_OK`.
    """
    global _CSFLOAT_DNS_OK
    if _CSFLOAT_DNS_OK is not None:
        return _CSFLOAT_DNS_OK
    import asyncio  # локально — не плодим top-level импорты
    import socket
    loop = asyncio.get_running_loop()
    try:
        await loop.getaddrinfo(_CSFLOAT_HOST, 443, proto=socket.IPPROTO_TCP)
        _CSFLOAT_DNS_OK = True
    except Exception as exc:  # noqa: BLE001
        _CSFLOAT_DNS_OK = False
        print(
            f"   [!] api.csfloat.com — DNS-резолв упал: "
            f"{type(exc).__name__}: {exc}\n"
            "       Скорее всего, провайдер блокирует csfloat.com (нужна VPN) "
            "или\n"
            "       нет интернета. Флоаты в этой сессии резолвиться не будут —\n"
            "       последующие запросы я не буду отправлять, чтобы не ждать\n"
            "       таймаут на каждом листинге. Включи VPN и перезапусти скрипт."
        )
    return _CSFLOAT_DNS_OK
_CSFLOAT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "DNT": "1",
    "Referer": "https://csfloat.com/",
    "Origin": "https://csfloat.com",
}


def _parse_csfloat_payload(data: Any) -> tuple[float | None, int | None]:
    """Из JSON-ответа api.csfloat.com достаёт (floatvalue, paintseed).

    Поддерживает оба формата: {"iteminfo": {...}} и старый flat-формат с
    floatvalue/paintseed на верхнем уровне.
    """
    if not isinstance(data, dict):
        return (None, None)
    info = data.get("iteminfo") if isinstance(data.get("iteminfo"), dict) else data
    fl_raw = info.get("floatvalue")
    sd_raw = info.get("paintseed")
    try:
        fl = float(fl_raw) if fl_raw is not None else None
    except (TypeError, ValueError):
        fl = None
    try:
        sd = int(sd_raw) if sd_raw is not None else None
    except (TypeError, ValueError):
        sd = None
    return (fl, sd)


async def _resolve_float_via_csfloat(
    session: aiohttp.ClientSession, inspect_url: str,
    *,
    proxy: str | None = None,
) -> tuple[float | None, int | None]:
    """Запрашивает api.csfloat.com и возвращает (float, seed).

    Сначала пытается через `httpx.AsyncClient` (api.csfloat при таком TLS
    fingerprint отвечает охотнее, чем у aiohttp). Если `httpx` не установлен —
    fallback на aiohttp с теми же браузерными заголовками. На ошибки/таймауты
    возвращает (None, None).

    Бесплатный публичный endpoint, rate-limit ~30 req/min — троттлинг задаёт
    вызывающий (`_show_listings_with_floats` спит 2с между запросами).
    """
    if not inspect_url:
        return (None, None)

    # 0) DNS pre-flight. Если api.csfloat.com не резолвится — выходим сразу.
    # Без этой проверки каждый из 10/20/50 листингов будет ждать таймаут
    # httpx.ConnectError → aiohttp.ClientConnectorDNSError, что подвешивает
    # UI на минуту-другую. Один раз за процесс пробуем DNS — потом кеш.
    if not await _csfloat_dns_check():
        return (None, None)

    # 1) httpx (предпочтительный путь, как предложено).
    try:
        import httpx  # noqa: PLC0415  (лазовый импорт — не у всех установлен)
    except ImportError:
        httpx = None  # type: ignore[assignment]

    if httpx is not None:
        try:
            httpx_kwargs: dict = dict(
                base_url=_CSFLOAT_BASE_URL,
                headers=_CSFLOAT_HEADERS,
                timeout=15.0,
            )
            if proxy:
                # httpx >=0.28 принимает `proxy="http://..."`; старые версии — `proxies=`.
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy, **httpx_kwargs
                    ) as client:
                        resp = await client.get("/", params={"url": inspect_url})
                except TypeError:
                    async with httpx.AsyncClient(
                        proxies=proxy, **httpx_kwargs
                    ) as client:
                        resp = await client.get("/", params={"url": inspect_url})
            else:
                async with httpx.AsyncClient(**httpx_kwargs) as client:
                    resp = await client.get("/", params={"url": inspect_url})
            if resp.status_code == 200:
                try:
                    return _parse_csfloat_payload(resp.json())
                except ValueError:
                    # json() ругнётся если ответ не JSON — fallback на regex по тексту.
                    text = resp.text
                    fl_m = _FLOAT_VALUE_RE.search(text)
                    sd_m = _PAINT_SEED_RE.search(text)
                    fl = float(fl_m.group(1)) if fl_m else None
                    sd = int(sd_m.group(1)) if sd_m else None
                    return (fl, sd)
        except Exception:  # noqa: BLE001
            # На любую ошибку httpx — попробуем aiohttp ниже.
            pass

    # 2) Fallback: aiohttp с теми же заголовками.
    aiohttp_kwargs: dict = dict(
        params={"url": inspect_url},
        headers=_CSFLOAT_HEADERS,
        timeout=aiohttp.ClientTimeout(total=15),
    )
    if proxy:
        aiohttp_kwargs["proxy"] = proxy
    try:
        async with session.get(
            _CSFLOAT_BASE_URL,
            **aiohttp_kwargs,
        ) as resp:
            if resp.status != 200:
                return (None, None)
            try:
                data = await resp.json(content_type=None)
                return _parse_csfloat_payload(data)
            except Exception:  # noqa: BLE001
                text = await resp.text()
                fl_m = _FLOAT_VALUE_RE.search(text)
                sd_m = _PAINT_SEED_RE.search(text)
                fl = float(fl_m.group(1)) if fl_m else None
                sd = int(sd_m.group(1)) if sd_m else None
                return (fl, sd)
    except Exception:  # noqa: BLE001
        return (None, None)


def render_listings_page(
    listings: list[dict],
    *,
    sym: str,
    start_idx: int,
    total: int,
    floats: dict[str, tuple[float | None, int | None]] | None = None,
) -> list[str]:
    """Рисует страницу листингов.

    floats: dict listing_id → (float, seed). Если есть — печатаем колонку флоата.
    """
    lines = []
    lines.append("=" * 88)
    lines.append(
        f"   Листинги ({start_idx + 1}..{start_idx + len(listings)} из {total})"
    )
    lines.append("=" * 88)
    has_floats = bool(floats)
    if has_floats:
        lines.append(
            f"   {'#':<4}{'Цена':<14}{'Float':<10}{'Seed':<7}{'listing_id':<22}asset_id"
        )
    else:
        lines.append(
            f"   {'#':<4}{'Цена':<14}{'listing_id':<22}{'asset_id':<20}inspect"
        )
    lines.append("-" * 88)
    for i, item in enumerate(listings, start_idx + 1):
        price_str = f"{item['price_cents'] / 100:.2f} {sym}"
        if has_floats:
            fl, sd = (floats or {}).get(item["listing_id"], (None, None))
            fl_str = f"{fl:.4f}" if fl is not None else "—"
            sd_str = str(sd) if sd is not None else "—"
            lines.append(
                f"   {i:<4}{price_str:<14}{fl_str:<10}{sd_str:<7}"
                f"{item['listing_id']:<22}{item['asset_id']}"
            )
        else:
            inspect_short = item["inspect_url"][:40] + "..." if (
                len(item["inspect_url"]) > 40
            ) else item["inspect_url"]
            lines.append(
                f"   {i:<4}{price_str:<14}{item['listing_id']:<22}"
                f"{item['asset_id']:<20}{inspect_short}"
            )
    lines.append("-" * 88)
    return lines


# =============================================================================
# Главное меню «Инфо о предмете»
# =============================================================================
async def show_item_info_menu(  # noqa: PLR0912, PLR0915, C901
    client,
    market_hash_name: str,
    app,  # aiosteampy.constants.App
    currency_enum,
    currency_code: int,
    *,
    ask=None,
    currency_sym: str = "",
) -> None:
    """CLI: показывает histogram + график цены, даёт переключать период графика.

    `ask` — асинхронная функция запроса строки от пользователя (как `_ask` в
    simple.py). Если None — функция отрисует один раз все 3 периода и вернётся.
    """
    print(f"\n=== ИНФОРМАЦИЯ ПО ПРЕДМЕТУ: {market_hash_name} ===")
    print("[..] Резолвлю item_nameid (из кеша или со страницы Steam) ...")
    app_id = int(app)
    nameid = await resolve_item_nameid(client, app_id, market_hash_name)
    if nameid is None:
        print(
            "   [!] Не смог вытащить item_nameid из Steam-страницы. "
            "Возможные причины: предмет убрали с маркета, "
            "либо Steam поменял HTML-структуру."
        )
        return

    print(f"   item_nameid = {nameid}.")
    print("[..] Гружу histogram (топ buy/sell) ...")
    try:
        histogram, _ = await client.get_item_orders_histogram(nameid)
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] Histogram не загружен: {type(exc).__name__}: {exc}")
        histogram = None

    print("[..] Гружу историю цен (rate-limited Steam'ом) ...")
    try:
        history = await client.fetch_price_history(market_hash_name, app)
    except Exception as exc:  # noqa: BLE001
        # `fetch_price_history` доступен только если предмет хоть раз был
        # у тебя в инвентаре / в покупках. Steam возвращает 500/400 иначе.
        print(
            f"   [!] История цен недоступна "
            f"({type(exc).__name__}: {exc}). "
            "Это ОК, если предмет никогда не был в инвентаре этого акка. "
            "Histogram всё равно покажем."
        )
        history = []

    # Печатаем histogram.
    if histogram is not None:
        for line in render_histogram_block(histogram, currency_sym or "_"):
            print(line)

    if not history:
        print("\n(без истории цен графики и счётчики продаж пусты)")
        return

    sym = currency_sym or ""

    # Сводка продаж за день/неделю/месяц — печатается один раз перед графиком,
    # потому что цифры одинаковые для всех периодов (день/неделя/месяц считаются
    # из тех же daily_volume).
    print()
    for line in render_sales_volume_block(history):
        print(line)

    if ask is None:
        # Не интерактивно — печатаем все 3 периода один раз.
        for mode in ("7d", "30d", "all"):
            print()
            for line in render_price_chart_block(history, mode, sym):
                print(line)
        return

    # Интерактив: пользователь переключает период / разворачивает стаканы.
    current = "30d"
    while True:
        print()
        for line in render_price_chart_block(history, current, sym):
            print(line)
        print()
        cmd = (
            await ask(
                "  команды: 7=неделя / 30=месяц / a=всё / "
                "t=таблица точек / s=полный sell-стакан / b=полный buy-стакан / "
                "f [N]=листинги с флоатами (по умолчанию 10, max 100) / "
                "Enter=выход: "
            )
        ).strip().lower()
        if cmd == "":
            return
        if cmd in ("7", "7d", "w", "week"):
            current = "7d"
        elif cmd in ("30", "30d", "m", "month"):
            current = "30d"
        elif cmd in ("a", "all"):
            current = "all"
        elif cmd in ("t", "table", "т", "таблица"):
            print()
            for line in render_data_table(history, current, sym):
                print(line)
            # Не меняем график — просто продолжаем цикл (он перерисует тот же
            # период следующей итерацией). Притормозим для удобства.
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("s") and histogram is not None:
            # `s` — полный sell-стакан (через sell_order_graph, до ~100 строк);
            # `s 30` — первые 30. Старая `_table` всегда содержит только ~6-10
            # строк (это для preview), а `_graph` — реальный стакан до 100.
            parts = cmd.split()
            limit = None
            if len(parts) >= 2 and parts[1].isdigit():
                limit = int(parts[1])
            print()
            for line in render_full_stack_block(
                histogram, sym or "_", side="sell", limit=limit
            ):
                print(line)
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("b") and histogram is not None:
            parts = cmd.split()
            limit = None
            if len(parts) >= 2 and parts[1].isdigit():
                limit = int(parts[1])
            print()
            for line in render_full_stack_block(
                histogram, sym or "_", side="buy", limit=limit
            ):
                print(line)
            await ask("  Enter — продолжить: ")
        elif cmd.startswith("f"):
            # Floats viewer — листинги с inspect-link'ами.
            # `f` → 10 шт. (по умолчанию). `f 50` → 50 шт. на странице.
            parts = cmd.split()
            page_count = 10
            if len(parts) >= 2 and parts[1].isdigit():
                page_count = max(1, min(100, int(parts[1])))
            await _show_listings_with_floats(
                client, app_id, market_hash_name, currency_code, sym, ask,
                page_size=page_count,
            )
        else:
            print(f"  (не понял «{cmd}»)")


async def _show_listings_with_floats(  # noqa: PLR0912, PLR0915, C901
    client, app_id: int, market_hash_name: str, currency_code: int,
    sym: str, ask,
    *,
    page_size: int = 10,
) -> None:
    """Просмотр выставленных листингов: цена + inspect-URL + (опц.) флоат.

    `page_size` — сколько грузить за раз (1..100). Steam ограничивает count<=100.

    Команды внутри:
        h / help          — показать подробную справку
        n / Enter         — следующая страница
        p                 — предыдущая
        resolve / r       — попытаться вытащить флоаты через api.csfloat.com
                            (медленно: ~30 req/min — может уйти 1-3 мин)
        flt 0.2           — фильтр: показывать только листинги с float<0.2
                            (нужно сначала загрузить флоаты через resolve)
        flt off / reset   — сбросить фильтр
        q / Enter (пустой) — выход
    """
    print(f"\n=== ЛИСТИНГИ С ФЛОАТАМИ: {market_hash_name} ===")
    print(f"   Размер страницы: {page_size} шт. Steam max=100.")
    print("   Введи 'h' чтобы увидеть все команды.")

    start = 0
    page_listings: list[dict] = []
    page_floats: dict[str, tuple[float | None, int | None]] = {}
    total = 0
    min_float_filter: float | None = None
    loaded_start: int | None = None

    def _help():
        print(
            "\n  СПРАВКА:\n"
            "    n         — следующая страница (start += page_size)\n"
            "    p         — предыдущая\n"
            "    resolve   — попытаться вытащить флоаты для текущей страницы\n"
            "                через api.csfloat.com. Бесплатный публичный inspector\n"
            "                ~30 запросов/мин (по 2с между запросами).\n"
            "                Если у тебя нет интернета до csfloat — флоаты\n"
            "                покажутся как «—».\n"
            "    flt 0.2   — фильтр: показывать только листинги с float<0.2.\n"
            "                Требует, чтобы флоаты были загружены через 'resolve'.\n"
            "    flt off   — снять фильтр.\n"
            "    q или Enter (пустой) — выход."
        )

    while True:
        # Если ещё не грузили текущую страницу — грузим.
        if loaded_start != start:
            print(f"\n[..] Гружу листинги: start={start} count={page_size} ...")
            data = await _fetch_listings_page(
                client.session, app_id, market_hash_name,
                start=start, count=page_size, currency_code=currency_code,
            )
            if data is None:
                print("   [ERR] Не смог загрузить страницу листингов.")
                return
            total = int(data.get("total_count") or 0)
            page_listings = _parse_listings_render(data)
            loaded_start = start
            page_floats = {}  # сбрасываем кеш флоатов

        # Фильтр (только если флоаты есть).
        listings_to_show = page_listings
        if min_float_filter is not None and page_floats:
            listings_to_show = [
                lst for lst in page_listings
                if page_floats.get(lst["listing_id"], (None, None))[0] is not None
                and page_floats[lst["listing_id"]][0] < min_float_filter
            ]

        if not listings_to_show:
            if page_listings and min_float_filter is not None:
                print(f"\n(нет листингов с float<{min_float_filter} на этой странице)")
            else:
                print("\n(на этой странице нет листингов)")
        else:
            for line in render_listings_page(
                listings_to_show, sym=sym, start_idx=start, total=total,
                floats=page_floats if page_floats else None,
            ):
                print(line)

        page_num = start // page_size + 1
        total_pages = max(1, (total + page_size - 1) // page_size)
        cmd = (await ask(
            f"  n=след / p=пред / resolve=флоаты / flt 0.X / h=справка / "
            f"Enter=выход (стр. {page_num}/{total_pages}): "
        )).strip().lower()

        if cmd in ("", "q", "b", "exit", "quit"):
            return
        if cmd in ("h", "help", "?"):
            _help()
            continue
        if cmd in ("n", "next"):
            new_start = start + page_size
            if new_start >= total:
                print("  (это последняя страница)")
                continue
            start = new_start
        elif cmd in ("p", "prev"):
            new_start = max(0, start - page_size)
            if new_start == start:
                print("  (это первая страница)")
                continue
            start = new_start
        elif cmd in ("resolve", "r"):
            n = len(page_listings)
            import asyncio as _asyncio
            # Опционально — прокси-пул из simple.py (round-robin + failover).
            # Импортируем лениво чтобы не создавать circular import.
            proxy_rot = None
            try:
                import simple as _simple  # type: ignore[import-not-found]
                pool = _simple._load_proxy_pool()
                if pool:
                    use_p = (await ask(
                        f"  Использовать прокси-пул ({len(pool)} шт.) для запросов "
                        "csfloat? (y/N): "
                    )).strip().lower()
                    if use_p in ("y", "yes", "д", "да"):
                        proxy_rot = _simple._ProxyRotator(pool)
                        print(
                            f"  [..] csfloat: round-robin через {len(pool)} прокси "
                            "(rotate at каждом запросе, failover при ошибке)."
                        )
            except Exception:  # noqa: BLE001
                proxy_rot = None
            print(f"  [..] Резолвлю флоаты {n} листингов через api.csfloat.com ...")
            print("       (rate-limit ~30 req/min, пауза 2с между; может уйти 1-3 мин)")
            ok_count = 0
            for i, lst in enumerate(page_listings, 1):
                if not lst["inspect_url"]:
                    page_floats[lst["listing_id"]] = (None, None)
                    continue
                current_proxy = proxy_rot.current() if proxy_rot else None
                fl, sd = await _resolve_float_via_csfloat(
                    client.session, lst["inspect_url"], proxy=current_proxy,
                )
                page_floats[lst["listing_id"]] = (fl, sd)
                if fl is not None:
                    ok_count += 1
                    if proxy_rot:
                        proxy_rot.advance()
                else:
                    # Не вышло — отметим прокси как bad (если есть).
                    if proxy_rot:
                        proxy_rot.mark_bad()
                if i % 10 == 0 or i == n:
                    print(f"       {i}/{n} ...", flush=True)
                await _asyncio.sleep(2.0)
            print(f"  [OK] Резолвлено {ok_count} из {n} (остальные — None).")
            if ok_count == 0:
                print("       [!] Не удалось получить ни одного флоата. "
                      "Скорее всего csfloat.com недоступен / rate-limit'ит / "
                      "прокси-пул умер.")
        elif cmd.startswith("flt"):
            # «flt 0.2» — фильтр float<0.2; «flt off» — сбросить.
            arg = cmd[3:].strip()
            if arg in ("off", "reset", "", "none"):
                min_float_filter = None
                print("  [filter] сброшен.")
            else:
                try:
                    thr = float(arg.replace(",", "."))
                    if not 0.0 <= thr <= 1.0:
                        print("  flt: значение должно быть 0..1.")
                        continue
                    if not page_floats:
                        print("  [!] Сначала загрузи флоаты командой 'resolve'.")
                        continue
                    min_float_filter = thr
                    print(f"  [filter] показываю только float<{thr}.")
                except ValueError:
                    print(f"  flt: «{arg}» — не число. Пример: flt 0.2")
        else:
            print(f"  (не понял «{cmd}» — введи 'h' для справки)")