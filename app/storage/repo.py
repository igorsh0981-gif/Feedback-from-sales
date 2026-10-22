"""Репозиторий: кэш таблицы в памяти + пакетная запись изменений в Google Sheets.

- Чтение для дашборда и анализа идёт из памяти (без квот API).
- Изменения копятся и пишутся пакетами: append для новых строк, batch update для изменённых.
- Перед обновлением строки её номер ищется по ключевому столбцу листа, поэтому ручная
  сортировка или вставка строк в таблице не приводит к записи «не в ту строку».
- Если запись не удалась, изменения остаются в очереди и уйдут при следующей фиксации.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable

from ..constants import COMMIT_ORDER, DEFAULT_SETTINGS, SCHEMA
from .backends import Backend

log = logging.getLogger(__name__)

_ISSUE_NUM_RE = re.compile(r"(\d+)$")


class Repo:
    def __init__(self, backend: Backend):
        self.backend = backend
        self.headers: dict[str, list[str]] = {}
        self.tables: dict[str, dict[str, dict]] = {tab: {} for tab in SCHEMA}
        self._new: dict[str, list[str]] = {tab: [] for tab in SCHEMA}
        self._dirty: dict[str, set[str]] = {tab: set() for tab in SCHEMA}
        self._commit_lock = asyncio.Lock()
        self._seq = 0

    # ------------------------------------------------------------------ загрузка
    async def init(self) -> None:
        await asyncio.to_thread(self.backend.ensure_tabs, {t: SCHEMA[t]["cols"] for t in SCHEMA})
        await self.load()
        for key, value in DEFAULT_SETTINGS.items():
            if not self.get("Settings", key):
                self.insert("Settings", {"key": key, "value": value})
        await self.commit()

    async def load(self) -> None:
        data = await asyncio.to_thread(self.backend.read_all, list(SCHEMA))
        tables: dict[str, dict[str, dict]] = {}
        for tab in SCHEMA:
            rows = data.get(tab) or []
            header = [h.strip() for h in rows[0]] if rows else list(SCHEMA[tab]["cols"])
            for col in SCHEMA[tab]["cols"]:
                if col not in header:
                    header.append(col)
            self.headers[tab] = header
            table: dict[str, dict] = {}
            for row in rows[1:]:
                if not any(str(c).strip() for c in row):
                    continue
                rec = {col: (str(row[i]) if i < len(row) else "") for i, col in enumerate(header)}
                key = self._key(tab, rec)
                if key in table:
                    log.warning("Дубль ключа %s в %s — используется первая строка", key, tab)
                    continue
                table[key] = rec
            tables[tab] = table
        self.tables = tables
        self._new = {tab: [] for tab in SCHEMA}
        self._dirty = {tab: set() for tab in SCHEMA}

    async def reload(self) -> None:
        """Перечитать таблицу (после ручных правок в Sheets). Сначала фиксирует свои изменения."""
        await self.commit()
        async with self._commit_lock:
            await self.load()

    # ------------------------------------------------------------------ ключи
    def _key(self, tab: str, rec: dict) -> str:
        keys = SCHEMA[tab]["key"]
        if not keys:
            self._seq += 1
            return f"#{self._seq}"
        return "|".join(str(rec.get(k, "")).strip() for k in keys)

    @staticmethod
    def make_key(*parts) -> str:
        return "|".join(str(p).strip() for p in parts)

    # ------------------------------------------------------------------ чтение
    def rows(self, tab: str) -> list[dict]:
        return list(self.tables[tab].values())

    def get(self, tab: str, *key_parts) -> dict | None:
        return self.tables[tab].get(self.make_key(*key_parts))

    def setting(self, key: str, default: str = "") -> str:
        rec = self.get("Settings", key)
        return rec["value"] if rec else DEFAULT_SETTINGS.get(key, default)

    def setting_on(self, key: str) -> bool:
        return self.setting(key) in {"1", "true", "yes", "да"}

    def next_issue_id(self) -> str:
        top = 0
        for key in self.tables["Issues"]:
            m = _ISSUE_NUM_RE.search(key)
            if m:
                top = max(top, int(m.group(1)))
        return f"FP-{top + 1:03d}"

    # ------------------------------------------------------------------ изменение
    def insert(self, tab: str, rec: dict) -> dict:
        clean = {col: "" for col in self.headers.get(tab, SCHEMA[tab]["cols"])}
        clean.update({k: ("" if v is None else str(v)) for k, v in rec.items()})
        key = self._key(tab, clean)
        if key in self.tables[tab]:
            self.update(tab, key, clean)
            return self.tables[tab][key]
        self.tables[tab][key] = clean
        self._new[tab].append(key)
        return clean

    def update(self, tab: str, key: str, changes: dict) -> dict:
        """Меняет поля записи. Возвращает {поле: старое значение} только для изменённых полей."""
        rec = self.tables[tab].get(key)
        if rec is None:
            raise KeyError(f"{tab}: нет записи {key}")
        old: dict[str, str] = {}
        for field, value in changes.items():
            value = "" if value is None else str(value)
            if rec.get(field, "") != value:
                old[field] = rec.get(field, "")
                rec[field] = value
        if old:
            new_key = self._key(tab, rec) if SCHEMA[tab]["key"] else key
            if new_key != key:
                raise ValueError("Ключевые поля менять нельзя")
            if key not in self._new[tab]:
                self._dirty[tab].add(key)
        return old

    def set_setting(self, key: str, value: str) -> dict:
        if self.get("Settings", key):
            return self.update("Settings", key, {"value": value})
        self.insert("Settings", {"key": key, "value": value})
        return {"value": ""}

    @property
    def pending_changes(self) -> int:
        return sum(len(v) for v in self._new.values()) + sum(len(v) for v in self._dirty.values())

    # ------------------------------------------------------------------ фиксация
    def _values(self, tab: str, rec: dict) -> list[str]:
        return [rec.get(col, "") for col in self.headers[tab]]

    def _key_indexes(self, tab: str) -> list[int]:
        return [self.headers[tab].index(k) for k in SCHEMA[tab]["key"]]

    async def delete_where(self, tabs_preds: dict) -> dict[str, int]:
        """Удаляет строки по условию {вкладка: функция(запись) -> bool}. Сначала фиксирует изменения,
        строки ищет по актуальному содержимому листа, после удаления перечитывает таблицу."""
        await self.commit()
        removed: dict[str, int] = {}
        async with self._commit_lock:
            data = await asyncio.to_thread(self.backend.read_all, list(tabs_preds))
            for tab, pred in tabs_preds.items():
                rows = data.get(tab) or []
                if not rows:
                    continue
                header = [h.strip() for h in rows[0]]
                hit = [i + 1 for i, row in enumerate(rows) if i > 0 and any(str(c).strip() for c in row)
                       and pred({col: (str(row[j]) if j < len(row) else "") for j, col in enumerate(header)})]
                if hit:
                    await asyncio.to_thread(self.backend.delete_rows, tab, hit)
                removed[tab] = len(hit)
            await self.load()
        return removed

    async def commit(self) -> int:
        """Пишет накопленные изменения. Возвращает число записанных строк."""
        async with self._commit_lock:
            new = {tab: list(keys) for tab, keys in self._new.items() if keys}
            dirty = {tab: set(keys) - set(new.get(tab, [])) for tab, keys in self._dirty.items() if keys}
            dirty = {t: k for t, k in dirty.items() if k}
            if not new and not dirty:
                return 0
            for tab in new:
                self._new[tab] = []
            for tab in self._dirty:
                self._dirty[tab] = set()

            appends: dict[str, list[tuple[str, list[str]]]] = {
                tab: [(k, self._values(tab, self.tables[tab][k])) for k in keys if k in self.tables[tab]]
                for tab, keys in new.items()
            }
            updates: dict[str, list[tuple[str, list[str]]]] = {
                tab: [(k, self._values(tab, self.tables[tab][k])) for k in keys if k in self.tables[tab]]
                for tab, keys in dirty.items()
            }
            done: set[tuple[str, str]] = set()  # (tab, "append"|"update")
            written = 0
            try:
                located: dict[str, list[tuple[int, list[str]]]] = {}
                if updates:
                    cols = await asyncio.to_thread(
                        self.backend.read_columns, {tab: self._key_indexes(tab) for tab in updates}
                    )
                    for tab, items in updates.items():
                        columns = cols.get(tab, [])
                        length = max((len(c) for c in columns), default=0)
                        row_of: dict[str, int] = {}
                        for i in range(1, length):  # строка 0 — заголовок
                            key = "|".join((c[i] if i < len(c) else "").strip() for c in columns)
                            row_of.setdefault(key, i + 1)
                        for key, values in items:
                            row = row_of.get(key)
                            if row:
                                located.setdefault(tab, []).append((row, values))
                            else:
                                # Строку удалили из листа вручную — добавим заново.
                                appends.setdefault(tab, []).append((key, values))
                for tab in COMMIT_ORDER:
                    if appends.get(tab):
                        await asyncio.to_thread(self.backend.append, tab, [v for _, v in appends[tab]])
                        done.add((tab, "append"))
                        written += len(appends[tab])
                    if located.get(tab):
                        await asyncio.to_thread(self.backend.update, tab, located[tab])
                        done.add((tab, "update"))
                        written += len(located[tab])
                return written
            except Exception:
                for tab, items in appends.items():
                    if (tab, "append") not in done:
                        for key, _ in items:
                            if key in new.get(tab, []):
                                if key not in self._new[tab]:
                                    self._new[tab].append(key)
                            else:
                                self._dirty[tab].add(key)
                for tab, items in updates.items():
                    if (tab, "update") not in done:
                        for key, _ in items:
                            self._dirty[tab].add(key)
                log.exception("Запись в таблицу не удалась, изменения остаются в очереди")
                raise


def records_where(rows: Iterable[dict], **conditions) -> list[dict]:
    return [r for r in rows if all(r.get(k) == v for k, v in conditions.items())]
