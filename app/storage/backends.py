"""Бэкенды хранения: Google Sheets (рабочий) и память (тесты, демо).

Интерфейс синхронный — Repo вызывает его через asyncio.to_thread.
Все значения пишутся как RAW: текст участника, начинающийся с «=», не станет формулой.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)

CELL_LIMIT = 45000


def _clip(row: list[str]) -> list[str]:
    return [v if len(v) <= CELL_LIMIT else v[: CELL_LIMIT - 1] + "…" for v in row]


class Backend(Protocol):
    def ensure_tabs(self, schema: dict[str, list[str]]) -> None: ...
    def read_all(self, tabs: list[str]) -> dict[str, list[list[str]]]: ...
    def read_columns(self, requests: dict[str, list[int]]) -> dict[str, list[list[str]]]: ...
    def append(self, tab: str, rows: list[list[str]]) -> None: ...
    def update(self, tab: str, updates: list[tuple[int, list[str]]]) -> None: ...
    def delete_rows(self, tab: str, row_numbers: list[int]) -> None: ...
    def ping(self) -> None: ...


class MemoryBackend:
    """Таблица в памяти с тем же поведением, что у листа: строка 1 — заголовок."""

    def __init__(self) -> None:
        self.sheets: dict[str, list[list[str]]] = {}
        self.calls: list[str] = []
        self.fail_next = 0

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError(f"simulated failure in {op}")

    def ensure_tabs(self, schema: dict[str, list[str]]) -> None:
        for tab, cols in schema.items():
            sheet = self.sheets.setdefault(tab, [])
            if not sheet:
                sheet.append(list(cols))
            else:
                header = sheet[0]
                for col in cols:
                    if col not in header:
                        header.append(col)

    def read_all(self, tabs: list[str]) -> dict[str, list[list[str]]]:
        self._maybe_fail("read_all")
        return {t: [list(r) for r in self.sheets.get(t, [])] for t in tabs}

    def read_columns(self, requests: dict[str, list[int]]) -> dict[str, list[list[str]]]:
        self._maybe_fail("read_columns")
        out = {}
        for tab, cols in requests.items():
            rows = self.sheets.get(tab, [])
            out[tab] = [[(r[c] if c < len(r) else "") for r in rows] for c in cols]
        return out

    def append(self, tab: str, rows: list[list[str]]) -> None:
        self._maybe_fail("append:" + tab)
        self.sheets.setdefault(tab, []).extend([list(r) for r in _clip_all(rows)])

    def delete_rows(self, tab: str, row_numbers: list[int]) -> None:
        self._maybe_fail("delete:" + tab)
        sheet = self.sheets.get(tab, [])
        for row in sorted(set(row_numbers), reverse=True):
            if 2 <= row <= len(sheet):
                del sheet[row - 1]

    def update(self, tab: str, updates: list[tuple[int, list[str]]]) -> None:
        self._maybe_fail("update:" + tab)
        sheet = self.sheets[tab]
        for row_number, values in updates:
            sheet[row_number - 1] = list(_clip(values))

    def ping(self) -> None:
        self._maybe_fail("ping")


def _clip_all(rows: list[list[str]]) -> list[list[str]]:
    return [_clip(r) for r in rows]


class SheetsBackend:
    """Google Sheets через gspread и сервисный аккаунт."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, sheet_id: str, service_account_json: str, attempts: int = 3):
        import gspread

        info = json.loads(service_account_json)
        self._gspread = gspread
        self.gc = gspread.service_account_from_dict(info)
        self.sheet_id = sheet_id
        self.attempts = attempts
        self.sh = self._retry(lambda: self.gc.open_by_key(sheet_id))
        self._ws: dict = {}

    def _worksheet(self, tab: str):
        ws = self._ws.get(tab)
        if ws is None:
            ws = self._retry(lambda: self.sh.worksheet(tab))
            self._ws[tab] = ws
        return ws

    # --- повторы при 429/5xx и сетевых ошибках -------------------------------------------------
    def _retry(self, fn):
        delay = 2.0
        for attempt in range(1, self.attempts + 1):
            try:
                return fn()
            except self._gspread.exceptions.APIError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in self.RETRY_STATUSES or attempt == self.attempts:
                    raise
                log.warning("Sheets API %s, попытка %s/%s", status, attempt, self.attempts)
            except (ConnectionError, TimeoutError, OSError) as exc:
                if attempt == self.attempts:
                    raise
                log.warning("Sheets network error %s, попытка %s/%s", exc, attempt, self.attempts)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    @staticmethod
    def _q(tab: str) -> str:
        return "'" + tab.replace("'", "''") + "'"

    @staticmethod
    def _col(index: int) -> str:
        n, out = index + 1, ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    def ensure_tabs(self, schema: dict[str, list[str]]) -> None:
        existing = {ws.title: ws for ws in self._retry(self.sh.worksheets)}
        self._ws.update(existing)
        for tab, cols in schema.items():
            ws = existing.get(tab)
            if ws is None:
                ws = self._retry(lambda: self.sh.add_worksheet(title=tab, rows=1000, cols=max(len(cols), 6)))
                self._ws[tab] = ws
                self._retry(lambda: ws.update([cols], "A1", value_input_option="RAW"))
                log.info("Создана вкладка %s", tab)
                continue
            header = self._retry(lambda: ws.row_values(1))
            missing = [c for c in cols if c not in header]
            if missing or not header:
                new_header = (header or []) + missing
                if ws.col_count < len(new_header):
                    self._retry(lambda: ws.add_cols(len(new_header) - ws.col_count))
                self._retry(lambda: ws.update([new_header], "A1", value_input_option="RAW"))
                log.info("Вкладка %s: добавлены столбцы %s", tab, missing)

    def read_all(self, tabs: list[str]) -> dict[str, list[list[str]]]:
        resp = self._retry(lambda: self.sh.values_batch_get([self._q(t) for t in tabs]))
        out: dict[str, list[list[str]]] = {}
        for tab, vr in zip(tabs, resp.get("valueRanges", [])):
            out[tab] = [[str(c) for c in row] for row in vr.get("values", [])]
        return out

    def read_columns(self, requests: dict[str, list[int]]) -> dict[str, list[list[str]]]:
        ranges, order = [], []
        for tab, cols in requests.items():
            for c in cols:
                letter = self._col(c)
                ranges.append(f"{self._q(tab)}!{letter}:{letter}")
                order.append(tab)
        if not ranges:
            return {}
        resp = self._retry(lambda: self.sh.values_batch_get(ranges, params={"majorDimension": "COLUMNS"}))
        out: dict[str, list[list[str]]] = {t: [] for t in requests}
        for tab, vr in zip(order, resp.get("valueRanges", [])):
            values = vr.get("values", [])
            out[tab].append([str(v) for v in (values[0] if values else [])])
        return out

    def append(self, tab: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        ws = self._worksheet(tab)
        self._retry(
            lambda: ws.append_rows(
                _clip_all(rows), value_input_option="RAW", insert_data_option="INSERT_ROWS", table_range="A1"
            )
        )

    def update(self, tab: str, updates: list[tuple[int, list[str]]]) -> None:
        if not updates:
            return
        data = [{"range": f"{self._q(tab)}!A{row}", "values": [_clip(values)]} for row, values in updates]
        self._retry(lambda: self.sh.values_batch_update({"valueInputOption": "RAW", "data": data}))

    def delete_rows(self, tab: str, row_numbers: list[int]) -> None:
        """Удаляет строки (номера с 1, заголовок не трогается) одним запросом, снизу вверх."""
        rows = sorted({r for r in row_numbers if r >= 2}, reverse=True)
        if not rows:
            return
        sheet_id = self._worksheet(tab).id
        requests = [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                   "startIndex": r - 1, "endIndex": r}}} for r in rows]
        self._retry(lambda: self.sh.batch_update({"requests": requests}))

    def ping(self) -> None:
        self._retry(lambda: self.sh.values_get(f"{self._q('Settings')}!A1:B1"))
