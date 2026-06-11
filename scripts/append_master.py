"""Append rows to the master Word list, preserving table formatting.

Usage: python append_master.py <master_docx> <rows_json>

rows_json is a JSON list of {"tab": <int|str>, "title": str, "vehicle": str}
with an optional "angle" key (a 1-2 sentence summary of the script's angle).
If any row carries an angle, the master table is upgraded in place from
3 columns to 4 (header "Angle / Summary"); existing rows get empty cells.

If the master docx does not exist, it is created from the bundled
assets/master_template.docx (which contains the heading, the styled header
row, and one placeholder data row used as the formatting source).

New rows are cloned from the last data row so borders, fonts, shading and
column widths carry over exactly.
"""
import sys, os, json, shutil, copy
import docx
from docx.oxml.ns import qn

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "assets", "master_template.docx")
PLACEHOLDER = "__TAB__"
ANGLE_HEADER = "Story Context"
WIDTHS_4 = [600, 4060, 2100, 2600]  # dxa, sums to 9360 like the 3-col layout


def set_cell_text(cell, text):
    # keep the first run of the first paragraph (carries formatting), drop the rest
    para = cell.paragraphs[0]
    for extra in cell.paragraphs[1:]:
        extra._element.getparent().remove(extra._element)
    runs = para.runs
    if not runs:
        para.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r._element.getparent().remove(r._element)


def set_cell_width(tc, dxa):
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = tc.makeelement(qn("w:tcPr"), {})
        tc.insert(0, tcPr)
    tcW = tcPr.find(qn("w:tcW"))
    if tcW is None:
        tcW = tcPr.makeelement(qn("w:tcW"), {})
        tcPr.append(tcW)
    tcW.set(qn("w:w"), str(dxa))
    tcW.set(qn("w:type"), "dxa")


def ensure_angle_column(table):
    """Upgrade a 3-column table to 4 columns in place. No-op if already 4."""
    if len(table.rows[0].cells) >= 4:
        return
    from docx.table import _Cell
    grid = table._tbl.find(qn("w:tblGrid"))
    cols = grid.findall(qn("w:gridCol"))
    grid.append(copy.deepcopy(cols[-1]))
    for i, row in enumerate(table.rows):
        tr = row._element
        last_tc = tr.findall(qn("w:tc"))[-1]
        new_tc = copy.deepcopy(last_tc)
        tr.append(new_tc)
        set_cell_text(_Cell(new_tc, table), ANGLE_HEADER if i == 0 else "")
    # rebalance column widths
    cols = grid.findall(qn("w:gridCol"))
    for col, w in zip(cols, WIDTHS_4):
        col.set(qn("w:w"), str(w))
    for row in table.rows:
        for tc, w in zip(row._element.findall(qn("w:tc")), WIDTHS_4):
            set_cell_width(tc, w)


def fill_row(row, entry, ncols):
    set_cell_text(row.cells[0], str(entry["tab"]))
    set_cell_text(row.cells[1], entry["title"])
    set_cell_text(row.cells[2], entry["vehicle"])
    if ncols >= 4:
        set_cell_text(row.cells[3], entry.get("angle", ""))


def main():
    master, rows_json = sys.argv[1], sys.argv[2]
    rows = json.load(open(rows_json, encoding="utf-8"))
    if not rows:
        print("nothing to append")
        return

    created = False
    if not os.path.exists(master):
        shutil.copyfile(TEMPLATE, master)
        created = True

    d = docx.Document(master)
    table = d.tables[0]

    if any(r.get("angle") for r in rows):
        ensure_angle_column(table)
    ncols = len(table.rows[0].cells)

    # if the template placeholder row is still present, fill it with the first entry
    if table.rows[-1].cells[0].text.strip() == PLACEHOLDER:
        fill_row(table.rows[-1], rows.pop(0), ncols)
        appended = 1
    else:
        appended = 0

    source_tr = table.rows[-1]._element
    for entry in rows:
        new_tr = copy.deepcopy(source_tr)
        source_tr.addnext(new_tr)
        source_tr = new_tr
        from docx.table import _Row
        fill_row(_Row(new_tr, table), entry, ncols)
        appended += 1

    d.save(master)
    verb = "created" if created else "updated"
    print(f"{verb} {master}: appended {appended} row(s), table now has "
          f"{len(table.rows) - 1} entries, {ncols} columns")


if __name__ == "__main__":
    main()
