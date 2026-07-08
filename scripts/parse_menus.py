#!/usr/bin/env python3
"""Parse menu (献立表) PDFs into a structured per-day dish list.

Two table formats exist:
  - Standard (尾西/木曽川 etc.): day in row[0], all dishes in row[2] as newline-separated text
  - 東浅井: day in row[1], each dish in its own row, dish cell has furigana as first line

Output: data/menu_days.json, a flat list of day records.
"""
import json, re
from pathlib import Path
import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
LIST_JSON = ROOT / "data" / "menu_pdf_list.json"
DL_DIR = ROOT / "downloads" / "menus"
OUT = ROOT / "data" / "menu_days.json"


def normalize_cell(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def split_lines(s: str):
    return [ln.strip() for ln in s.split("\n") if ln.strip()]


# ---------------------------------------------------------------------------
# Standard format (尾西・木曽川 etc.): day in row[0]
# ---------------------------------------------------------------------------

def is_day_row_standard(row):
    """Row[0] is a 1-2 digit day number."""
    if not row or not row[0]:
        return False
    return bool(re.fullmatch(r"\d{1,2}", str(row[0]).strip()))


def extract_day_records_standard(table):
    for row in table:
        if not is_day_row_standard(row):
            continue
        day = int(row[0].strip())
        weekday = normalize_cell(row[1] if len(row) > 1 else "")
        dish_text = normalize_cell(row[2] if len(row) > 2 else "")
        dishes = split_lines(dish_text)
        ing_cells = []
        for c in row[3:-2] if len(row) > 5 else row[3:]:
            s = normalize_cell(c)
            if s:
                ing_cells.append(s)
        ingredients_blob = " ".join(ing_cells)
        kcal = protein = ""
        if len(row) >= 11:
            m = re.search(r"\d+", normalize_cell(row[-2]))
            if m:
                kcal = m.group(0)
            m = re.search(r"[\d.]+", normalize_cell(row[-1]))
            if m:
                protein = m.group(0)
        yield {
            "day": day, "weekday": weekday, "dishes": dishes,
            "ingredients": ingredients_blob, "kcal": kcal, "protein_g": protein,
        }


# ---------------------------------------------------------------------------
# 東浅井 format: day in row[1], each dish in its own row, furigana as first line
# ---------------------------------------------------------------------------

def _count_kanji(s):
    return sum(1 for c in s if "一" <= c <= "鿿")


def _extract_dish_name_higashiazai(cell):
    """Extract dish name from a cell that may have furigana as the first line(s).

    Strategy: skip leading lines whose kanji count is less than the maximum kanji
    count found anywhere in the cell.  This handles cases like:
      - "ぎゅうにゅう はしづかいの日 / 牛乳" where 日 fools ratio tests
      - "こんだて / ぎゅうにゅう / 牛乳" with 3-line furigana
      - "あ / こまつなのこんぶ和え" where the dish has very few kanji
    Falls back to the first line when the whole cell is hiragana (e.g., てりどり).
    """
    if not cell:
        return None
    lines = [ln.strip() for ln in str(cell).split("\n") if ln.strip()]
    if not lines:
        return None

    # Filter sub-items (e.g. ＜とり団子＞) before computing max-kanji
    content_lines = [ln for ln in lines if not re.match(r"^[<〈＜〔【]", ln)]
    if not content_lines:
        content_lines = lines

    kanji_counts = [_count_kanji(ln) for ln in content_lines]
    max_kanji = max(kanji_counts, default=0)

    if max_kanji == 0:
        result = content_lines[0]  # all-hiragana dish like てりどり
    else:
        result = next(
            (ln for ln, cnt in zip(content_lines, kanji_counts) if cnt >= max_kanji),
            content_lines[0],
        )

    # Strip leading non-word symbols like ◎ ☆ ★ ● ＊
    result = re.sub(r"^[^぀-ヿ一-鿿\w]+", "", result)

    # Skip footnote-like strings (Japanese sentences end with 。)
    if "。" in result:
        return None

    return result or None


def _get_kcal(row):
    """Return the first cell value >200 found anywhere in the row, else 0.

    Original code only checked row[-2], but page-2 PDFs can have extra columns
    that push the kcal column away from the end.  Scanning all cells is safe
    because ingredient cells only contain Japanese text or small float values
    (protein g < 200), so a float > 200 is always a kcal total.
    """
    if not row:
        return 0
    for cell in row:
        if cell:
            try:
                v = float(str(cell).strip())
                if v > 200:
                    return v
            except (ValueError, TypeError):
                pass
    return 0


def _day_column(table):
    """Return the column index where the lone 1-2 digit day numbers live.

    Page 1 and page 2 of the same 東浅井 PDF can have different column offsets
    (pdfplumber inserts an extra leading column on some pages), so the day
    column is not always col[1].  Detect it from the first lone-digit cell.
    Defaults to 1 (the common page-1 layout) when nothing is found.
    """
    for row in table:
        if not row:
            continue
        for idx, cell in enumerate(row):
            if cell and re.fullmatch(r"\d{1,2}", str(cell).strip()):
                return idx
    return 1


def extract_day_records_higashiazai(table):
    """Parse 東浅井 format using kcal rows as day-boundary markers.

    Each day's block:
      row[k-1]  = ご飯/パン  (first dish, no day number)
      row[k]    = 牛乳 with large kcal  (kcal marker)
      row[k+1…] = main dishes; one of them has the day number in col[day_col]

    Column offsets are detected per-table (day_col, weekday_col=day_col+1,
    dish_col=day_col+2) because page 2 is shifted one column to the right.
    """
    day_col = _day_column(table)
    weekday_col = day_col + 1
    dish_col = day_col + 2
    # Pass 1: find kcal row indices (牛乳 rows with total kcal > 200)
    kcal_indices = [
        i for i, row in enumerate(table)
        if row and len(row) >= 2 and _get_kcal(row) > 0
    ]
    if not kcal_indices:
        return

    # Pass 2: iterate day blocks
    for n, k in enumerate(kcal_indices):
        start = k  # k IS the ご飯 row (kcal is on the ご飯 row)
        if n + 1 < len(kcal_indices):
            end = kcal_indices[n + 1] - 1  # last row before next day's ご飯
        else:
            end = len(table) - 1

        day_num = None
        weekday = ""
        dishes = []
        ing_parts = []

        for i in range(start, end + 1):
            row = table[i]
            if not row or len(row) <= dish_col:
                continue

            day_cell = str(row[day_col]).strip() if row[day_col] else ""
            weekday_cell = str(row[weekday_col]).strip() if row[weekday_col] else ""
            dish_cell = row[dish_col]

            # Day anchor: the day column holds a 1-2 digit number
            if re.fullmatch(r"\d{1,2}", day_cell):
                day_num = int(day_cell)
                weekday = weekday_cell

            # Dish name
            dish = _extract_dish_name_higashiazai(dish_cell)
            if dish and not re.match(r"^[<〈＜〔【]", dish):
                dishes.append(dish)

            # Ingredients: columns after the dish column up to the kcal columns.
            # Skip pure kcal-like numbers (>200) that can leak in on shifted pages.
            for c in row[dish_col + 1:-2]:
                s = str(c).strip() if c else ""
                if not s or s == "None":
                    continue
                try:
                    if float(s) > 200:
                        continue
                except ValueError:
                    pass
                ing_parts.append(s)

        if day_num is not None and dishes:
            yield {
                "day": day_num, "weekday": weekday, "dishes": dishes,
                "ingredients": " ".join(ing_parts), "kcal": "", "protein_g": "",
            }


# ---------------------------------------------------------------------------
# Format detection and unified entry point
# ---------------------------------------------------------------------------

def detect_format(table):
    """Return 'standard' or 'higashiazai' based on table structure.

    The first lone 1-2 digit day cell tells them apart: standard packs the day
    in col[0]; 東浅井 puts it in col[1] (page 1) or col[2] (page 2, shifted).
    Any day column other than 0 means 東浅井.
    """
    for row in table:
        if not row:
            continue
        for idx, cell in enumerate(row):
            if cell and re.fullmatch(r"\d{1,2}", str(cell).strip()):
                return "standard" if idx == 0 else "higashiazai"
    return "standard"


def extract_day_records(pdf_path: Path):
    """Yield day-record dicts from a single menu PDF, auto-detecting format."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for t in tables:
                fmt = detect_format(t)
                if fmt == "higashiazai":
                    yield from extract_day_records_higashiazai(t)
                else:
                    yield from extract_day_records_standard(t)


def main():
    menu_records = json.loads(LIST_JSON.read_text(encoding="utf-8"))
    out = []
    for i, m in enumerate(menu_records, 1):
        pdf = DL_DIR / m["filename"]
        if not pdf.exists():
            continue
        count_before = len(out)
        try:
            for day in extract_day_records(pdf):
                date = f"{m['year']:04d}-{m['month']:02d}-{day['day']:02d}"
                out.append({
                    "date": date,
                    "school_type": m["school_type"],
                    "area": m["area"],
                    **day,
                    "source_pdf": m["url"],
                })
        except Exception as e:
            print(f"[{i}] error parsing {m['filename']}: {e}")
        count = len(out) - count_before
        print(f"[{i}] {m['filename']}: {count} days")

    # Deduplicate
    seen = set()
    unique = []
    for r in out:
        key = (r["date"], r["school_type"], r["area"], "|".join(r["dishes"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    unique.sort(key=lambda r: (r["date"], r["school_type"], r["area"]))

    OUT.write_text(json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(unique)} day-records -> {OUT}")
    print(f"  unique dates: {len(set(r['date'] for r in unique))}")
    print(f"  total dishes: {sum(len(r['dishes']) for r in unique)}")


if __name__ == "__main__":
    main()
