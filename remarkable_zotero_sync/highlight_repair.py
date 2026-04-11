from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from remarkable_zotero_sync.models import HighlightRepairResult

try:
    import fitz
except ImportError:
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "--break-system-packages",
            "pymupdf",
        ]
    )
    import fitz

OUTPUT_SUFFIX = " real_highlights"
YELLOW_FILL_LITERAL = "1 .92941177 .45882353 rg"
WORD_OVERLAP_THRESHOLD = 0.20
MERGE_GAP_X = 1.0
MERGE_GAP_Y = 1.0

RECT_PATTERN = re.compile(
    r"([-\d.]+)\s+([-\d.]+)\s+m\s+"
    r"([-\d.]+)\s+\2\s+l\s+"
    r"\3\s+([-\d.]+)\s+l\s+"
    r"\1\s+\4\s+l"
)

YELLOW_BLOCK_PATTERN = re.compile(
    r"q\s+1\s+\.92941177\s+\.45882353\s+rg\b.*?\bf\s+Q",
    re.S,
)


def is_highlight_repair_output(path: Path) -> bool:
    return path.stem.endswith(OUTPUT_SUFFIX)


def rect_area(rect: fitz.Rect) -> float:
    return max(0.0, rect.width) * max(0.0, rect.height)


def merge_rects(
    rects: list[fitz.Rect],
    gap_x: float = MERGE_GAP_X,
    gap_y: float = MERGE_GAP_Y,
) -> list[fitz.Rect]:
    if not rects:
        return []

    merged_rects: list[fitz.Rect] = []
    for rect in rects:
        current_rect = fitz.Rect(rect)
        merged = False
        for index, existing in enumerate(merged_rects):
            expanded = fitz.Rect(
                existing.x0 - gap_x,
                existing.y0 - gap_y,
                existing.x1 + gap_x,
                existing.y1 + gap_y,
            )
            if expanded.intersects(current_rect):
                merged_rects[index] = existing | current_rect
                merged = True
                break
        if not merged:
            merged_rects.append(current_rect)

    changed = True
    while changed:
        changed = False
        next_round: list[fitz.Rect] = []
        used = [False] * len(merged_rects)

        for index, rect in enumerate(merged_rects):
            if used[index]:
                continue

            current = fitz.Rect(rect)
            used[index] = True

            for other_index, other in enumerate(merged_rects):
                if used[other_index]:
                    continue
                expanded = fitz.Rect(
                    current.x0 - gap_x,
                    current.y0 - gap_y,
                    current.x1 + gap_x,
                    current.y1 + gap_y,
                )
                if expanded.intersects(other):
                    current |= other
                    used[other_index] = True
                    changed = True

            next_round.append(current)

        merged_rects = next_round

    return merged_rects


def patch_missing_extgstate_aliases(doc: fitz.Document) -> int:
    """
    Some reMarkable exports reference FXE2 / FXE3 aliases without defining them.
    This patch mirrors the working notebook logic:
    FXE1 -> Normal
    FXE3 -> Normal
    FXE4 -> Darken
    FXE2 -> Darken
    """

    patched = 0
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False)
        except Exception:
            continue

        if "/FXE1" not in obj or "/FXE4" not in obj:
            continue

        normal_match = re.search(r"/FXE1\s+(\d+)\s+0\s+R", obj)
        darken_match = re.search(r"/FXE4\s+(\d+)\s+0\s+R", obj)
        if not (normal_match and darken_match):
            continue

        normal_ref = f"{normal_match.group(1)} 0 R"
        darken_ref = f"{darken_match.group(1)} 0 R"

        changed = False
        if "/FXE2" not in obj:
            doc.xref_set_key(xref, "FXE2", darken_ref)
            changed = True
        if "/FXE3" not in obj:
            doc.xref_set_key(xref, "FXE3", normal_ref)
            changed = True

        if changed:
            patched += 1

    return patched


def get_contents_stream_xrefs(doc: fitz.Document, page: fitz.Page) -> list[int]:
    page_obj = doc.xref_object(page.xref, compressed=False)
    contents_match = re.search(r"/Contents\s+(\d+)\s+0\s+R", page_obj)
    if not contents_match:
        return []

    contents_xref = int(contents_match.group(1))
    contents_obj = doc.xref_object(contents_xref, compressed=False).strip()
    if contents_obj.startswith("["):
        return [int(value) for value in re.findall(r"(\d+)\s+0\s+R", contents_obj)]
    return [contents_xref]


def extract_highlight_rects_from_stream(stream_text: str) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for x1, y1, x2, y2 in RECT_PATTERN.findall(stream_text):
        rects.append(
            fitz.Rect(
                min(float(x1), float(x2)),
                min(float(y1), float(y2)),
                max(float(x1), float(x2)),
                max(float(y1), float(y2)),
            )
        )
    return rects


def remove_fake_yellow_fill_block(stream_text: str) -> str:
    return YELLOW_BLOCK_PATTERN.sub("", stream_text)


def pdf_rect_to_page_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    return fitz.Rect(
        rect.x0,
        page.rect.height - rect.y1,
        rect.x1,
        page.rect.height - rect.y0,
    )


def words_overlapping_rect(
    page: fitz.Page,
    rect: fitz.Rect,
    overlap_threshold: float = WORD_OVERLAP_THRESHOLD,
) -> list[tuple[int, int, int, fitz.Rect]]:
    hits: list[tuple[int, int, int, fitz.Rect]] = []
    for word in page.get_text("words"):
        word_rect = fitz.Rect(word[:4])
        overlap_area = (word_rect & rect).get_area()
        if overlap_area <= 0:
            continue

        overlap_fraction = overlap_area / max(rect_area(word_rect), 1e-6)
        if overlap_fraction >= overlap_threshold:
            hits.append((word[5], word[6], word[7], word_rect))
    return hits


def line_rects_from_word_hits(
    word_hits: list[tuple[int, int, int, fitz.Rect]]
) -> list[fitz.Rect]:
    grouped: dict[tuple[int, int], list[tuple[int, fitz.Rect]]] = {}
    for block_no, line_no, word_no, word_rect in word_hits:
        grouped.setdefault((block_no, line_no), []).append((word_no, word_rect))

    line_rects: list[fitz.Rect] = []
    for items in grouped.values():
        items.sort(key=lambda item: item[0])
        line_rect = fitz.Rect(items[0][1])
        for _, word_rect in items[1:]:
            line_rect |= word_rect
        line_rects.append(line_rect)

    line_rects.sort(key=lambda rect: (round(rect.y0, 1), round(rect.x0, 1)))
    return line_rects


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}{OUTPUT_SUFFIX}.pdf")


def repair_pdf(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    skip_if_output_exists: bool = True,
    verbose: bool = False,
) -> HighlightRepairResult:
    source_path = Path(input_path).expanduser().resolve()
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else default_output_path(source_path)
    )

    if skip_if_output_exists and destination.exists():
        return HighlightRepairResult(
            input_path=source_path,
            output_path=destination,
            status="skipped_existing_output",
            patched_extgstate_count=0,
            pages_with_highlights=0,
            parsed_highlight_rect_count=0,
            added_highlight_annotation_count=0,
        )

    if destination.exists():
        destination.unlink()

    doc = fitz.open(source_path)
    try:
        patched = patch_missing_extgstate_aliases(doc)
        total_rects = 0
        total_annots = 0
        pages_with_highlights = 0

        for page_index in range(len(doc)):
            page = doc[page_index]
            content_streams = get_contents_stream_xrefs(doc, page)
            overlay_streams = content_streams[1:] if len(content_streams) > 1 else []

            page_rects: list[fitz.Rect] = []
            for content_xref in overlay_streams:
                data = doc.xref_stream(content_xref)
                if not data:
                    continue

                stream_text = data.decode("latin1", errors="ignore")
                if YELLOW_FILL_LITERAL not in stream_text:
                    continue

                page_rects.extend(extract_highlight_rects_from_stream(stream_text))
                cleaned = remove_fake_yellow_fill_block(stream_text)
                if cleaned != stream_text:
                    doc.update_stream(content_xref, cleaned.encode("latin1"))

            page_rects = merge_rects(page_rects)
            if page_rects:
                pages_with_highlights += 1
                total_rects += len(page_rects)
                if verbose:
                    print(
                        f"Page {page_index + 1}: found {len(page_rects)} highlight rect(s)"
                    )

            for rect in page_rects:
                page_rect = pdf_rect_to_page_rect(page, rect)
                word_hits = words_overlapping_rect(page, page_rect)
                if not word_hits:
                    continue

                line_rects = line_rects_from_word_hits(word_hits)
                if not line_rects:
                    continue

                annot = page.add_highlight_annot(line_rects)
                annot.update()
                total_annots += 1

        if total_annots == 0 and patched == 0:
            return HighlightRepairResult(
                input_path=source_path,
                output_path=None,
                status="no_changes",
                patched_extgstate_count=patched,
                pages_with_highlights=pages_with_highlights,
                parsed_highlight_rect_count=total_rects,
                added_highlight_annotation_count=total_annots,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        doc.save(destination, garbage=4, deflate=True)
        return HighlightRepairResult(
            input_path=source_path,
            output_path=destination,
            status="saved",
            patched_extgstate_count=patched,
            pages_with_highlights=pages_with_highlights,
            parsed_highlight_rect_count=total_rects,
            added_highlight_annotation_count=total_annots,
        )
    finally:
        doc.close()
