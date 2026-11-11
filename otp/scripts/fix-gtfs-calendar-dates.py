#!/usr/bin/env python3
"""
Исправить невалидные end_date в calendar.txt внутри GTFS-фида.

Проблема фида ОРГП (Санкт-Петербург)
-----------------------------------
Во всех 1013 записях calendar.txt стоит end_date=20191230, тогда как
start_date лежит в 2025-2026. Получается инвертированный интервал действия,
на котором OTP падает:

    IllegalArgumentException: Invalid range, the end 2019-12-30
                            is before the start 2026-08-01

Причина: колонка «конец периода» в исходной БД не заполняется, и при экспорте
в неё кладётся константа-заглушка.

Что делает скрипт
-----------------
Заменяет end_date ТОЛЬКО в строках, где end_date < start_date. Корректные
записи не трогаются, поэтому скрипт идемпотентен и безопасен при повторном
запуске.

Новая end_date = максимум из (все start_date, все даты calendar_dates.txt)
плюс несколько лет запаса. Запас нужен, потому что расписание действует и
после выгрузки фида, а обрезка по последней известной дате лишила бы OTP
транспорта в запросах на «будущее».

Сохранность остальных байтов
----------------------------
calendar.txt читается и разделяется СТРОГО по байту 0x0A, в кодировке latin-1
(байт <-> символ 1:1). Это принципиально: service_name содержит двойную UTF-8
кодировку кириллицы, где буква «х» даёт байт 0x85, который str.splitlines()
считает разделителем строк (NEL). Обычный splitlines() разрезал бы записи
посередине слов.

Остальные 13 файлов фида переупаковываются побайтово и не изменяются, так что
нестандартная кодировка (например двойное UTF-8 в agency.txt) остаётся ровно
такой, какой была скачана.

Использование
-------------
    python3 scripts/fix-gtfs-calendar-dates.py data/spb-orgp.gtfs.zip --dry-run
    python3 scripts/fix-gtfs-calendar-dates.py data/spb-orgp.gtfs.zip
    python3 scripts/fix-gtfs-calendar-dates.py data/spb-orgp.gtfs.zip --end-date 20301231
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from datetime import date, timedelta

# start_date и end_date идут двумя 8-значными полями подряд после флагов дней.
# Завершающая запятая не обязательна: end_date может быть последней колонкой.
DATE_PAIR = re.compile(r",(\d{8}),(\d{8})(?=,|$)")


def to_date(value: str) -> date | None:
    """Распаковать GTFS-дату YYYYMMDD; None, если это не корректная дата."""
    if not re.fullmatch(r"\d{8}", value):
        return None
    year, month, day = int(value[:4]), int(value[4:6]), int(value[6:8])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def split_records(raw: bytes) -> list[bytes]:
    """Разделить строго по 0x0A, не трогая прочие управляющие байты."""
    return raw.split(b"\n")


def find_max_date(zf: zipfile.ZipFile) -> date:
    """Максимальная дата, реально присутствующая в расписаниях."""
    import csv
    import io

    latest = None

    if "calendar.txt" in zf.namelist():
        raw = zf.read("calendar.txt")
        for part in split_records(raw):
            for m in DATE_PAIR.finditer(part.decode("latin-1")):
                for value in m.groups():
                    d = to_date(value)
                    if d and (latest is None or d > latest):
                        latest = d

    if "calendar_dates.txt" in zf.namelist():
        # парсим по колонке date, чтобы не спутать с 8-значными service_id
        text = zf.read("calendar_dates.txt").decode("latin-1")
        for row in csv.DictReader(io.StringIO(text)):
            d = to_date((row.get("date") or "").strip())
            if d and (latest is None or d > latest):
                latest = d

    if latest is None:
        raise SystemExit("ERROR: в фиде не найдено ни одной даты в calendar*.txt")
    return latest


def fix_calendar(raw: bytes, new_end: str) -> tuple[bytes, int, int, int]:
    """
    Вернуть (исправленные байты, исправлено, пропущено, всего записей).
    Исправляется только инверсия end_date < start_date.
    """
    parts = split_records(raw)
    out = []
    fixed = 0
    skipped = 0
    records = 0

    for idx, part in enumerate(parts):
        # parts[0] — заголовок; пустые хвосты (в т.ч. завершающий перевод строки)
        if idx == 0 or not part.strip():
            out.append(part)
            continue

        text = part.decode("latin-1")
        matches = list(DATE_PAIR.finditer(text))
        if len(matches) != 1:
            sys.stderr.write(
                f"WARN: запись {idx}: ожидалась одна пара дат, найдено "
                f"{len(matches)} — пропускаю\n"
            )
            skipped += 1
            out.append(part)
            continue

        records += 1
        m = matches[0]
        start_raw, end_raw = m.group(1), m.group(2)
        start_d, end_d = to_date(start_raw), to_date(end_raw)
        if start_d is None or end_d is None:
            sys.stderr.write(f"WARN: запись {idx}: даты не распознаны — пропускаю\n")
            skipped += 1
            out.append(part)
            continue
        if end_d >= start_d:
            out.append(part)  # интервал корректный, не трогаем
            continue

        s, e = m.span()
        # Разделитель после end_date не входит в совпадение (lookahead),
        # поэтому текст после span сохраняется как есть.
        rebuilt = text[:s] + f",{start_raw},{new_end}" + text[e:]
        out.append(rebuilt.encode("latin-1"))
        fixed += 1

    return b"\n".join(out), fixed, skipped, records


def rewrite_zip(path: str, new_calendar: bytes) -> None:
    """
    Переупаковать фид: calendar.txt заменён, остальные записи перенесены
    побайтово с сохранением compress_type и метаданных.
    """
    tmp_path = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(
        tmp_path, "w", allowZip64=True
    ) as zout:
        for item in zin.infolist():
            data = new_calendar if item.filename == "calendar.txt" else zin.read(item.filename)
            info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            info.compress_type = item.compress_type
            info.external_attr = item.external_attr
            zout.writestr(info, data)
    os.replace(tmp_path, path)


def count_inversions(raw: bytes) -> int:
    """Сколько записей имеют end_date < start_date."""
    bad = 0
    for idx, part in enumerate(split_records(raw)):
        if idx == 0 or not part.strip():
            continue
        for m in DATE_PAIR.finditer(part.decode("latin-1")):
            sd, ed = to_date(m.group(1)), to_date(m.group(2))
            if sd and ed and ed < sd:
                bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Исправить инвертированные end_date в GTFS calendar.txt"
    )
    ap.add_argument("feed", help="путь к GTFS-фиду (*.gtfs.zip)")
    ap.add_argument(
        "--end-date",
        metavar="YYYYMMDD",
        help="явная новая end_date (по умолчанию — автоподбор)",
    )
    ap.add_argument(
        "--extend-years",
        type=int,
        default=3,
        help="лет запаса при автоподборе (по умолчанию 3)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать изменения, файл не трогать",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.feed):
        sys.stderr.write(f"ERROR: файл не найден: {args.feed}\n")
        return 1

    with zipfile.ZipFile(args.feed) as zf:
        names = zf.namelist()
        if "calendar.txt" not in names:
            sys.stderr.write("ERROR: в фиде нет calendar.txt — исправлять нечего\n")
            return 1

        latest = find_max_date(zf)

        if args.end_date:
            if to_date(args.end_date) is None:
                sys.stderr.write("ERROR: --end-date должен быть в формате YYYYMMDD\n")
                return 1
            new_end = args.end_date
            source = "явная дата из параметров"
        else:
            new_end = (latest + timedelta(days=365 * args.extend_years)).strftime("%Y%m%d")
            source = f"автоподбор: max-дата {latest:%Y-%m-%d} + {args.extend_years} г."

        raw = zf.read("calendar.txt")

    print(f"Фид:                {args.feed}")
    print(f"Файлов в фиде:      {len(names)}")
    print(f"Макс. дата в фиде:  {latest:%Y-%m-%d}")
    print(f"Новая end_date:     {new_end}  ({source})")

    new_raw, fixed, skipped, records = fix_calendar(raw, new_end)

    print(f"Записей calendar.txt: {records}")
    print(f"Инверсий до правки:  {count_inversions(raw)}")
    print(f"Будет исправлено:    {fixed}")
    if skipped:
        print(f"Пропущено:           {skipped}")

    if args.dry_run:
        print("\n--dry-run: файл не изменён")
        return 0

    if fixed == 0:
        print("\nНечего исправлять — все интервалы корректны")
        return 0

    rewrite_zip(args.feed, new_raw)
    print(f"\nЗаписано: {args.feed}")

    with zipfile.ZipFile(args.feed) as zf:
        after = count_inversions(zf.read("calendar.txt"))
        others_ok = all(
            zf.read(n) == zipfile.ZipFile(
                os.environ.get("GTFS_ORIG", "/dev/null"), "r"
            ).read(n)
            for n in zf.namelist()
            if n != "calendar.txt"
        ) if os.environ.get("GTFS_ORIG") and os.path.isfile(os.environ.get("GTFS_ORIG", "")) else None
    print(f"Проверка: инверсий после правки — {after}")
    if others_ok is not None:
        print(f"Проверка: прочие файлы идентичны оригиналу — {others_ok}")

    return 0 if after == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
